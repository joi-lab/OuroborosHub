from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
from typing import Any, Dict

import httpx
from starlette.responses import JSONResponse

from .lib.telegram_api import TelegramClient

_SLASH_COMMAND_RE = re.compile(r"^\s*/[A-Za-z]")


def _state_file(api, name: str) -> pathlib.Path:
    return pathlib.Path(api.get_state_dir()) / name


def _load_settings(api) -> Dict[str, Any]:
    path = _state_file(api, "settings.json")
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def _setting_int(settings: Dict[str, Any], key: str, default: int, *, minimum: int = 1, maximum: int = 100) -> int:
    try:
        value = int(settings.get(key) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _rejects_owner_slash_command(text: str) -> bool:
    return bool(_SLASH_COMMAND_RE.match(str(text or "")))


def _make_settings_save(api):
    async def _settings_save(request):
        data = await request.json()
        allowed = {"TELEGRAM_CHAT_ID", "TELEGRAM_MAX_UPDATES_PER_POLL"}
        payload = {key: data.get(key) for key in allowed if key in data}
        path = _state_file(api, "settings.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return JSONResponse({"ok": True, "message": "Telegram settings saved. Toggle the skill to restart polling."})
    return _settings_save


def _host_headers(api) -> Dict[str, str]:
    return {"X-Skill-Token": api.get_skill_token().use_in_request()}


def _target_chat(settings: Dict[str, Any], event: Dict[str, Any]) -> int:
    configured = str(settings.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if configured:
        try:
            return int(configured)
        except ValueError:
            return 0
    transport = event.get("transport") if isinstance(event.get("transport"), dict) else {}
    try:
        return int(transport.get("conversation_id") or event.get("chat_id") or 0)
    except (TypeError, ValueError):
        return 0


async def _inject(api, payload: Dict[str, Any]) -> None:
    port = os.environ.get("OUROBOROS_HOST_SERVICE_PORT", "8767")
    async with httpx.AsyncClient(timeout=60) as client:
        await client.post(
            f"http://127.0.0.1:{port}/chat/inject",
            headers=_host_headers(api),
            json=payload,
        )


def _make_poller(api):
    async def poller() -> None:
        protected_settings = api.get_settings(["TELEGRAM_BOT_TOKEN"])
        local_settings = _load_settings(api)
        client = TelegramClient(protected_settings.get("TELEGRAM_BOT_TOKEN", ""))
        pinned_chat = str(local_settings.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        max_updates = _setting_int(local_settings, "TELEGRAM_MAX_UPDATES_PER_POLL", 20, minimum=1, maximum=100)
        offset = 0
        while True:
            updates = await client.get_updates(offset)
            for update in updates[:max_updates]:
                update_id = int(update.get("update_id") or 0)
                if update_id >= offset:
                    offset = update_id + 1
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                sender = message.get("from") or {}
                chat_id = int(chat.get("id") or 0)
                if pinned_chat and str(chat_id) != pinned_chat:
                    continue
                text = str(message.get("text") or message.get("caption") or "").strip()
                caption = str(message.get("caption") or "").strip()
                if _rejects_owner_slash_command(text) or _rejects_owner_slash_command(caption):
                    await client.send_message(chat_id, "Slash commands are reserved for direct Ouroboros owner input and were not forwarded.")
                    continue
                photos = message.get("photo") or []
                image_base64 = ""
                image_mime = ""
                if photos:
                    file_id = str((photos[-1] or {}).get("file_id") or "").strip()
                    if file_id:
                        image_base64, image_mime = await client.download_photo(file_id)
                if not text and not image_base64:
                    continue
                sender_name = (
                    str(sender.get("username") or "").strip()
                    or " ".join(
                        str(part).strip()
                        for part in (sender.get("first_name"), sender.get("last_name"))
                        if part
                    )
                    or f"Telegram {sender.get('id') or chat_id}"
                )
                sender_label = f"Telegram ({sender_name})"
                await _inject(api, {
                    "text": text,
                    "chat_id": chat_id,
                    "user_id": int(sender.get("id") or chat_id or 1),
                    "source": "telegram-bridge",
                    "sender_label": sender_label,
                    "transport": {
                        "kind": "telegram",
                        "conversation_id": str(chat_id),
                        "sender_label": sender_label,
                    },
                    "image_base64": image_base64,
                    "image_mime": image_mime,
                    "image_caption": caption,
                })
            await asyncio.sleep(0.1)
    return poller


def _make_outbound(api):
    async def handle(event: Dict[str, Any]) -> None:
        protected_settings = api.get_settings(["TELEGRAM_BOT_TOKEN"])
        local_settings = _load_settings(api)
        client = TelegramClient(protected_settings.get("TELEGRAM_BOT_TOKEN", ""))
        chat_id = _target_chat(local_settings, event)
        if not chat_id:
            return
        text = str(event.get("text") or "").strip()
        if text:
            await client.send_message(chat_id, text)
    return handle


def _make_typing(api):
    async def handle(event: Dict[str, Any]) -> None:
        protected_settings = api.get_settings(["TELEGRAM_BOT_TOKEN"])
        local_settings = _load_settings(api)
        client = TelegramClient(protected_settings.get("TELEGRAM_BOT_TOKEN", ""))
        chat_id = _target_chat(local_settings, event)
        if chat_id:
            await client.send_chat_action(chat_id, "typing")
    return handle


def _make_photo(api):
    async def handle(event: Dict[str, Any]) -> None:
        protected_settings = api.get_settings(["TELEGRAM_BOT_TOKEN"])
        local_settings = _load_settings(api)
        client = TelegramClient(protected_settings.get("TELEGRAM_BOT_TOKEN", ""))
        chat_id = _target_chat(local_settings, event)
        image_base64 = str(event.get("image_base64") or "").strip()
        if chat_id and image_base64:
            await client.send_photo(
                chat_id,
                image_base64,
                caption=str(event.get("caption") or ""),
                mime=str(event.get("mime") or "image/png"),
            )
    return handle


def register(api):
    api.register_supervised_task("poller", _make_poller(api), restart_policy="on_failure", max_restarts=10)
    api.subscribe_event("chat.outbound", _make_outbound(api))
    api.subscribe_event("chat.typing", _make_typing(api))
    api.subscribe_event("chat.photo", _make_photo(api))
    api.register_route("settings/save", handler=_make_settings_save(api), methods=("POST",))
    api.register_settings_section(
        "telegram",
        title="Telegram Bridge",
        schema={
            "components": [
                {
                    "type": "markdown",
                    "text": "Set TELEGRAM_BOT_TOKEN in Settings → Secrets, grant it to this skill, then configure the chat id here.",
                },
                {
                    "type": "form",
                    "route": "settings/save",
                    "method": "POST",
                    "fields": [
                        {"name": "TELEGRAM_CHAT_ID", "label": "Telegram Chat ID", "type": "text", "placeholder": "optional pinned chat id"},
                        {"name": "TELEGRAM_MAX_UPDATES_PER_POLL", "label": "Max updates per poll", "type": "number", "placeholder": "20"},
                    ],
                    "submit_label": "Save Telegram settings",
                },
            ]
        },
    )
