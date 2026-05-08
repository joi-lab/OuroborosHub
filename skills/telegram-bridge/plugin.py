from __future__ import annotations

import asyncio
import os
from typing import Any, Dict

import httpx

from .lib.telegram_api import TelegramClient


def _host_headers(api) -> Dict[str, str]:
    return {"X-Skill-Token": api.get_skill_token().use_in_request()}


def _target_chat(settings: Dict[str, Any], event: Dict[str, Any]) -> int:
    configured = str(settings.get("TELEGRAM_CHAT_ID") or "").strip()
    if configured:
        try:
            return int(configured)
        except ValueError:
            return 0
    try:
        return int(event.get("telegram_chat_id") or event.get("chat_id") or 0)
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
        settings = api.get_settings(["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
        client = TelegramClient(settings.get("TELEGRAM_BOT_TOKEN", ""))
        pinned_chat = str(settings.get("TELEGRAM_CHAT_ID") or "").strip()
        offset = 0
        while True:
            updates = await client.get_updates(offset)
            for update in updates:
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
                await _inject(api, {
                    "text": text,
                    "chat_id": chat_id,
                    "user_id": int(sender.get("id") or chat_id or 1),
                    "sender_label": f"Telegram ({sender_name})",
                    "telegram_chat_id": chat_id,
                    "image_base64": image_base64,
                    "image_mime": image_mime,
                    "image_caption": str(message.get("caption") or ""),
                })
            await asyncio.sleep(0.1)
    return poller


def _make_outbound(api):
    async def handle(event: Dict[str, Any]) -> None:
        settings = api.get_settings(["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
        client = TelegramClient(settings.get("TELEGRAM_BOT_TOKEN", ""))
        chat_id = _target_chat(settings, event)
        if not chat_id:
            return
        text = str(event.get("text") or "").strip()
        if text:
            await client.send_message(chat_id, text)
    return handle


def _make_typing(api):
    async def handle(event: Dict[str, Any]) -> None:
        settings = api.get_settings(["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
        client = TelegramClient(settings.get("TELEGRAM_BOT_TOKEN", ""))
        chat_id = _target_chat(settings, event)
        if chat_id:
            await client.send_chat_action(chat_id, "typing")
    return handle


def _make_photo(api):
    async def handle(event: Dict[str, Any]) -> None:
        settings = api.get_settings(["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
        client = TelegramClient(settings.get("TELEGRAM_BOT_TOKEN", ""))
        chat_id = _target_chat(settings, event)
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
    api.register_settings_section(
        "telegram",
        title="Telegram Bridge",
        schema={
            "components": [
                {
                    "type": "markdown",
                    "text": "Set TELEGRAM_BOT_TOKEN in Settings → Secrets, grant it to this skill, then enable the skill.",
                }
            ]
        },
    )
