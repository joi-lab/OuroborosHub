from __future__ import annotations

import json
import os
import pathlib
import inspect
import uuid
import base64
from typing import Any, Dict

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


STATE_DIR = pathlib.Path(os.environ.get("OUROBOROS_SKILL_STATE_DIR") or ".")
HOST_SERVICE_URL = os.environ.get("HOST_SERVICE_URL", "http://127.0.0.1:8767").rstrip("/")
HOST_SERVICE_TOKEN = os.environ.get("HOST_SERVICE_TOKEN", "")


def _load_settings() -> Dict[str, Any]:
    path = STATE_DIR / "settings.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


_SETTINGS = _load_settings()
A2A_HOST = os.environ.get("A2A_HOST") or str(_SETTINGS.get("A2A_HOST") or "127.0.0.1")
A2A_PORT = int(os.environ.get("A2A_PORT") or _SETTINGS.get("A2A_PORT") or "18800")
A2A_AGENT_NAME = os.environ.get("A2A_AGENT_NAME") or str(_SETTINGS.get("A2A_AGENT_NAME") or "Ouroboros")
A2A_AGENT_DESCRIPTION = os.environ.get("A2A_AGENT_DESCRIPTION") or str(_SETTINGS.get("A2A_AGENT_DESCRIPTION") or "Ouroboros A2A peer")
A2A_SERVER_PASSWORD = (os.environ.get("A2A_SERVER_PASSWORD") or str(_SETTINGS.get("A2A_SERVER_PASSWORD") or "")).strip()


def _host_headers() -> Dict[str, str]:
    return {"X-Skill-Token": HOST_SERVICE_TOKEN}


def _is_loopback(host: str) -> bool:
    return str(host or "").strip() in {"127.0.0.1", "localhost", "::1"}


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


def _agent_card() -> Dict[str, Any]:
    tools = []
    try:
        response = httpx.get(f"{HOST_SERVICE_URL}/tools/schemas", headers=_host_headers(), timeout=5)
        if response.status_code == 200:
            tools = response.json().get("tools") or []
    except Exception:
        tools = []
    skills = []
    for schema in tools:
        func = schema.get("function", schema) if isinstance(schema, dict) else {}
        name = str(func.get("name") or "")
        if name:
            skills.append({
                "id": name,
                "name": name,
                "description": str(func.get("description") or "")[:200],
                "tags": [name.split("_", 1)[0] if "_" in name else "tool"],
            })
    return {
        "name": A2A_AGENT_NAME,
        "description": A2A_AGENT_DESCRIPTION,
        "url": f"http://{A2A_HOST}:{A2A_PORT}/",
        "version": "1.0.0",
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": skills or [{"id": "general", "name": "General", "description": A2A_AGENT_DESCRIPTION, "tags": ["general"]}],
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
                "timeout_sec": 1800,
            },
            timeout=1810,
        )
        injected.raise_for_status()
        response_text = injected.json().get("response") or ""
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

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = Task(
            id=getattr(context, "task_id", "") or uuid.uuid4().hex,
            status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
        )
        result = event_queue.enqueue_event(task)
        if inspect.isawaitable(result):
            await result


async def _dispatch_to_host(text: str) -> str:
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
            "timeout_sec": 1800,
        },
        timeout=1810,
    )
    injected.raise_for_status()
    return str(injected.json().get("response") or "")


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
        routes = [
            Route("/health", health, methods=["GET"]),
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, "/", enable_v0_3_compat=True),
        ]
        return Starlette(routes=routes, middleware=[Middleware(_A2AAuthMiddleware)])
    return Starlette(
        routes=[
            Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Route("/", jsonrpc, methods=["POST"]),
        ],
        middleware=[Middleware(_A2AAuthMiddleware)],
    )


app = _build_app()


if __name__ == "__main__":
    uvicorn.run(app, host=A2A_HOST, port=A2A_PORT, log_level="warning")
