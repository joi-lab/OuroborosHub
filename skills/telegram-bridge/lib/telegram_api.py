from __future__ import annotations

import base64
import mimetypes
import os
from typing import Any, Dict, Optional

import httpx


class TelegramClient:
    def __init__(self, token: str):
        self.token = str(token or "").strip()
        if not self.token:
            raise ValueError("TELEGRAM_BOT_TOKEN is missing")
        default_api = f"https://api.telegram.org/bot{self.token}"
        default_file = f"https://api.telegram.org/file/bot{self.token}"
        self.api_base = os.environ.get("TELEGRAM_API_BASE", default_api).rstrip("/")
        self.file_base = os.environ.get("TELEGRAM_FILE_BASE", default_file).rstrip("/")

    async def call(self, method: str, *, data: Optional[dict] = None, files: Optional[dict] = None, timeout: int = 30) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{self.api_base}/{method}", data=data, files=files)
            response.raise_for_status()
            payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description") or f"Telegram API error: {method}")
        return payload

    async def get_updates(self, offset: int) -> list[dict]:
        payload = await self.call("getUpdates", data={"timeout": 20, "offset": offset}, timeout=25)
        return list(payload.get("result") or [])

    async def send_message(self, chat_id: int, text: str) -> None:
        await self.call("sendMessage", data={"chat_id": str(chat_id), "text": text}, timeout=20)

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        await self.call("sendChatAction", data={"chat_id": str(chat_id), "action": action}, timeout=10)

    async def send_photo(self, chat_id: int, image_base64: str, *, caption: str = "", mime: str = "image/png") -> None:
        filename = "image.png" if mime == "image/png" else "image.jpg"
        files = {"photo": (filename, base64.b64decode(image_base64), mime)}
        await self.call("sendPhoto", data={"chat_id": str(chat_id), "caption": caption}, files=files, timeout=30)

    async def download_photo(self, file_id: str) -> tuple[str, str]:
        payload = await self.call("getFile", data={"file_id": file_id}, timeout=20)
        file_path = str((payload.get("result") or {}).get("file_path") or "").strip()
        if not file_path:
            raise RuntimeError("Telegram file path is missing")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self.file_base}/{file_path}")
            response.raise_for_status()
            content = response.content
        mime = mimetypes.guess_type(file_path)[0] or "image/jpeg"
        return base64.b64encode(content).decode("ascii"), mime
