"""Narrow agent-attention bridge for Anime Studio.

One function: `request_agent_attention(job_id, summary)` POSTs a SHORT,
FIXED-TEMPLATE notification to the loopback Host Service `POST /chat/inject`
so the owner's agent can look at a job that finished PARTIAL or ERROR.

Security argument for the `inject_chat` permission — the whole of it:
the injected text is built from a FIXED template with ONLY numeric /
enumerated substitutions (job id, quality-mode name, missing/unverified
scene counts, whitelist-filtered partial-reason keys). It NEVER interpolates
the user's theme, the storyboard, dialogue, warning strings, or any other
free text. Free text reaching the owner's chat would turn a generation skill
into a prompt-injection vector; typed-values-only is what keeps this
permission narrow enough to grant. Enforced structurally below: every
substitution is an int, a member of a closed enum, or a token matching
``^[a-z0-9_:]+$``; anything else is dropped, never quoted.

Generation must never depend on this call: it never raises, refuses loudly
on a non-loopback or credentialed Host Service URL or a missing token, and a
403 (owner has not granted `inject_chat`) is an ordinary refusal the caller
logs and continues past.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("anime_studio.host_bridge")

# Read the same way anime_worker.py does.
HOST_SERVICE_URL = (os.environ.get("HOST_SERVICE_URL") or "http://127.0.0.1:8767").rstrip("/")
HOST_SERVICE_TOKEN = os.environ.get("HOST_SERVICE_TOKEN", "")

_TIMEOUT_SEC = 10
_TEXT_CAP = 800
_MODES = ("low", "medium", "max")  # closed enum; anything else renders "unknown"
_REASON_KEY_RE = re.compile(r"^[a-z0-9_:]+$")
_JOB_ID_RE = re.compile(r"[^A-Za-z0-9_-]")
_MAX_REASON_KEYS = 8

# At most one successful-or-attempted network call per job id (process lifetime).
_attempted_job_ids: set[str] = set()
_attempted_lock = threading.Lock()

_TEMPLATE = (
    "Anime Studio job {job_id} finished with status {status} "
    "(quality mode: {mode}). Missing scenes: {missing}. "
    "Unverified scenes: {unverified}. Partial reasons: {reasons}. "
    "Please review the job in the Anime Studio tab."
)


def _is_loopback(url: str) -> bool:
    """Same guard as anime_worker.py: loopback only, no embedded credentials."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").strip()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _build_text(job_id: str, summary: dict) -> str:
    """Render the fixed template from TYPED values only (see module docstring)."""
    safe_job_id = _JOB_ID_RE.sub("", str(job_id))[:32] or "unknown"
    mode = summary.get("mode")
    safe_mode = mode if mode in _MODES else "unknown"
    status = summary.get("status")
    safe_status = status if status in ("partial", "error") else "unknown"

    def _count(key: str) -> int:
        try:
            return max(0, int(summary.get(key, 0)))
        except (TypeError, ValueError):
            return 0

    raw_reasons = summary.get("partial_reasons") or []
    if not isinstance(raw_reasons, (list, tuple)):
        raw_reasons = []
    reasons = [
        r for r in raw_reasons
        if isinstance(r, str) and _REASON_KEY_RE.match(r)
    ][:_MAX_REASON_KEYS]

    text = _TEMPLATE.format(
        job_id=safe_job_id,
        status=safe_status,
        mode=safe_mode,
        missing=_count("missing_scenes"),
        unverified=_count("unverified_scenes"),
        reasons=", ".join(reasons) or "none",
    )
    return text[:_TEXT_CAP]


def request_agent_attention(job_id: str, summary: dict) -> dict:
    """Ask the owner's agent to look at a PARTIAL/ERROR job. Never raises.

    Returns ``{"ok": bool, "status": int | None, "error": str}``. A 403 means
    the owner has not granted `inject_chat`; that is recorded in the result
    and the caller simply logs it — generation never depends on this call.
    """
    try:
        if not _is_loopback(HOST_SERVICE_URL):
            return {
                "ok": False,
                "status": None,
                "error": (
                    "refused: HOST_SERVICE_URL is not loopback or carries "
                    "embedded credentials"
                ),
            }
        if not HOST_SERVICE_TOKEN:
            return {"ok": False, "status": None, "error": "refused: empty HOST_SERVICE_TOKEN"}

        key = str(job_id)
        with _attempted_lock:
            if key in _attempted_job_ids:
                return {
                    "ok": False,
                    "status": None,
                    "error": "skipped: attention already attempted for this job id",
                }
            _attempted_job_ids.add(key)

        body = json.dumps({
            "text": _build_text(job_id, summary if isinstance(summary, dict) else {}),
            "chat_id": 0,
            "sender_label": "anime_studio",
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{HOST_SERVICE_URL}/chat/inject",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Skill-Token": HOST_SERVICE_TOKEN,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SEC) as response:  # noqa: S310 - loopback Host Service
                return {"ok": True, "status": int(response.status), "error": ""}
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                return {
                    "ok": False,
                    "status": 403,
                    "error": "inject_chat not granted by the owner (HTTP 403)",
                }
            return {"ok": False, "status": int(exc.code), "error": f"HTTP {exc.code}: {exc.reason}"}
        except Exception as exc:  # noqa: BLE001 - transport failure must not escape
            return {"ok": False, "status": None, "error": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001 - absolute never-raise contract
        log.warning("request_agent_attention internal failure: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "status": None, "error": f"{type(exc).__name__}: {exc}"}
