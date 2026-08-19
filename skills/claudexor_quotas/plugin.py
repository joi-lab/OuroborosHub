"""Claudexor Quotas — read-only quota/limit projection for authorized accounts.

Reads exactly one existing host endpoint (GET /api/claudexor/status) over
loopback and normalizes it for the widget. No daemon token is touched, no
gateway route is added to the core repo, nothing is mutated.

Every projection below preserves provenance: a facet that was not read or that
failed is reported as such, never as an empty or zero value.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FETCH_TIMEOUT_SEC = 25.0
STATUS_PATH = "/api/claudexor/status"

FACETS = ("catalog", "accounts", "quota")
READ_OK = "ok"
READ_STATES = (READ_OK, "not_read", "failed")


def _server_port(api: Any) -> int:
    """Resolve the live gateway port from the runtime's own port file."""
    for candidate in _port_candidates(api):
        try:
            value = int(str(candidate).strip())
        except (TypeError, ValueError):
            continue
        if 1 <= value <= 65535:
            return value
    return 8765


def _port_candidates(api: Any) -> List[Any]:
    out: List[Any] = []
    try:
        info = api.get_runtime_info() or {}
        for key in ("server_port", "port"):
            if info.get(key):
                out.append(info.get(key))
    except Exception:
        pass
    try:
        port_file = Path(api.get_state_dir()).resolve().parents[1] / "server_port"
        if port_file.is_file():
            out.append(port_file.read_text(encoding="utf-8"))
    except Exception:
        pass
    if os.environ.get("OUROBOROS_SERVER_PORT"):
        out.append(os.environ["OUROBOROS_SERVER_PORT"])
    return out


def _fetch_status(port: int) -> Tuple[Optional[Dict[str, Any]], str]:
    """Return (payload, transport_error). Exactly one of them is meaningful."""
    url = f"http://127.0.0.1:{port}{STATUS_PATH}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=FETCH_TIMEOUT_SEC) as response:
            body = response.read().decode("utf-8", "replace")
            code = int(getattr(response, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} from {STATUS_PATH}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if code != 200:
        return None, f"HTTP {code} from {STATUS_PATH}"
    try:
        payload = json.loads(body)
    except Exception:
        return None, "status response was not JSON"
    if not isinstance(payload, dict):
        return None, "status response was not a JSON object"
    return payload, ""


def facet_states(payload: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Per-facet provenance. An unusable reads block is indeterminate, not ok."""
    if not isinstance(payload, dict):
        return {facet: "indeterminate" for facet in FACETS}
    reads = payload.get("reads")
    if not isinstance(reads, dict):
        return {facet: "indeterminate" for facet in FACETS}
    out: Dict[str, str] = {}
    for facet in FACETS:
        raw = str(reads.get(facet) or "").strip()
        out[facet] = raw if raw in READ_STATES else "indeterminate"
    return out


def facet_note(states: Dict[str, str]) -> str:
    """Name exactly which facets did not answer; empty when all are ok."""
    bad = [f"{facet}: {state}" for facet, state in states.items() if state != READ_OK]
    return "; ".join(bad)


def _subject_key(value: Any) -> str:
    """Native login is subject_id null/''; a profile is its exact profile id."""
    if value is None:
        return ""
    return str(value).strip()


def _is_future(iso_text: Any) -> Optional[bool]:
    """True/False for a parseable instant, None when it cannot be parsed."""
    raw = str(iso_text or "").strip()
    if not raw:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed > _dt.datetime.now(_dt.timezone.utc)


def _constraint_view(constraint: Dict[str, Any]) -> Dict[str, Any]:
    try:
        ratio = float(constraint.get("used_ratio"))
    except (TypeError, ValueError):
        ratio = float("nan")
    used_pct: Optional[int] = (
        int(round(min(1.0, max(0.0, ratio)) * 100)) if math.isfinite(ratio) else None
    )
    models = constraint.get("applies_to_models")
    scoped = [str(m) for m in models if m] if isinstance(models, list) else []
    return {
        "id": str(constraint.get("id") or ""),
        "label": str(constraint.get("label") or constraint.get("id") or "constraint"),
        "used_pct": used_pct,
        "resets_at": str(constraint.get("resets_at") or ""),
        "cooldown_until": str(constraint.get("cooldown_until") or ""),
        "scoped_models": scoped,
        "window_seconds": constraint.get("window_seconds"),
    }


def _spent(view: Dict[str, Any]) -> bool:
    """A cooling (or unparseable-cooldown) or fully used window is spent."""
    if view["cooldown_until"]:
        future = _is_future(view["cooldown_until"])
        if future is None or future:
            return True
    return view["used_pct"] is not None and view["used_pct"] >= 100


def quota_for(
    snapshots: Any,
    harness_id: str,
    subject_id: str,
    quota_read: str,
) -> Dict[str, Any]:
    """Project quota for ONE account. Absence is stated, never invented."""
    if quota_read != READ_OK:
        return {
            "state": "not_checked",
            "label": f"Limits not checked — quota facet {quota_read}",
            "resets_at": "",
            "note": "",
            "constraints": [],
            "stale": [],
            "availability": "",
        }
    rows = [
        row for row in (snapshots if isinstance(snapshots, list) else [])
        if isinstance(row, dict)
        and str((row.get("subject") or {}).get("harness") or "") == harness_id
        and _subject_key((row.get("subject") or {}).get("subject_id")) == subject_id
    ]
    fresh = [row for row in rows if str(row.get("freshness") or "") == "fresh"]
    other = [row for row in rows if row not in fresh]
    stale_views = [
        {
            "observed_at": str(row.get("observed_at") or ""),
            "freshness": str(row.get("freshness") or "unknown"),
            "source": str(row.get("source") or ""),
            "constraints": [
                _constraint_view(c) for c in (row.get("constraints") or [])
                if isinstance(c, dict)
            ],
        }
        for row in other
    ]

    if not fresh:
        if stale_views:
            return {
                "state": "no_fresh_window",
                "label": "No fresh reading — last reading is stale",
                "resets_at": "",
                "note": "A stale reading does not gate routing; shown below as-is.",
                "constraints": [],
                "stale": stale_views,
                "availability": "",
            }
        return {
            "state": "no_data",
            "label": "No quota window reported for this account",
            "resets_at": "",
            "note": "",
            "constraints": [],
            "stale": [],
            "availability": "",
        }

    views: List[Dict[str, Any]] = []
    availability = ""
    for row in fresh:
        availability = availability or str((row.get("availability") or {}).get("state") or "")
        for constraint in row.get("constraints") or []:
            if isinstance(constraint, dict):
                views.append(_constraint_view(constraint))

    exhausted = False
    exhausted_resets = ""
    worst: Optional[Dict[str, Any]] = None
    scoped_spent: List[str] = []
    for view in views:
        spent = _spent(view)
        if view["scoped_models"]:
            if spent:
                scoped_spent.append(view["label"])
            continue
        if spent and not exhausted:
            exhausted = True
            exhausted_resets = view["cooldown_until"] or view["resets_at"]
        if view["used_pct"] is None:
            continue
        if worst is None or view["used_pct"] > worst["used_pct"]:
            worst = view

    note = ", ".join(sorted(set(scoped_spent)))
    note = f"per-model caps spent: {note}" if note else ""
    if exhausted:
        state, label = "exhausted", "Limit reached"
        resets_at = exhausted_resets or (worst or {}).get("resets_at", "")
    elif worst is not None:
        state, label = "ok", f"{worst['used_pct']}% used"
        resets_at = worst["resets_at"]
    else:
        state = "no_data"
        label = "Read, but no usage numbers reported"
        resets_at = ""
    return {
        "state": state,
        "label": label,
        "resets_at": str(resets_at or ""),
        "note": note,
        "constraints": views,
        "stale": stale_views,
        "availability": availability,
    }


def verification_view(
    verification: str,
    source: str,
    accounts_read: str,
    signed_in: bool,
) -> Dict[str, str]:
    """Honest verification wording; degraded reads can never look green."""
    verification = str(verification or "").strip()
    source = str(source or "").strip()
    if verification == "passed" and source == "vendor":
        view = {"tone": "ok", "label": "Verified live"}
    elif verification == "passed":
        detail = source or "local session"
        view = {"tone": "muted", "label": f"Signed in — not verified live ({detail})"}
    elif verification == "failed":
        view = {"tone": "warn", "label": "Verification failed"}
    elif signed_in:
        view = {"tone": "muted", "label": "Signed in — not verified live (local session)"}
    else:
        view = {"tone": "muted", "label": "Not verified"}
    if accounts_read != READ_OK:
        view = {"tone": "muted", "label": f"{view['label']} — last known"}
    return view


def build_groups(payload: Dict[str, Any], states: Dict[str, str]) -> List[Dict[str, Any]]:
    """One card per agent family; account rows inside it."""
    harnesses = payload.get("harnesses")
    harnesses = [h for h in harnesses if isinstance(h, dict)] if isinstance(harnesses, list) else []
    profiles_block = payload.get("profiles")
    profiles_block = profiles_block if isinstance(profiles_block, dict) else {}
    native_rows = [r for r in (profiles_block.get("harnessAccounts") or []) if isinstance(r, dict)]
    profile_rows = [r for r in (profiles_block.get("profiles") or []) if isinstance(r, dict)]
    snapshots = payload.get("quota")
    accounts_read = states.get("accounts", "indeterminate")
    quota_read = states.get("quota", "indeterminate")

    order: List[str] = [str(h.get("id") or "") for h in harnesses if h.get("id")]
    for row in native_rows:
        hid = str(row.get("harness_id") or "")
        if hid and hid not in order:
            order.append(hid)
    for row in profile_rows:
        hid = str((row.get("profile") or {}).get("harness_id") or "")
        if hid and hid not in order:
            order.append(hid)

    groups: List[Dict[str, Any]] = []
    for hid in order:
        meta = next((h for h in harnesses if str(h.get("id") or "") == hid), {})
        accounts: List[Dict[str, Any]] = []
        for row in native_rows:
            if str(row.get("harness_id") or "") != hid:
                continue
            accounts.append(_native_account(row, snapshots, hid, quota_read, accounts_read))
        for row in profile_rows:
            profile = row.get("profile") or {}
            if str(profile.get("harness_id") or "") != hid:
                continue
            accounts.append(_profile_account(
                row, native_rows, snapshots, hid, quota_read, accounts_read,
            ))
        groups.append({
            "harness_id": hid,
            "family_label": str(meta.get("display_name") or meta.get("displayName") or hid),
            "harness_status": str(meta.get("status") or ""),
            "harness_enabled": bool(meta.get("enabled")) if meta else None,
            "provider_family": str(meta.get("provider_family") or meta.get("providerFamily") or ""),
            "catalog_known": states.get("catalog") == READ_OK and bool(meta),
            "accounts": accounts,
            "accounts_signed_in": sum(1 for a in accounts if a["signed_in"]),
            "accounts_unavailable": accounts_read != READ_OK,
        })
    return groups


def _native_account(
    row: Dict[str, Any],
    snapshots: Any,
    hid: str,
    quota_read: str,
    accounts_read: str,
) -> Dict[str, Any]:
    identity = row.get("identity") or {}
    signed_in = bool(row.get("native_login_detected"))
    next_up = row.get("next_up") or {}
    return {
        "key": f"{hid}:native",
        "kind": "native",
        "label": "Default CLI login",
        "caption": "managed by the vendor CLI",
        "email": str(identity.get("email") or ""),
        "plan": str(identity.get("plan") or ""),
        "enabled": bool(row.get("native_credentials_enabled")),
        "signed_in": signed_in,
        "next_up": str(next_up.get("kind") or "") == "native",
        "last_verified_at": "",
        "verification": verification_view("", "", accounts_read, signed_in),
        "quota": quota_for(snapshots, hid, "", quota_read),
    }


def _profile_account(
    row: Dict[str, Any],
    native_rows: List[Dict[str, Any]],
    snapshots: Any,
    hid: str,
    quota_read: str,
    accounts_read: str,
) -> Dict[str, Any]:
    profile = row.get("profile") or {}
    status = row.get("status") or {}
    identity = row.get("identity") or {}
    profile_id = str(profile.get("profile_id") or "")
    verification = str(status.get("verification") or "")
    availability = str(status.get("availability") or "")
    signed_in = verification == "passed" or availability == "available"
    native = next((r for r in native_rows if str(r.get("harness_id") or "") == hid), {})
    next_up = native.get("next_up") or {}
    is_next = (
        str(next_up.get("kind") or "") == "profile"
        and str(next_up.get("profile_id") or "") == profile_id
    )
    return {
        "key": f"{hid}:{profile_id}",
        "kind": "profile",
        "label": str(profile.get("display_name") or profile_id or "account"),
        "caption": str(profile.get("credential_kind") or ""),
        "email": str(identity.get("email") or ""),
        "plan": str(identity.get("plan") or status.get("plan_label") or ""),
        "enabled": bool(profile.get("enabled")),
        "signed_in": bool(signed_in),
        "next_up": is_next,
        "last_verified_at": str(status.get("last_verified_at") or ""),
        "detail": str(status.get("detail") or ""),
        "availability": availability,
        "verification": verification_view(
            verification, str(status.get("verification_source") or ""),
            accounts_read, bool(signed_in),
        ),
        "quota": quota_for(snapshots, hid, profile_id, quota_read),
    }


def build_view(payload: Optional[Dict[str, Any]], transport_error: str) -> Dict[str, Any]:
    """The whole widget payload, provenance first."""
    states = facet_states(payload)
    daemon = (payload or {}).get("daemon")
    daemon = daemon if isinstance(daemon, dict) else {}
    runtime = daemon.get("runtime") if isinstance(daemon.get("runtime"), dict) else {}
    view: Dict[str, Any] = {
        "ok": bool(payload) and not transport_error,
        "transport_error": transport_error,
        "facets": states,
        "facet_note": facet_note(states),
        "daemon": {
            "state": str(daemon.get("state") or ("unknown" if payload else "")),
            "engine_version": str(daemon.get("engine_version") or ""),
            "self_started": bool(daemon.get("self_started")),
            "last_error": str(runtime.get("last_error") or daemon.get("last_error") or ""),
        },
        "groups": build_groups(payload, states) if isinstance(payload, dict) else [],
    }
    return view


def register(api: Any) -> None:
    def quotas_route(_request: Any) -> Dict[str, Any]:
        payload, transport_error = _fetch_status(_server_port(api))
        if transport_error:
            api.log("error", f"claudexor status read failed: {transport_error}")
        return build_view(payload, transport_error)

    api.register_route("quotas", quotas_route, methods=("GET",))
    api.register_ui_tab(
        "quotas",
        "Claudexor Quotas",
        icon="gauge",
        render={"kind": "module", "entry": "widget.js", "span": 2},
    )
