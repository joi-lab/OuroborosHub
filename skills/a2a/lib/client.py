from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict


def _auth():
    password = os.environ.get("A2A_CLIENT_PASSWORD", "").strip()
    return ("ouroboros", password) if password else None


def discover(url: str) -> str:
    import httpx

    base = str(url or "").rstrip("/")
    # A2: try the v0.3 well-known path first, then fall back to the legacy one, so this client
    # interoperates with peers that publish either name.
    card = None
    last_err: Any = None
    for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
        try:
            response = httpx.get(f"{base}{path}", auth=_auth(), timeout=10)
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
        response = httpx.post(f"{base}/", json=payload, auth=_auth(), timeout=120)
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
        response = httpx.post(f"{base}/", json=payload, auth=_auth(), timeout=30)
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
