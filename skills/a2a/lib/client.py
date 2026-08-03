from __future__ import annotations

import json
import os
import urllib.parse
import uuid
from typing import Any, Dict


def _origin(url: str) -> str:
    """Normalized scheme://host:port, or "" when the URL is not usable http(s)."""
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{parsed.hostname.lower()}:{port}"


def _auth_for(url: str):
    """Peer credentials are bound to ONE operator-configured origin.

    A process-wide credential attached to every caller-supplied URL meant a
    model-selected or otherwise untrusted peer address received the global peer
    password — an outbound secret leak to an arbitrary host. The credential is now
    sent ONLY when the request's origin exactly matches the origin of the
    explicitly configured A2A_CLIENT_PEER_URL; every other peer is contacted
    anonymously. Both values are ordinary process configuration for the client
    tools, not forwarded core settings (env_from_settings stays empty).
    """
    password = os.environ.get("A2A_CLIENT_PASSWORD", "").strip()
    expected = _origin(os.environ.get("A2A_CLIENT_PEER_URL", ""))
    target = _origin(url)
    if not password or not expected or not target or target != expected:
        return None
    return ("ouroboros", password)


def discover(url: str) -> str:
    import httpx

    base = str(url or "").rstrip("/")
    # A2: try the v0.3 well-known path first, then fall back to the legacy one, so this client
    # interoperates with peers that publish either name.
    card = None
    last_err: Any = None
    for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
        try:
            response = httpx.get(f"{base}{path}", auth=_auth_for(base), timeout=10)
            response.raise_for_status()
            card = response.json()
            break
        except Exception as exc:
            last_err = exc
    if card is None:
        return json.dumps({"error": f"Failed to fetch agent card: {last_err}"})
    return json.dumps({
        "name": card.get("name", ""),
        "description": card.get("description", ""),
        "version": card.get("version", ""),
        "url": card.get("url", base),
        "capabilities": card.get("capabilities", {}),
        "skills": card.get("skills", []),
    }, ensure_ascii=False, indent=2)


def send(url: str, message: str, task_id: str = "", context_id: str = "") -> str:
    import httpx

    base = str(url or "").rstrip("/")
    request_id = uuid.uuid4().hex
    msg: Dict[str, Any] = {
        "messageId": request_id,
        "role": "user",
        "parts": [{"kind": "text", "text": str(message or "")}],
    }
    if task_id:
        msg["taskId"] = task_id
    if context_id:
        msg["contextId"] = context_id
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {"message": msg},
    }
    try:
        response = httpx.post(f"{base}/", json=payload, auth=_auth_for(base), timeout=120)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return json.dumps({"error": f"Request failed: {exc}"})
    return json.dumps(data, ensure_ascii=False, indent=2)


def status(url: str, task_id: str) -> str:
    import httpx

    base = str(url or "").rstrip("/")
    request_id = uuid.uuid4().hex
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tasks/get",
        "params": {"id": str(task_id or "")},
    }
    try:
        response = httpx.post(f"{base}/", json=payload, auth=_auth_for(base), timeout=30)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return json.dumps({"error": f"Request failed: {exc}"})
    return json.dumps(data, ensure_ascii=False, indent=2)


def stream(url: str, message: str, task_id: str = "", context_id: str = "") -> str:
    """A1: optional streaming entrypoint. The Ouroboros A2A executor emits a SINGLE final Task
    event (interop, not incremental progress), so streaming degrades to a normal send() that
    returns the completed task. Provided so a stream-only caller has a working method."""
    return send(url, message, task_id, context_id)
