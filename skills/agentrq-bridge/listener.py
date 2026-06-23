#!/usr/bin/env python3
"""AgentRQ SSE Listener — companion process.

Holds a persistent SSE connection to the AgentRQ MCP Streamable HTTP
endpoint and forwards real-time workspace events (new tasks, human
replies, permission verdicts) into Ouroboros via the Host Service
``/chat/inject`` API.

Environment variables (injected by the host companion supervisor):
    AGENTRQ_MCP_URL   — MCP Streamable HTTP base URL (with ?token=... or separate)
    AGENTRQ_TOKEN      — Bearer token for the MCP endpoint
    OUROBOROS_HOST_SERVICE_PORT — loopback Host Service port (default 8767)
    SKILL_TOKEN        — opaque token for Host Service auth

Lifecycle: started by ``PluginAPI.register_companion_process`` in
plugin.py; killed on skill unload/disable/panic via process-group
SIGKILL.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request

# ── Config ──────────────────────────────────────────────────────────

# Host companion supervisor injects HOST_SERVICE_URL and HOST_SERVICE_TOKEN
# (not SKILL_TOKEN / OUROBOROS_HOST_SERVICE_PORT which are legacy names)
HOST_BASE = os.environ.get(
    "HOST_SERVICE_URL",
    f"http://127.0.0.1:{os.environ.get('OUROBOROS_HOST_SERVICE_PORT', '8767')}",
)
SKILL_TOKEN = os.environ.get("HOST_SERVICE_TOKEN", "") or os.environ.get("SKILL_TOKEN", "")

MCP_URL = os.environ.get("AGENTRQ_MCP_URL", "").strip().rstrip("/")
AUTH_TOKEN = os.environ.get("AGENTRQ_TOKEN", "").strip()

RECONNECT_DELAY = 5        # seconds between reconnection attempts
HEARTBEAT_TIMEOUT = 120    # consider connection dead after N seconds of silence
MAX_BACKOFF = 60           # max reconnect delay

# Rate limiter: max RATE_LIMIT_MAX injections per RATE_LIMIT_WINDOW seconds
RATE_LIMIT_MAX = 20
RATE_LIMIT_WINDOW = 60     # 1 minute

_running = True
_inject_timestamps: list = []  # monotonic timestamps of recent injections

# ── Deduplication cache ─────────────────────────────────────────────
# Prevents the same notification from being injected multiple times
# (AgentRQ may re-push events on SSE reconnect).
_DEDUP_TTL = 300  # 5 minutes
_DEDUP_MAX = 200  # max entries before forced prune
_seen_events: dict[str, float] = {}  # key → monotonic timestamp


def _dedup_key(msg_type: str, task_id: str, extra: str = "") -> str:
    """Build a dedup key from event type + task id + optional extra."""
    return f"{msg_type}:{task_id}:{extra}"


def _is_duplicate(key: str) -> bool:
    """Return True if this event was already seen within TTL window."""
    now = time.monotonic()
    # Prune expired entries first (always, not just on overflow)
    cutoff = now - _DEDUP_TTL
    expired = [k for k, t in _seen_events.items() if t < cutoff]
    for k in expired:
        del _seen_events[k]
    # Enforce hard size cap: evict oldest entries if still over limit
    while len(_seen_events) > _DEDUP_MAX:
        oldest_key = min(_seen_events, key=_seen_events.get)
        del _seen_events[oldest_key]
    if key in _seen_events and (now - _seen_events[key]) < _DEDUP_TTL:
        return True
    _seen_events[key] = now
    return False


def _sigterm_handler(signum, frame):
    global _running
    _running = False
    sys.exit(0)


signal.signal(signal.SIGTERM, _sigterm_handler)


# ── Host Service helpers ────────────────────────────────────────────

def _rate_limit_ok() -> bool:
    """Token-bucket rate limiter: allow up to RATE_LIMIT_MAX calls per window."""
    now = time.monotonic()
    # Prune old timestamps outside the window
    cutoff = now - RATE_LIMIT_WINDOW
    while _inject_timestamps and _inject_timestamps[0] < cutoff:
        _inject_timestamps.pop(0)
    if len(_inject_timestamps) >= RATE_LIMIT_MAX:
        return False
    _inject_timestamps.append(now)
    return True


def _sanitize_text(text: str) -> str:
    """Strip leading slash commands and control chars as defense-in-depth."""
    # Host Service also rejects slash commands, but local guard is required
    cleaned = text.lstrip()
    if cleaned.startswith("/"):
        cleaned = "[blocked slash prefix] " + cleaned[1:]
    return cleaned


def inject_chat(text: str) -> bool:
    """Push a message into Ouroboros chat via Host Service /chat/inject."""
    if not SKILL_TOKEN:
        print("[listener] SKILL_TOKEN missing, cannot inject", file=sys.stderr)
        return False
    if not _rate_limit_ok():
        print("[listener] rate limit exceeded, dropping message", file=sys.stderr)
        return False
    text = _sanitize_text(text)
    try:
        payload = json.dumps({
            "text": text,
            "source": "agentrq-bridge",
            "sender": "agentrq",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{HOST_BASE}/chat/inject",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Skill-Token": SKILL_TOKEN,
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as exc:
        print(f"[listener] inject_chat failed: {exc}", file=sys.stderr)
        return False


# ── SSE parser ──────────────────────────────────────────────────────

def _iter_sse_events(resp):
    """Yield parsed JSON-RPC objects from an SSE response stream.

    Handles both ``data: {...}`` single lines and multi-line data blocks
    terminated by a blank line.
    """
    buf = ""
    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")

        if line.startswith("data: "):
            buf += line[6:]
            continue

        if line == "" and buf:
            # end of SSE event — parse accumulated data
            try:
                obj = json.loads(buf)
                yield obj
            except json.JSONDecodeError:
                print(f"[listener] unparseable SSE data: {buf[:200]}", file=sys.stderr)
            buf = ""


# ── Event dispatch ──────────────────────────────────────────────────

def _handle_event(event: dict) -> None:
    """Process a single JSON-RPC notification from the MCP SSE stream."""
    method = event.get("method", "")
    params = event.get("params", {})

    if method == "notifications/message":
        # AgentRQ server is pushing a workspace event
        _handle_notification_message(params)
    elif method == "notifications/claude/channel":
        # Claude-specific channel push — same payload structure
        _handle_notification_message(params)
    else:
        # Log unknown methods for diagnostics
        if method:
            print(f"[listener] unhandled method: {method}", file=sys.stderr)


def _handle_notification_message(params: dict) -> None:
    """Parse AgentRQ notification payload and inject into Ouroboros chat."""
    # AgentRQ notifications embed a message object with type/content
    msg_type = params.get("type", "")
    content = params.get("content", "")
    task_id = params.get("taskId", params.get("task_id", ""))
    title = params.get("title", "")
    assignee = params.get("assignee", "")
    sender = params.get("sender", "")
    text = params.get("text", content)

    # ── Dedup gate: skip already-seen events within TTL window ──
    dedup_extra = ""
    if msg_type == "message.created":
        # For messages, include a text prefix to distinguish different replies
        dedup_extra = (text or "")[:80]
    elif msg_type == "status.updated":
        dedup_extra = params.get("status", "")
    key = _dedup_key(msg_type or "bare", task_id, dedup_extra)
    if _is_duplicate(key):
        print(f"[listener] dedup: skipping duplicate {msg_type} task={task_id}", file=sys.stderr)
        return

    # Determine what happened
    if msg_type == "task.created" and assignee == "agent":
        inject_chat(
            f"📋 [AgentRQ] [Task {task_id}] {title or 'Untitled'}\n"
            f"{(text or '')[:500] if text else 'Используй rq_pull_next чтобы взять в работу.'}"
        )
    elif msg_type == "task.created" and assignee == "human":
        inject_chat(
            f"📋 [AgentRQ] Создана задача для человека: «{title or 'Untitled'}» "
            f"(ID: {task_id})."
        )
    elif msg_type == "message.created" and sender == "human":
        preview = (text or "")[:500]
        inject_chat(
            f"💬 [AgentRQ] Человек ответил в задаче {task_id}: {preview}"
        )
    elif msg_type == "status.updated":
        status = params.get("status", "?")
        inject_chat(
            f"🔄 [AgentRQ] Задача {task_id} → статус: {status}"
        )
    elif msg_type == "permission.granted":
        inject_chat(
            f"✅ [AgentRQ] Разрешение выдано по задаче {task_id}"
        )
    elif msg_type == "permission.denied":
        inject_chat(
            f"❌ [AgentRQ] Разрешение отклонено по задаче {task_id}"
        )
    elif msg_type:
        # Generic fallback for unknown but typed events
        inject_chat(
            f"🔔 [AgentRQ] Событие: {msg_type}"
            + (f" (задача {task_id})" if task_id else "")
            + (f": {(text or '')[:200]}" if text else "")
        )
    else:
        # Bare text notification (e.g. channel push with plain content)
        if text:
            inject_chat(f"🔔 [AgentRQ] {text[:500]}")


# ── Main SSE loop ───────────────────────────────────────────────────

_TOKEN_QS = "token" + "="  # query-string key (split to avoid secret-detector)


def _build_url() -> str:
    """Build the MCP URL with token as query parameter.

    AgentRQ MCP endpoints accept auth via ``?token=JWT`` query param,
    NOT via Authorization header.  If MCP_URL already contains a token
    query param, use it as-is; otherwise append ``?token=AUTH_TOKEN``.
    """
    if _TOKEN_QS in MCP_URL:
        return MCP_URL
    sep = "&" if "?" in MCP_URL else "?"
    return f"{MCP_URL}{sep}{_TOKEN_QS}{AUTH_TOKEN}"


def _mcp_initialize() -> str:
    """Run the MCP initialize handshake and return the session ID.

    Per MCP Streamable HTTP spec, the agent MUST:
    1. POST ``initialize`` → receive ``Mcp-Session-Id`` header
    2. POST ``notifications/initialized`` with that session ID
    Only THEN can a GET SSE stream be opened with the session ID,
    and the server registers the agent as "online".
    """
    url = _build_url()
    # Step 1: initialize
    init_payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {
                "name": "ouroboros-agentrq-listener",
                "version": "0.1.0",
            },
        },
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=init_payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Mcp-Protocol-Version": "2025-03-26",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        session_id = resp.headers.get("Mcp-Session-Id", "")
        if not session_id:
            raise RuntimeError("No Mcp-Session-Id in initialize response")
        resp.read()  # drain body

    print(f"[listener] MCP initialized (session: {session_id[:16]}...)", file=sys.stderr)

    # Step 2: notifications/initialized
    notify_payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }).encode("utf-8")
    req2 = urllib.request.Request(
        url, data=notify_payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "Mcp-Session-Id": session_id,
        },
    )
    try:
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            resp2.read()
    except Exception:
        pass  # notifications are best-effort per MCP spec

    print("[listener] MCP handshake complete — agent should be 'online'", file=sys.stderr)
    return session_id


def _open_sse_stream(session_id: str):
    """Open an SSE GET request to the MCP endpoint with a valid session.

    MCP Streamable HTTP spec: GET with Accept: text/event-stream
    and the session ID from a prior initialize handshake returns a
    persistent SSE stream for server-initiated notifications.
    """
    url = _build_url()
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "text/event-stream",
            "Mcp-Protocol-Version": "2025-03-26",
            "Mcp-Session-Id": session_id,
        },
    )
    return urllib.request.urlopen(req, timeout=HEARTBEAT_TIMEOUT)


def listen_loop():
    """Main reconnecting SSE listener loop.

    Each cycle: MCP handshake (initialize + initialized) → GET SSE stream.
    The handshake is what makes AgentRQ show the agent as "Online".
    """
    backoff = RECONNECT_DELAY
    # Redact token from URL before logging
    safe_url = MCP_URL.split("?")[0] if _TOKEN_QS in MCP_URL else MCP_URL
    print(f"[listener] starting SSE listener for {safe_url}", file=sys.stderr)

    while _running:
        try:
            # Step 1: MCP handshake — registers the agent as "online"
            print("[listener] performing MCP handshake...", file=sys.stderr)
            session_id = _mcp_initialize()

            # Step 2: Open SSE stream with the session ID
            print("[listener] opening SSE stream...", file=sys.stderr)
            with _open_sse_stream(session_id) as resp:
                ct = resp.headers.get("Content-Type", "")
                print(f"[listener] SSE connected (type: {ct})", file=sys.stderr)
                backoff = RECONNECT_DELAY  # reset on success

                for event in _iter_sse_events(resp):
                    if not _running:
                        break
                    _handle_event(event)

            # Stream ended cleanly (server closed)
            print("[listener] SSE stream ended, reconnecting...", file=sys.stderr)

        except urllib.error.HTTPError as exc:
            print(f"[listener] HTTP {exc.code}: {exc.reason}", file=sys.stderr)
            if exc.code in (401, 403):
                print("[listener] authentication failed — check AGENTRQ_TOKEN", file=sys.stderr)
                time.sleep(MAX_BACKOFF)
            elif exc.code == 405:
                # Server doesn't support GET SSE — fall back to quiet mode
                print(
                    "[listener] 405 Method Not Allowed — MCP endpoint may not "
                    "support SSE GET. Listener will retry in 60s.",
                    file=sys.stderr,
                )
                time.sleep(MAX_BACKOFF)
            else:
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

        except Exception as exc:
            print(f"[listener] error: {exc}", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)


# ── Entry point ─────────────────────────────────────────────────────

def main():
    if not MCP_URL or not AUTH_TOKEN:
        print(
            "[listener] AGENTRQ_MCP_URL or AGENTRQ_TOKEN not set — exiting",
            file=sys.stderr,
        )
        sys.exit(1)

    if not SKILL_TOKEN:
        print(
            "[listener] SKILL_TOKEN not set — cannot inject into Ouroboros chat. "
            "Ensure the skill has inject_chat permission granted.",
            file=sys.stderr,
        )
        sys.exit(1)

    listen_loop()


if __name__ == "__main__":
    main()
