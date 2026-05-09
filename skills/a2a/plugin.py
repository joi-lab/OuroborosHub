from __future__ import annotations

import json
import os
import pathlib

from .lib import client
from starlette.responses import JSONResponse


def _state_file(api, name: str) -> pathlib.Path:
    return pathlib.Path(api.get_state_dir()) / name


def _load_settings(api) -> dict:
    path = _state_file(api, "settings.json")
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _make_settings_save(api):
    async def _settings_save(request):
        data = await request.json()
        path = pathlib.Path(api.get_state_dir()) / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        allowed = {
            "A2A_HOST",
            "A2A_PORT",
            "A2A_AGENT_NAME",
            "A2A_AGENT_DESCRIPTION",
            "A2A_MAX_CONCURRENT",
            "A2A_RESPONSE_TIMEOUT_SEC",
            "A2A_TASK_TTL_HOURS",
            "A2A_SERVER_PASSWORD",
        }
        payload = {key: data.get(key) for key in allowed if key in data}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return JSONResponse({"ok": True, "message": "A2A settings saved. Toggle the skill to restart the server."})
    return _settings_save


def register(api):
    api.register_tool(
        "discover",
        handler=lambda ctx, url="": client.discover(url),
        description="Discover another A2A-compatible agent by fetching its Agent Card.",
        schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    )
    api.register_tool(
        "send",
        handler=lambda ctx, url="", message="", task_id="", context_id="": client.send(
            url, message, task_id=task_id, context_id=context_id
        ),
        description="Send a message to another A2A-compatible agent.",
        schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "message": {"type": "string"},
                "task_id": {"type": "string"},
                "context_id": {"type": "string"},
            },
            "required": ["url", "message"],
        },
    )
    api.register_tool(
        "status",
        handler=lambda ctx, url="", task_id="": client.status(url, task_id),
        description="Check a remote A2A task status.",
        schema={
            "type": "object",
            "properties": {"url": {"type": "string"}, "task_id": {"type": "string"}},
            "required": ["url", "task_id"],
        },
    )
    api.register_route("settings/save", handler=_make_settings_save(api), methods=("POST",))
    api.register_settings_section(
        "a2a",
        title="A2A Server",
        schema={
            "components": [
                {
                    "type": "form",
                    "route": "settings/save",
                    "method": "POST",
                    "fields": [
                        {"name": "A2A_HOST", "label": "Host", "type": "text", "placeholder": "127.0.0.1"},
                        {"name": "A2A_PORT", "label": "Port", "type": "number", "placeholder": "18800"},
                        {"name": "A2A_AGENT_NAME", "label": "Agent name", "type": "text"},
                        {"name": "A2A_AGENT_DESCRIPTION", "label": "Description", "type": "textarea"},
                        {"name": "A2A_MAX_CONCURRENT", "label": "Max concurrent inbound requests", "type": "number", "placeholder": "5"},
                        {"name": "A2A_RESPONSE_TIMEOUT_SEC", "label": "Response timeout seconds", "type": "number", "placeholder": "600"},
                        {"name": "A2A_SERVER_PASSWORD", "label": "Server password for non-loopback binds", "type": "password"},
                    ],
                    "submit_label": "Save A2A settings",
                }
            ]
        },
    )
    info = api.get_runtime_info()
    api.get_skill_token()
    api.register_companion_process("a2a_server")
