from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import inspect
import ipaddress
import re
import time
import urllib.parse
import uuid
import base64
from typing import Any, Dict, List

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

try:
    from a2a.server.agent_execution.agent_executor import AgentExecutor
    from a2a.server.agent_execution.context import RequestContext
    from a2a.server.events.event_queue import EventQueue
    from a2a.server.request_handlers.default_request_handler import LegacyRequestHandler
    from a2a.server.routes.agent_card_routes import create_agent_card_routes
    from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
    from a2a.types import (
        AgentCapabilities,
        AgentCard,
        AgentSkill,
        Artifact,
        Part,
        Role,
        Task,
        TaskState,
        TaskStatus,
    )
    _A2A_SDK_AVAILABLE = True
except Exception:
    _A2A_SDK_AVAILABLE = False

logger = logging.getLogger("a2a_daemon")

STATE_DIR = pathlib.Path(os.environ.get("OUROBOROS_SKILL_STATE_DIR") or ".")

# Card version — kept in step with the skill version (SKILL.md / catalog entry).
A2A_CARD_VERSION = "1.1.1"


def _is_loopback(host: str) -> bool:
    clean = str(host or "").strip().strip("[]")
    if clean == "localhost":
        return True
    try:
        return ipaddress.ip_address(clean).is_loopback
    except ValueError:
        return False


def _host_service_hostname(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password:
        raise RuntimeError("HOST_SERVICE_URL must not contain userinfo")
    return parsed.hostname or ""


HOST_SERVICE_URL = os.environ.get("HOST_SERVICE_URL", "http://127.0.0.1:8767").rstrip("/")


class _SkillToken:
    """Companion-side SkillToken wrapper: prevents accidental logging of the raw token."""

    __slots__ = ("_value",)

    def __init__(self, raw: str) -> None:
        self._value = raw

    def use_in_request(self) -> str:
        """Deliberate access at request construction sites."""
        return self._value

    def __str__(self) -> str:
        return "<SkillToken:redacted>"

    def __repr__(self) -> str:
        return "<SkillToken:redacted>"


_HOST_TOKEN = _SkillToken(os.environ.get("HOST_SERVICE_TOKEN", ""))

# Enforce loopback-only Host Service calls (checklist item 12: host_token_handling)
if not _is_loopback(_host_service_hostname(HOST_SERVICE_URL)):
    raise RuntimeError(
        "HOST_SERVICE_URL must be a loopback address; "
        "refusing to send skill token to a non-local endpoint"
    )


def _load_settings() -> Dict[str, Any]:
    path = STATE_DIR / "settings.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


_SETTINGS = _load_settings()
A2A_HOST = os.environ.get("A2A_HOST") or str(_SETTINGS.get("A2A_HOST") or "127.0.0.1")
A2A_PORT = int(os.environ.get("A2A_PORT") or _SETTINGS.get("A2A_PORT") or "18800")

# Distinguish an OPERATOR-SET name/description from the built-in default. When the
# operator explicitly configures A2A_AGENT_NAME / A2A_AGENT_DESCRIPTION (env or
# settings), that value WINS over the live /identity value. When neither is set,
# the card is populated from the host /identity endpoint, falling back to these
# defaults only if /identity is unavailable.
_A2A_AGENT_NAME_EXPLICIT = (os.environ.get("A2A_AGENT_NAME") or str(_SETTINGS.get("A2A_AGENT_NAME") or "")).strip()
_A2A_AGENT_DESCRIPTION_EXPLICIT = (os.environ.get("A2A_AGENT_DESCRIPTION") or str(_SETTINGS.get("A2A_AGENT_DESCRIPTION") or "")).strip()
A2A_AGENT_NAME = _A2A_AGENT_NAME_EXPLICIT or "Ouroboros"
A2A_AGENT_DESCRIPTION = _A2A_AGENT_DESCRIPTION_EXPLICIT or "Ouroboros A2A peer"
A2A_SERVER_PASSWORD = (os.environ.get("A2A_SERVER_PASSWORD") or str(_SETTINGS.get("A2A_SERVER_PASSWORD") or "")).strip()

# Bounded retry for the host tool-schema fetch: the companion routinely starts
# before the host chat-agent is built, so GET /tools/schemas answers 200 with an
# EMPTY tools list for the first few seconds. An empty 200 must be retried like a
# transient failure (not accepted as final), or the card collapses to the
# identity-only entry. The window is generous enough to survive that startup race.
_TOOLS_FETCH_ATTEMPTS = 8
_TOOLS_FETCH_BACKOFF_SEC = 0.5
_HOST_FETCH_TIMEOUT_SEC = 5

# Last non-empty tool list seen this process. Once the card has populated, a later
# transient empty fetch must never regress it back to the identity-only entry
# (self-healing stability across per-request rebuilds and the startup bake).
_LAST_GOOD_TOOLS: List[Dict[str, Any]] = []


def _setting_int(name: str, default: int, *, minimum: int = 1, maximum: int = 600) -> int:
    try:
        value = int(os.environ.get(name) or _SETTINGS.get(name) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


A2A_MAX_CONCURRENT = _setting_int("A2A_MAX_CONCURRENT", 5, minimum=1, maximum=20)
A2A_RESPONSE_TIMEOUT_SEC = _setting_int("A2A_RESPONSE_TIMEOUT_SEC", 600, minimum=1, maximum=600)
_A2A_SEMAPHORE = None
_SLASH_COMMAND_RE = re.compile(r"^\s*/[A-Za-z]")


def _get_semaphore() -> asyncio.Semaphore:
    global _A2A_SEMAPHORE
    if _A2A_SEMAPHORE is None:
        _A2A_SEMAPHORE = asyncio.Semaphore(A2A_MAX_CONCURRENT)
    return _A2A_SEMAPHORE


def _host_headers() -> Dict[str, str]:
    return {"X-Skill-Token": _HOST_TOKEN.use_in_request()}


class _A2AAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _is_loopback(A2A_HOST):
            return await call_next(request)
        expected = "Basic " + base64.b64encode(f"ouroboros:{A2A_SERVER_PASSWORD}".encode()).decode()
        if not A2A_SERVER_PASSWORD or request.headers.get("authorization") != expected:
            return JSONResponse({"error": "A2A authentication required"}, status_code=401)
        return await call_next(request)


def _tasks_dir() -> pathlib.Path:
    path = STATE_DIR / "tasks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _task_path(task_id: str) -> pathlib.Path:
    safe = "".join(ch for ch in str(task_id or "") if ch.isalnum() or ch in ("-", "_")) or uuid.uuid4().hex
    return _tasks_dir() / f"{safe}.json"


def _save_task(task: Dict[str, Any]) -> None:
    _task_path(str(task["id"])).write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_task(task_id: str) -> Dict[str, Any] | None:
    path = _task_path(task_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _fetch_identity() -> Dict[str, str]:
    """Best-effort read of the host's real name/description from GET /identity.

    Returns {} when the endpoint is unavailable so the caller can fall back to the
    configured A2A_AGENT_NAME / A2A_AGENT_DESCRIPTION defaults.
    """
    try:
        response = httpx.get(
            f"{HOST_SERVICE_URL}/identity", headers=_host_headers(), timeout=_HOST_FETCH_TIMEOUT_SEC
        )
        if response.status_code == 200:
            data = response.json() or {}
            return {
                "name": str(data.get("name") or "").strip(),
                "description": str(data.get("description") or "").strip(),
            }
        logger.warning("a2a agent-card: /identity returned status %s", response.status_code)
    except Exception as exc:
        logger.warning("a2a agent-card: /identity unavailable (%s)", exc)
    return {}


def _fetch_tool_schemas() -> List[Dict[str, Any]]:
    """Fetch the host tool schemas, treating an empty list as "not ready yet".

    The companion routinely starts before the host chat-agent is built, so
    GET /tools/schemas answers 200 {"tools": []} for the first few seconds. A
    single 200 must NOT be accepted as final when the list is empty — that was
    the regression that let the agent card collapse to the identity-only entry
    on a peer whose host was still warming up. So an empty 200 is retried like a
    transient failure; the last non-empty result is cached at module scope; and
    a populated card is never regressed back to empty. If every attempt yields
    empty we serve the last known-good tool list when we have one, and fall back
    to the identity-derived entry only when we have never seen a populated list.
    """
    global _LAST_GOOD_TOOLS
    last_error = "no attempt made"
    for attempt in range(_TOOLS_FETCH_ATTEMPTS):
        try:
            response = httpx.get(
                f"{HOST_SERVICE_URL}/tools/schemas",
                headers=_host_headers(),
                timeout=_HOST_FETCH_TIMEOUT_SEC,
            )
            if response.status_code == 200:
                tools = response.json().get("tools") or []
                if tools:
                    _LAST_GOOD_TOOLS = tools
                    return tools
                last_error = "host returned an empty tool list (chat-agent not ready yet)"
            else:
                last_error = f"status {response.status_code}"
        except Exception as exc:
            last_error = str(exc)
        if attempt + 1 < _TOOLS_FETCH_ATTEMPTS:
            time.sleep(_TOOLS_FETCH_BACKOFF_SEC * (attempt + 1))
    if _LAST_GOOD_TOOLS:
        logger.warning(
            "a2a agent-card: /tools/schemas empty after %d attempts (%s); "
            "serving last known-good tool list (%d tools)",
            _TOOLS_FETCH_ATTEMPTS,
            last_error,
            len(_LAST_GOOD_TOOLS),
        )
        return _LAST_GOOD_TOOLS
    logger.warning(
        "a2a agent-card: /tools/schemas unavailable after %d attempts (%s); "
        "card will advertise the identity-derived capability entry only",
        _TOOLS_FETCH_ATTEMPTS,
        last_error,
    )
    return []


def _resolve_identity() -> Dict[str, str]:
    """Resolve the card's top-level name/description.

    Operator-set A2A_AGENT_NAME / A2A_AGENT_DESCRIPTION win; otherwise the live
    /identity value is used; otherwise the built-in defaults.
    """
    identity = _fetch_identity()
    name = _A2A_AGENT_NAME_EXPLICIT or identity.get("name") or A2A_AGENT_NAME
    description = _A2A_AGENT_DESCRIPTION_EXPLICIT or identity.get("description") or A2A_AGENT_DESCRIPTION
    return {"name": name, "description": description}


def _agent_card() -> Dict[str, Any]:
    ident = _resolve_identity()
    name = ident["name"]
    description = ident["description"]

    skills = []
    for schema in _fetch_tool_schemas():
        func = schema.get("function", schema) if isinstance(schema, dict) else {}
        tool_name = str(func.get("name") or "")
        if tool_name:
            skills.append({
                "id": tool_name,
                "name": tool_name,
                "description": str(func.get("description") or "")[:200],
                "tags": [tool_name.split("_", 1)[0] if "_" in tool_name else "tool"],
            })

    # No contentless collapse: when the tool list is genuinely empty (host chat-agent
    # not ready, empty registry, or a transient fetch failure), still advertise the
    # REAL identity-derived description plus a single honest capability entry — never
    # a bare "General" / "Ouroboros A2A peer" stub.
    if not skills:
        skills = [{
            "id": "ouroboros",
            "name": name,
            "description": description or A2A_AGENT_DESCRIPTION,
            "tags": ["ouroboros", "agent"],
        }]

    base_url = f"http://{A2A_HOST}:{A2A_PORT}/"
    return {
        "name": name,
        "description": description,
        "url": base_url,
        "version": A2A_CARD_VERSION,
        # A2: advertise the A2A v0.3 transport interface so v0.3-aware clients can negotiate.
        "protocolVersion": "0.3.0",
        "preferredTransport": "JSONRPC",
        "additionalInterfaces": [{"url": base_url, "transport": "JSONRPC"}],
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": skills,
    }


def _sdk_agent_card():
    card = _agent_card()
    return AgentCard(
        name=card["name"],
        description=card["description"],
        version=card["version"],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id=str(skill.get("id") or skill.get("name") or "general"),
                name=str(skill.get("name") or skill.get("id") or "General"),
                description=str(skill.get("description") or ""),
                tags=list(skill.get("tags") or ["general"]),
            )
            for skill in card.get("skills", [])
        ],
    )


async def agent_card(_request: Request) -> JSONResponse:
    return JSONResponse(_agent_card())


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "a2a"})


def _extract_text(params: Dict[str, Any]) -> str:
    message = params.get("message") or {}
    parts = message.get("parts") or []
    texts = []
    for part in parts:
        if isinstance(part, dict):
            texts.append(str(part.get("text") or ""))
    return "\n".join(text for text in texts if text).strip()


async def jsonrpc(request: Request) -> JSONResponse:
    payload = await request.json()
    request_id = payload.get("id") or uuid.uuid4().hex
    method = str(payload.get("method") or "")
    params = payload.get("params") or {}
    if method == "tasks/get":
        task = _load_task(str(params.get("id") or ""))
        if not task:
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32004, "message": "task not found"}})
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": task})
    if method != "message/send":
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}})
    text = _extract_text(params)
    task_id = str((params.get("message") or {}).get("taskId") or uuid.uuid4().hex)
    try:
        response_text = await _dispatch_to_host(text)
        task = {
            "id": task_id,
            "contextId": (params.get("message") or {}).get("contextId") or task_id,
            "status": {"state": "completed"},
            "artifacts": [{"parts": [{"kind": "text", "text": response_text}]}],
        }
    except Exception as exc:
        task = {
            "id": task_id,
            "status": {"state": "failed", "message": {"parts": [{"kind": "text", "text": str(exc)}]}},
        }
    _save_task(task)
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": task})


class OuroborosExecutor(AgentExecutor if _A2A_SDK_AVAILABLE else object):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        parts = getattr(getattr(context, "message", None), "parts", []) or []
        text = "\n".join(str(getattr(part, "text", "") or "") for part in parts if getattr(part, "text", ""))
        task_id = getattr(context, "task_id", "") or uuid.uuid4().hex
        response_text = await _dispatch_to_host(text)
        task = Task(
            id=task_id,
            context_id=getattr(context, "context_id", "") or task_id,
            status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            artifacts=[
                Artifact(
                    artifact_id=uuid.uuid4().hex,
                    parts=[Part(text=response_text)],
                )
            ],
        )
        result = event_queue.enqueue_event(task)
        if inspect.isawaitable(result):
            await result
        # A1: a single final Task event terminates the stream (the executor does not emit
        # intermediate updates — interop, not progress streaming). Make it observable.
        logger.info("a2a executor finalized task %s (state=completed)", task_id)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = Task(
            id=getattr(context, "task_id", "") or uuid.uuid4().hex,
            status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
        )
        result = event_queue.enqueue_event(task)
        if inspect.isawaitable(result):
            await result


def _dispatch_to_host_sync(text: str) -> str:
    alloc = httpx.post(
        f"{HOST_SERVICE_URL}/chat/allocate-internal",
        headers=_host_headers(),
        json={"range_name": "a2a"},
        timeout=5,
    )
    alloc.raise_for_status()
    chat_id = int(alloc.json()["chat_id"])
    injected = httpx.post(
        f"{HOST_SERVICE_URL}/chat/inject",
        headers=_host_headers(),
        json={
            "text": text,
            "chat_id": chat_id,
            "source": "a2a",
            "sender_label": "A2A",
            "wait_for_response": True,
            "timeout_sec": A2A_RESPONSE_TIMEOUT_SEC,
            "transport": {
                "kind": "a2a",
                "conversation_id": str(chat_id),
                "sender_label": "A2A",
            },
        },
        timeout=A2A_RESPONSE_TIMEOUT_SEC + 10,
    )
    injected.raise_for_status()
    return str(injected.json().get("response") or "")


async def _dispatch_to_host(text: str) -> str:
    if _SLASH_COMMAND_RE.match(text or ""):
        raise ValueError("slash commands are reserved for direct owner input")
    try:
        semaphore = _get_semaphore()
        await asyncio.wait_for(semaphore.acquire(), timeout=0.1)
    except asyncio.TimeoutError as exc:
        raise RuntimeError("A2A server is busy; retry later") from exc
    try:
        return await asyncio.to_thread(_dispatch_to_host_sync, text)
    finally:
        semaphore.release()


def _build_app() -> Starlette:
    if not _is_loopback(A2A_HOST) and not A2A_SERVER_PASSWORD:
        raise RuntimeError("Refusing non-loopback A2A bind without A2A_SERVER_PASSWORD")
    if _A2A_SDK_AVAILABLE:
        card = _sdk_agent_card()
        handler = LegacyRequestHandler(
            agent_executor=OuroborosExecutor(),
            task_store=InMemoryTaskStore(),
            agent_card=card,
        )
        logger.info("a2a daemon using SDK agent-card routes")
        # A2: serve the v0.3-complete DICT card at BOTH well-known paths, registered BEFORE
        # the SDK helper so Starlette first-match makes it authoritative. The SDK AgentCard
        # object deliberately omits the v0.3 transport fields (protocolVersion /
        # preferredTransport / additionalInterfaces — kept out for SDK-version safety), so it
        # must NOT own the v0.3 path; the dict card carries them. The SDK's own card route is
        # then a harmless shadow, and the JSON-RPC handler still holds the card object.
        routes = [
            Route("/health", health, methods=["GET"]),
            Route("/.well-known/agent.json", agent_card, methods=["GET"]),
            Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, "/", enable_v0_3_compat=True),
        ]
        return Starlette(routes=routes, middleware=[Middleware(_A2AAuthMiddleware)])
    logger.info("a2a daemon using fallback (no SDK) agent-card routes")
    return Starlette(
        routes=[
            Route("/.well-known/agent.json", agent_card, methods=["GET"]),
            Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Route("/", jsonrpc, methods=["POST"]),
        ],
        middleware=[Middleware(_A2AAuthMiddleware)],
    )


app = _build_app()


if __name__ == "__main__":
    uvicorn.run(app, host=A2A_HOST, port=A2A_PORT, log_level="warning")
