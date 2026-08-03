from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import pathlib
import inspect
import ipaddress
import re
import threading
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
    from a2a.server.tasks.task_updater import TaskUpdater
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
A2A_CARD_VERSION = "1.3.0"


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
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        # A malformed or unreadable settings file must not be indistinguishable
        # from "never configured": defaults still apply, but say why.
        logger.warning("a2a settings: %s is unreadable (%s); using defaults", path, exc)
        return {}
    if not isinstance(document, dict):
        logger.warning(
            "a2a settings: %s is valid JSON but not an object (%s); using defaults",
            path,
            type(document).__name__,
        )
        return {}
    return document


_SETTINGS = _load_settings()


def _setting_int(name: str, default: int, *, minimum: int = 1, maximum: int = 600) -> int:
    """Bounded int setting.

    Defined here, ABOVE its first use: a bare int() conversion at module scope
    raised on one malformed owner value before the `app` object existed, which
    crash-looped the host-supervised companion with no server left to report why.
    An unparseable or out-of-range value is clamped and logged instead; the typed
    rejection belongs at the WRITE boundary (plugin.py's settings route), where an
    operator can see which field was refused.
    """
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        raw = _SETTINGS.get(name)
    try:
        value = int(raw if raw is not None and str(raw).strip() != "" else default)
    except (TypeError, ValueError):
        logger.warning(
            "a2a settings: %s=%r is not an integer; using default %d", name, raw, default
        )
        value = default
    bounded = max(minimum, min(maximum, value))
    if bounded != value:
        logger.warning(
            "a2a settings: %s=%d is outside %d..%d; clamped to %d",
            name, value, minimum, maximum, bounded,
        )
    return bounded


A2A_HOST = os.environ.get("A2A_HOST") or str(_SETTINGS.get("A2A_HOST") or "127.0.0.1")
# A port is 1..65535. Reaching uvicorn with an out-of-range value prevented the
# companion from starting at all.
A2A_PORT = _setting_int("A2A_PORT", 18800, minimum=1, maximum=65535)

# Read-only gateway log endpoints (documented for headless clients) provide the
# progress notes streamed as working-status updates and the durable completion
# fallback for host waits that outlive the response subscription. Loopback only —
# same discipline as HOST_SERVICE_URL (no token is ever sent to the gateway).
A2A_GATEWAY_URL = (
    os.environ.get("A2A_GATEWAY_URL")
    or str(_SETTINGS.get("A2A_GATEWAY_URL") or "")
    or "http://127.0.0.1:8765"
).rstrip("/")
if not _is_loopback(_host_service_hostname(A2A_GATEWAY_URL)):
    raise RuntimeError("A2A_GATEWAY_URL must be a loopback address")

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
# transient empty fetch must never regress it back to a tool-less card
# (self-healing stability across per-request rebuilds and the startup bake).
_LAST_GOOD_TOOLS: List[Dict[str, Any]] = []

# In-request fetch is SHORT: a discovery GET must answer in seconds, not ride the
# full warmup ladder (a cold host once held /.well-known/agent-card.json for ~57s).
# The full ladder lives in the background refresher started with the app.
_TOOLS_FETCH_ATTEMPTS_REQUEST = 2

# Curated capability categories for the card's skill list. These are STABLE core
# capabilities of every Ouroboros install, so the card stays honest and USEFUL for
# integrators even while the host tool registry is warming up or unavailable —
# the two prior regressions were a contentless "General" stub and an identity-only
# persona entry, both useless to a consumer that reads capabilities off the card.
_CORE_CAPABILITY_SKILLS: List[Dict[str, Any]] = [
    {
        "id": "code-and-files",
        "name": "Code and files",
        "description": (
            "Read, search, edit and run code; structural code queries (definitions, "
            "callers, impact), git operations, cherry-picks and code review."
        ),
        "tags": ["code", "files", "git"],
    },
    {
        "id": "web-and-media",
        "name": "Web and media",
        "description": (
            "Web search, open pages in a real browser, screenshots, PDFs, images, "
            "video frame extraction and YouTube subtitles."
        ),
        "tags": ["web", "media", "research"],
    },
    {
        "id": "long-running-tasks",
        "name": "Long-running tasks and projects",
        "description": (
            "Background tasks with live progress across multiple rounds, and named "
            "projects with a durable journal and memory."
        ),
        "tags": ["tasks", "projects", "background"],
    },
    {
        "id": "self-modification",
        "name": "Self-modification",
        "description": (
            "Edits its own code, prompts and configuration, landing changes through a "
            "mandatory multi-model review and restart cycle."
        ),
        "tags": ["self-modification", "evolution"],
    },
    {
        "id": "skills-extensions",
        "name": "Skills (extensions)",
        "description": (
            "Installable skills extend the agent (A2A bridge, desktop observation and "
            "control, and more); per-tool entries below reflect what is enabled live."
        ),
        "tags": ["skills", "extensions"],
    },
]

# Capability-first top-level description. The identity file's own first line is
# appended as flavor — personality belongs on the card, but never INSTEAD of the
# capability summary an integrator consumes.
_CAPABILITY_LEAD = (
    "General-purpose autonomous AI agent: code and files (including git and code "
    "review), web research and media, long-running background tasks and projects, "
    "reviewed self-modification, and installable skills."
)


A2A_MAX_CONCURRENT = _setting_int("A2A_MAX_CONCURRENT", 5, minimum=1, maximum=20)
# Host-side /chat/inject clamps its wait at 1800s; stay under it (plus the httpx
# margin) so the daemon, not a socket, always decides what a wait expiry means.
A2A_RESPONSE_TIMEOUT_SEC = _setting_int("A2A_RESPONSE_TIMEOUT_SEC", 600, minimum=1, maximum=1740)
# The old hardcoded 5s allocate timeout was the source of spurious "timed out"
# stream errors on a busy host — allocation is cheap but the host polls at 1s.
A2A_ALLOCATE_TIMEOUT_SEC = _setting_int("A2A_ALLOCATE_TIMEOUT_SEC", 30, minimum=1, maximum=120)
# Total lifetime of one inbound message, including the chat-log completion
# fallback after the inject wait expires. The host task itself is never cancelled.
A2A_STREAM_DEADLINE_SEC = _setting_int("A2A_STREAM_DEADLINE_SEC", 3600, minimum=60, maximum=21600)
# Cross-validate the two independently-bounded waits. The deadline is only checked
# AFTER _inject_sync returns, so a response timeout longer than the total deadline
# (e.g. 1740s against a 60s deadline) blocks for the full socket wait and then
# gives up immediately — defeating the deadline it was supposed to respect.
if A2A_RESPONSE_TIMEOUT_SEC + 15 > A2A_STREAM_DEADLINE_SEC:
    _clamped_response_timeout = max(1, A2A_STREAM_DEADLINE_SEC - 15)
    logger.warning(
        "a2a settings: A2A_RESPONSE_TIMEOUT_SEC=%d exceeds A2A_STREAM_DEADLINE_SEC=%d "
        "(minus the 15s socket margin); clamping the response wait to %d",
        A2A_RESPONSE_TIMEOUT_SEC,
        A2A_STREAM_DEADLINE_SEC,
        _clamped_response_timeout,
    )
    A2A_RESPONSE_TIMEOUT_SEC = _clamped_response_timeout

A2A_PROGRESS_POLL_SEC = _setting_int("A2A_PROGRESS_POLL_SEC", 3, minimum=1, maximum=60)
A2A_HEARTBEAT_SEC = _setting_int("A2A_HEARTBEAT_SEC", 25, minimum=5, maximum=300)
# "1" (default): forward the host agent's own progress notes as working-status
# updates; "0": heartbeats only (no gateway progress polling).
A2A_PROGRESS_ENRICH = str(os.environ.get("A2A_PROGRESS_ENRICH") or _SETTINGS.get("A2A_PROGRESS_ENRICH") or "1").strip() != "0"
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


class _TaskStateCorrupt(RuntimeError):
    """A task record exists but is unreadable or is not a JSON object."""


def _task_path(task_id: str) -> pathlib.Path:
    """Map a task id to a state file INJECTIVELY.

    Stripping every character outside [A-Za-z0-9_-] is a lossy mapping: distinct
    ids such as "job/a" and "joba" collapsed onto one filename, so one peer's task
    could overwrite or return another's record. The sha256 of the COMPLETE id is
    the identity; the sanitized prefix is retained only to keep the directory
    human-readable. An empty id now maps deterministically as well — the previous
    uuid fallback meant _load_task("") could never find what _save_task wrote.
    """
    raw = str(task_id or "")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    prefix = "".join(ch for ch in raw if ch.isalnum() or ch in ("-", "_"))[:48]
    return _tasks_dir() / (f"{prefix}-{digest}.json" if prefix else f"{digest}.json")


def _save_task(task: Dict[str, Any]) -> None:
    # The untouched original id stays inside the record; the filename is a digest.
    _task_path(str(task["id"])).write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_task(task_id: str) -> Dict[str, Any] | None:
    path = _task_path(task_id)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        # A present-but-unreadable record is NOT "task not found": report it as an
        # actionable task-state failure instead of letting a bare decode error
        # escape as an opaque server error.
        raise _TaskStateCorrupt(f"task state for {task_id!r} is unreadable: {exc}") from exc
    if not isinstance(record, dict):
        raise _TaskStateCorrupt(f"task state for {task_id!r} is not a JSON object")
    return record


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


def _fetch_tool_schemas(
    attempts_override: "int | None" = None,
    *,
    stop_event: "threading.Event | None" = None,
) -> List[Dict[str, Any]]:
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

    ``stop_event`` (passed only by the background refresher) aborts the ladder and
    its backoff waits as soon as companion shutdown is requested.
    """
    global _LAST_GOOD_TOOLS
    attempts = _TOOLS_FETCH_ATTEMPTS if attempts_override is None else max(1, int(attempts_override))
    last_error = "no attempt made"
    for attempt in range(attempts):
        if stop_event is not None and stop_event.is_set():
            last_error = "shutdown requested"
            break
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
        if attempt + 1 < attempts:
            delay = _TOOLS_FETCH_BACKOFF_SEC * (attempt + 1)
            # Shutdown must interrupt the ladder, not merely the interval between
            # runs: the full backoff chain sleeps ~14s, longer than
            # _REFRESHER_JOIN_TIMEOUT_SEC, so a plain sleep here made a graceful
            # stop time out its join and leak the thread with a warning.
            if stop_event is not None:
                if stop_event.wait(delay):
                    last_error = "shutdown requested"
                    break
            else:
                time.sleep(delay)
    if _LAST_GOOD_TOOLS:
        logger.warning(
            "a2a agent-card: /tools/schemas empty after %d attempts (%s); "
            "serving last known-good tool list (%d tools)",
            attempts,
            last_error,
            len(_LAST_GOOD_TOOLS),
        )
        return _LAST_GOOD_TOOLS
    logger.warning(
        "a2a agent-card: /tools/schemas unavailable after %d attempts (%s); "
        "card will advertise the curated capability categories only",
        attempts,
        last_error,
    )
    return []


def _resolve_identity() -> Dict[str, str]:
    """Resolve the card's top-level name/description.

    Operator-set A2A_AGENT_NAME / A2A_AGENT_DESCRIPTION always win. Otherwise the
    NAME is the stable product name "Ouroboros" — the /identity name is the
    identity document's first heading, which in practice is a section title like
    "Who I Am", not an agent name — and the DESCRIPTION leads with the curated
    capability summary, with the live /identity first line appended as flavor
    (personality on the card, never INSTEAD of capabilities: an integrator reads
    what the agent can do off this field)."""
    identity = _fetch_identity()
    name = _A2A_AGENT_NAME_EXPLICIT or "Ouroboros"
    if _A2A_AGENT_DESCRIPTION_EXPLICIT:
        description = _A2A_AGENT_DESCRIPTION_EXPLICIT
    else:
        flavor = str(identity.get("description") or "").strip()
        description = _CAPABILITY_LEAD + ((" " + flavor) if flavor else "")
    return {"name": name, "description": description}


def _agent_card() -> Dict[str, Any]:
    ident = _resolve_identity()
    name = ident["name"]
    description = ident["description"]

    # Curated capability categories come FIRST and are ALWAYS present: the card must
    # describe what this agent can actually do even while the host tool registry is
    # warming up or unavailable (the two prior regressions served a contentless
    # "General" stub and then an identity-persona-only entry — both useless to an
    # integrator that consumes capabilities off the card).
    skills = [dict(entry) for entry in _CORE_CAPABILITY_SKILLS]

    # Live per-tool entries follow, when available. The in-request fetch is SHORT
    # (the background refresher owns the long warmup ladder and keeps the last-good
    # list warm), so a discovery GET answers in seconds even on a cold host.
    seen_ids = {entry["id"] for entry in skills}
    tools = _LAST_GOOD_TOOLS or _fetch_tool_schemas(attempts_override=_TOOLS_FETCH_ATTEMPTS_REQUEST)
    for schema in tools:
        func = schema.get("function", schema) if isinstance(schema, dict) else {}
        tool_name = str(func.get("name") or "")
        if tool_name and tool_name not in seen_ids:
            seen_ids.add(tool_name)
            skills.append({
                "id": tool_name,
                "name": tool_name,
                "description": str(func.get("description") or "")[:200],
                "tags": [tool_name.split("_", 1)[0] if "_" in tool_name else "tool"],
            })

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
    # The SDK AgentCard requires the card URL, and v0.3-aware SDKs also carry the
    # transport identity fields the dict card already publishes. Omitting them
    # raised during _build_app() and killed the supervised companion before it
    # served a single request. Values come from the dict card so the two cards
    # cannot drift; unknown kwargs are filtered so an older SDK still constructs.
    kwargs = dict(
        name=card["name"],
        description=card["description"],
        version=card["version"],
        url=card["url"],
        protocol_version=card["protocolVersion"],
        preferred_transport=card["preferredTransport"],
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
    # The filter must cover BOTH card families an SDK can export: pydantic models
    # (model_fields) and protobuf messages, whose fields live on
    # DESCRIPTOR.fields_by_name and which raise ValueError — not TypeError — for an
    # unknown kwarg. a2a-sdk 1.1.2 exports the PROTO AgentCard, which has no `url`
    # field: passing it raised at import time, so the host-supervised companion
    # died before serving a single request and just restart-looped.
    fields = getattr(AgentCard, "model_fields", None) or getattr(AgentCard, "__fields__", None)
    if not fields:
        descriptor = getattr(AgentCard, "DESCRIPTOR", None)
        if descriptor is not None:
            fields = dict.fromkeys(descriptor.fields_by_name)
    if fields:
        kwargs = {key: value for key, value in kwargs.items() if key in fields}
    try:
        return AgentCard(**kwargs)
    except (ValueError, TypeError) as exc:
        # An SDK-shape mismatch must never stop the daemon from booting: the dict
        # card served at /.well-known/ is the authoritative one either way.
        logger.warning("a2a agent-card: SDK card rejected kwargs (%s); retrying minimal card", exc)
        minimal = {
            key: kwargs[key]
            for key in ("name", "description", "version", "capabilities", "skills")
            if key in kwargs
        }
        return AgentCard(**minimal)


async def agent_card(_request: Request) -> JSONResponse:
    # _agent_card() does BLOCKING httpx.get calls plus time.sleep backoff on the
    # cold path (_LAST_GOOD_TOOLS empty / host unreachable). uvicorn runs this app
    # on one event loop, so calling it inline stalled EVERY concurrent request —
    # including in-flight message/send dispatch — for up to ~10s during exactly
    # the cold-start window the retry ladder exists to survive. _agent_card stays
    # synchronous because _sdk_agent_card() calls it once at startup, off the
    # request path, where blocking is correct.
    return JSONResponse(await asyncio.to_thread(_agent_card))


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "a2a"})


def _extract_text(params: Dict[str, Any]) -> str:
    # Independently defensive: safe even if reached from a path that has not
    # already proven the nested shapes.
    message = params.get("message") if isinstance(params, dict) else None
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    texts = []
    for part in parts:
        if isinstance(part, dict):
            texts.append(str(part.get("text") or ""))
    return "\n".join(text for text in texts if text).strip()


def _jsonrpc_error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


async def jsonrpc(request: Request) -> JSONResponse:
    # Untrusted bodies are validated at EVERY nesting level this handler touches
    # before any .get access: a valid JSON list or scalar previously raised an
    # uncaught AttributeError that surfaced as an opaque HTTP 500 rather than a
    # JSON-RPC invalid-request response.
    try:
        payload = await request.json()
    except ValueError as exc:
        return _jsonrpc_error(None, -32700, f"parse error: {exc}")
    if not isinstance(payload, dict):
        return _jsonrpc_error(None, -32600, "invalid request: body must be a JSON object")
    request_id = payload.get("id") or uuid.uuid4().hex
    method = str(payload.get("method") or "")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return _jsonrpc_error(request_id, -32600, "invalid request: params must be a JSON object")
    message = params.get("message")
    if message is not None and not isinstance(message, dict):
        return _jsonrpc_error(request_id, -32600, "invalid request: params.message must be a JSON object")
    if method == "tasks/get":
        try:
            task = _load_task(str(params.get("id") or ""))
        except _TaskStateCorrupt as exc:
            # Distinct from "task not found": the record exists but cannot be read,
            # so the caller gets an actionable internal error, not a bare traceback.
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}})
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


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class OuroborosExecutor(AgentExecutor if _A2A_SDK_AVAILABLE else object):
    """Streams the host agent's work as spec-shaped A2A events.

    Lifecycle per message: submitted -> working (start note) -> working updates
    carrying the host agent's own progress notes (plus heartbeats while quiet)
    -> one final artifact + completed. Failures become a terminal failed status
    with a human-readable message instead of a JSON-RPC -32603 stream error."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        parts = getattr(getattr(context, "message", None), "parts", []) or []
        text = "\n".join(str(getattr(part, "text", "") or "") for part in parts if getattr(part, "text", ""))
        # Reuse the SDK-provided ids: the request handler rejects events whose
        # task_id differs from RequestContext.task_id when the client named one.
        task_id = getattr(context, "task_id", "") or uuid.uuid4().hex
        context_id = getattr(context, "context_id", "") or task_id
        updater = TaskUpdater(event_queue, task_id, context_id)

        async def emit_working(note: str) -> None:
            await _maybe_await(
                updater.update_status(
                    TaskState.TASK_STATE_WORKING,
                    message=updater.new_agent_message([Part(text=note)]),
                )
            )

        if not getattr(context, "current_task", None):
            await _maybe_await(updater.submit())
        await _maybe_await(updater.start_work())
        try:
            response_text = await self._run_dispatch(text, emit_working)
        except Exception as exc:
            # Terminal failed status keeps the stream spec-shaped; -32603 is now
            # reserved for genuinely unexpected crashes above this handler.
            logger.warning("a2a executor task %s failed: %s", task_id, exc)
            await _maybe_await(
                updater.failed(message=updater.new_agent_message([Part(text=f"dispatch failed: {exc}")]))
            )
            return
        await _maybe_await(
            updater.add_artifact([Part(text=response_text)], last_chunk=True)
        )
        await _maybe_await(updater.complete())
        logger.info("a2a executor finalized task %s (state=completed)", task_id)

    async def _run_dispatch(self, text: str, emit_working) -> str:
        _guard_inbound_text(text)
        try:
            semaphore = _get_semaphore()
            await asyncio.wait_for(semaphore.acquire(), timeout=0.1)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("A2A server is busy; retry later") from exc
        try:
            chat_id = await asyncio.to_thread(_allocate_chat_id_sync)
            started = time.monotonic()
            deadline = started + A2A_STREAM_DEADLINE_SEC
            await emit_working("Message accepted; dispatching to the host agent")
            dispatch = asyncio.ensure_future(
                asyncio.to_thread(_dispatch_after_allocate_sync, chat_id, text, deadline)
            )
            seen_progress: set = set()
            last_emit = time.monotonic()
            while True:
                done, _pending = await asyncio.wait({dispatch}, timeout=A2A_PROGRESS_POLL_SEC)
                if done:
                    break
                notes: List[str] = []
                if A2A_PROGRESS_ENRICH:
                    notes = await asyncio.to_thread(_fetch_progress_notes_sync, chat_id, seen_progress)
                for note in notes:
                    await emit_working(note)
                    last_emit = time.monotonic()
                if time.monotonic() - last_emit >= A2A_HEARTBEAT_SEC:
                    await emit_working(f"Still working ({int(time.monotonic() - started)}s elapsed)")
                    last_emit = time.monotonic()
            return await dispatch
        finally:
            semaphore.release()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = Task(
            id=getattr(context, "task_id", "") or uuid.uuid4().hex,
            status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
        )
        result = event_queue.enqueue_event(task)
        if inspect.isawaitable(result):
            await result


class _HostWaitExpired(RuntimeError):
    """The host accepted the message but the response wait closed before the
    agent answered. The underlying task keeps running host-side; the durable
    completion fallback below can still deliver its answer."""


def _guard_inbound_text(text: str) -> None:
    if _SLASH_COMMAND_RE.match(text or ""):
        raise ValueError("slash commands are reserved for direct owner input")


def _allocate_chat_id_sync() -> int:
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            alloc = httpx.post(
                f"{HOST_SERVICE_URL}/chat/allocate-internal",
                headers=_host_headers(),
                json={"range_name": "a2a"},
                timeout=A2A_ALLOCATE_TIMEOUT_SEC,
            )
            alloc.raise_for_status()
            return int(alloc.json()["chat_id"])
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(1.0)
    raise RuntimeError(f"host chat-id allocation failed: {last_exc}") from last_exc


def _inject_sync(chat_id: int, text: str) -> str:
    """One /chat/inject with wait_for_response. Raises _HostWaitExpired when the
    wait window closed while the task is still running (host 504, or a socket
    timeout on our side); other HTTP failures propagate as hard errors."""
    try:
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
            timeout=A2A_RESPONSE_TIMEOUT_SEC + 15,
        )
    except httpx.TimeoutException as exc:
        raise _HostWaitExpired(
            f"host response socket timed out after ~{A2A_RESPONSE_TIMEOUT_SEC}s ({exc})"
        ) from exc
    if injected.status_code == 504:
        raise _HostWaitExpired(f"host response wait expired after {A2A_RESPONSE_TIMEOUT_SEC}s")
    injected.raise_for_status()
    try:
        payload = injected.json()
    except ValueError as exc:
        raise RuntimeError(f"host chat injection returned a non-JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("host chat injection returned a non-object body")
    # HTTP 200 is not by itself success: the host reports a refusal in the body as
    # {"ok": false, "error": ...}. Reading only "response" turned that into an
    # EMPTY completed answer, hiding the real host-side error from the A2A caller.
    if not payload.get("ok", True):
        raise RuntimeError(str(payload.get("error") or "host rejected chat injection"))
    return str(payload.get("response") or "")


def _gateway_log_entries_sync(name: str, limit: int) -> List[Dict[str, Any]]:
    resp = httpx.get(
        f"{A2A_GATEWAY_URL}/api/logs/{name}",
        params={"limit": limit},
        timeout=10,
    )
    resp.raise_for_status()
    entries = resp.json().get("entries")
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def _fetch_final_answer_sync(chat_id: int) -> "str | None":
    """Durable completion fallback: the final answer of an injected turn is always
    appended to the chat log under our single-use negative chat_id, even after the
    response subscription is gone."""
    try:
        entries = _gateway_log_entries_sync("chat", 300)
    except Exception:
        return None
    for entry in reversed(entries):
        if entry.get("chat_id") == chat_id and str(entry.get("direction")) == "out":
            return str(entry.get("text") or "")
    return None


def _fetch_progress_notes_sync(chat_id: int, seen: set) -> List[str]:
    """New progress-log notes for our chat_id (the same narration the web UI
    shows), deduplicated across polls via the caller-owned ``seen`` set."""
    try:
        entries = _gateway_log_entries_sync("progress", 200)
    except Exception:
        return []
    notes: List[str] = []
    for entry in entries:
        if entry.get("chat_id") != chat_id:
            continue
        raw = str(entry.get("text") or entry.get("content") or "").strip()
        if not raw:
            continue
        key = (str(entry.get("ts") or ""), str(entry.get("_line") or ""), raw[:80])
        if key in seen:
            continue
        seen.add(key)
        note = raw.lstrip("💬").strip()
        notes.append(note[:400] + ("…" if len(note) > 400 else ""))
    return notes


def _wait_final_after_expiry_sync(chat_id: int, deadline_monotonic: float) -> str:
    while time.monotonic() < deadline_monotonic:
        answer = _fetch_final_answer_sync(chat_id)
        if answer is not None:
            return answer
        time.sleep(max(1.0, float(A2A_PROGRESS_POLL_SEC)))
    raise RuntimeError(
        f"no response within A2A_STREAM_DEADLINE_SEC={A2A_STREAM_DEADLINE_SEC}s; "
        "the host task may still be running"
    )


def _dispatch_after_allocate_sync(chat_id: int, text: str, deadline_monotonic: float) -> str:
    try:
        return _inject_sync(chat_id, text)
    except _HostWaitExpired as exc:
        logger.warning("a2a dispatch: %s; falling back to durable chat-log polling", exc)
        return _wait_final_after_expiry_sync(chat_id, deadline_monotonic)


def _dispatch_to_host_sync(text: str) -> str:
    chat_id = _allocate_chat_id_sync()
    return _dispatch_after_allocate_sync(chat_id, text, time.monotonic() + A2A_STREAM_DEADLINE_SEC)


async def _dispatch_to_host(text: str) -> str:
    """Non-streaming dispatch used by the no-SDK fallback message/send route."""
    _guard_inbound_text(text)
    try:
        semaphore = _get_semaphore()
        await asyncio.wait_for(semaphore.acquire(), timeout=0.1)
    except asyncio.TimeoutError as exc:
        raise RuntimeError("A2A server is busy; retry later") from exc
    try:
        return await asyncio.to_thread(_dispatch_to_host_sync, text)
    finally:
        semaphore.release()


_REFRESHER_STARTED = False
_REFRESHER_STOP = threading.Event()
_REFRESHER_THREAD: "threading.Thread | None" = None


def _stop_tools_refresher() -> None:
    """Terminate the refresher thread and allow a later restart.

    Invoked from the app lifespan shutdown path, so an ordinary companion
    stop (host unload/disable, uvicorn SIGTERM) ends the thread through a real
    code path instead of relying on the interpreter tearing a daemon thread
    down. Panic still hard-kills the process group; this is the graceful path
    that previously did not exist at all. Idempotent, and safe to call when the
    thread was never started."""
    global _REFRESHER_STARTED, _REFRESHER_THREAD
    _REFRESHER_STOP.set()
    thread = _REFRESHER_THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=_REFRESHER_JOIN_TIMEOUT_SEC)
        if thread.is_alive():
            logger.warning(
                "a2a tools refresher did not exit within %ss; leaving it as a daemon thread",
                _REFRESHER_JOIN_TIMEOUT_SEC,
            )
    _REFRESHER_THREAD = None
    _REFRESHER_STARTED = False


def _start_tools_refresher() -> None:
    """Keep ``_LAST_GOOD_TOOLS`` warm from a daemon thread.

    The host chat-agent can take minutes to build on a fresh install, far longer
    than any latency a discovery GET should pay. This thread owns the LONG warmup:
    it runs the full retry ladder until the first non-empty tool list lands, then
    re-polls occasionally so an enabled/disabled skill is reflected without a
    companion restart. Card requests never block on it — they serve the curated
    categories plus whatever this thread has already warmed.

    ``A2A_TOOLS_REFRESHER=0`` disables the thread (tests and embedders that build
    the app repeatedly must not accumulate pollers); repeated calls are no-ops.
    ``_stop_tools_refresher`` ends it on companion shutdown."""
    global _REFRESHER_STARTED, _REFRESHER_THREAD
    if _REFRESHER_STARTED or os.environ.get("A2A_TOOLS_REFRESHER", "1") == "0":
        return
    _REFRESHER_STARTED = True
    _REFRESHER_STOP.clear()

    def _loop() -> None:
        while not _REFRESHER_STOP.is_set():
            try:
                _fetch_tool_schemas(stop_event=_REFRESHER_STOP)
            except Exception as exc:
                # A poll error must not end the refresher permanently. The thread
                # body has no other recovery path, so a single unexpected
                # exception (the stop_event signature mismatch was exactly this)
                # killed background warming for the whole companion lifetime
                # after one stderr traceback nobody reads.
                logger.warning("a2a tools refresher poll failed: %s", exc)
            if _REFRESHER_STOP.is_set():
                break
            interval = _TOOLS_REFRESH_INTERVAL_SEC if _LAST_GOOD_TOOLS else _TOOLS_RETRY_INTERVAL_SEC
            if _REFRESHER_STOP.wait(interval):
                break

    _REFRESHER_THREAD = threading.Thread(target=_loop, name="a2a-tools-refresher", daemon=True)
    _REFRESHER_THREAD.start()


_TOOLS_REFRESH_INTERVAL_SEC = 300
_TOOLS_RETRY_INTERVAL_SEC = 15
_REFRESHER_JOIN_TIMEOUT_SEC = 10


@contextlib.asynccontextmanager
async def _app_lifespan(_app: Starlette):
    """Modern-starlette shutdown hook (the on_shutdown kwarg no longer exists):
    end the tools refresher through a real code path on companion stop."""
    try:
        yield
    finally:
        _stop_tools_refresher()


def _build_app() -> Starlette:
    if not _is_loopback(A2A_HOST) and not A2A_SERVER_PASSWORD:
        raise RuntimeError("Refusing non-loopback A2A bind without A2A_SERVER_PASSWORD")
    _start_tools_refresher()
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
        return Starlette(
            routes=routes,
            middleware=[Middleware(_A2AAuthMiddleware)],
            lifespan=_app_lifespan,
        )
    logger.info("a2a daemon using fallback (no SDK) agent-card routes")
    return Starlette(
        routes=[
            Route("/.well-known/agent.json", agent_card, methods=["GET"]),
            Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            Route("/", jsonrpc, methods=["POST"]),
        ],
        middleware=[Middleware(_A2AAuthMiddleware)],
        lifespan=_app_lifespan,
    )


app = _build_app()


if __name__ == "__main__":
    uvicorn.run(app, host=A2A_HOST, port=A2A_PORT, log_level="warning")
