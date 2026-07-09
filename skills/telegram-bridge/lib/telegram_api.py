from __future__ import annotations

import base64
import mimetypes
import os
import re
from typing import Any, Dict, Optional

import httpx

# Telegram hard-caps a single sendMessage at 4096 chars. We chunk the RAW text
# (pre-HTML) on line/space boundaries below this so each chunk is converted and
# sent independently — avoiding both the silent "message is too long" 400 that
# dropped long replies and tag-splitting corruption from cutting inside HTML.
_TELEGRAM_TEXT_LIMIT = 4096
_TELEGRAM_CHUNK_RAW = 3500


def _chunk_raw_text(text: str, limit: int = _TELEGRAM_CHUNK_RAW) -> list[str]:
    """Split raw text into <=limit-char pieces on line, then space, boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for line in text.split("\n"):
        while len(line) > limit:
            # A single very long line: break on the last space within the window,
            # else hard-cut at the limit.
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            piece = line[:cut]
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(piece)
            line = line[cut:].lstrip(" ")
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) > limit:
            if buf:
                chunks.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return chunks or [""]


def markdown_to_telegram_html(text: str) -> str:
    """Convert standard rich Markdown text into Telegram-compliant HTML syntax."""
    if not text:
        return text

    # 1. Escape HTML special characters
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Placeholder dictionaries
    pre_placeholder_map = {}
    code_placeholder_map = {}

    # 2. Extract multi-line code blocks before parsing other markdown
    def replace_pre(match: re.Match) -> str:
        code_content = match.group(2)
        placeholder = f"PREPLACEHOLDER{len(pre_placeholder_map)}"
        pre_placeholder_map[placeholder] = f"<pre>{code_content}</pre>"
        return placeholder

    text = re.sub(r"```([A-Za-z0-9_-]*)\s*\n?(.*?)```", replace_pre, text, flags=re.DOTALL)

    # 3. Extract inline code blocks
    def replace_code(match: re.Match) -> str:
        inner = match.group(1)
        placeholder = f"CODEPLACEHOLDER{len(code_placeholder_map)}"
        code_placeholder_map[placeholder] = f"<code>{inner}</code>"
        return placeholder

    text = re.sub(r"`([^`\n]+)`", replace_code, text)

    # 4. Headers and list markdown formatting line-by-line
    lines = []
    for line in text.split("\n"):
        header_match = re.match(r"^(\s*)#{1,6}\s+(.+)$", line)
        if header_match:
            indent = header_match.group(1) or ""
            content = header_match.group(2)
            lines.append(f"{indent}<b>{content}</b>")
        else:
            # Replace starting list bullet * or - with •
            bullet_match = re.match(r"^(\s*)[*-]\s+(.+)$", line)
            if bullet_match:
                lines.append(f"{bullet_match.group(1)}• {bullet_match.group(2)}")
            else:
                lines.append(line)
    text = "\n".join(lines)

    # 5. Bold and Italic replacing outside of inline blocks.
    # Asterisk patterns match anywhere — `**bold**` and `*italic*` are unambiguous.
    # Underscore patterns require non-word context on both sides so identifiers
    # like `chat_id`, `state_dir`, `OUROBOROS_MODEL` inside bold spans do NOT
    # trigger spurious italic wraps that cross outer tag boundaries (which
    # would produce malformed nested HTML and a Telegram 400 Bad Request).
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)__(?=\S)([^_\n]+?)(?<=\S)__(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_(?=\S)([^_\n]+?)(?<=\S)_(?!\w)", r"<i>\1</i>", text)

    # 6. Links [text](url) -> <a href="url">text</a>
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r'<a href="\2">\1</a>', text)

    # 7. Reconstruct codeblocks (sorted by length descending to prevent sub-string prefix collisions)
    for placeholder, code_html in sorted(pre_placeholder_map.items(), key=lambda x: len(x[0]), reverse=True):
        text = text.replace(placeholder, code_html)
    for placeholder, code_html in sorted(code_placeholder_map.items(), key=lambda x: len(x[0]), reverse=True):
        text = text.replace(placeholder, code_html)

    return text


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
            if response.status_code >= 400:
                # Surface Telegram's description so callers can distinguish
                # benign cases (e.g. "message is not modified") from real errors.
                try:
                    desc = str((response.json() or {}).get("description") or "").strip()
                except Exception:
                    desc = ""
                suffix = f": {desc}" if desc else ""
                raise RuntimeError(f"Telegram API {method} returned HTTP {response.status_code}{suffix}")
            payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description") or f"Telegram API error: {method}")
        return payload

    async def get_updates(self, offset: int) -> list[dict]:
        payload = await self.call("getUpdates", data={"timeout": 20, "offset": offset}, timeout=25)
        return list(payload.get("result") or [])

    async def send_message(self, chat_id: int, text: str, parse_mode: str = "HTML") -> int:
        """Send a text message, chunking past Telegram's 4096-char limit.

        Long text is split on line/space boundaries and each chunk is sent as its
        own message (converted independently so HTML tags never span a cut).
        Returns the LAST chunk's message_id (0 on parse failure / empty input).
        """
        chunks = _chunk_raw_text(str(text or ""))
        last_message_id = 0
        for chunk in chunks:
            formatted = markdown_to_telegram_html(chunk) if parse_mode == "HTML" else chunk
            data = {"chat_id": str(chat_id), "text": formatted}
            if parse_mode:
                data["parse_mode"] = parse_mode
            payload = await self.call("sendMessage", data=data, timeout=20)
            try:
                last_message_id = int((payload.get("result") or {}).get("message_id") or 0)
            except (TypeError, ValueError):
                last_message_id = 0
        return last_message_id

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, parse_mode: str = "HTML") -> bool:
        """Replace the text of an existing message in-place (silent mode). Returns True on success.

        Failures (message too old, deleted, identical content, parse error) are
        suppressed so the caller can fall back to send_message + reset tracking.
        """
        formatted = markdown_to_telegram_html(text) if parse_mode == "HTML" else text
        data = {
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "text": formatted,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        try:
            await self.call("editMessageText", data=data, timeout=20)
            return True
        except Exception as exc:
            # "message is not modified" means the bubble already shows this exact
            # text — treat it as success so the caller does NOT post a duplicate.
            if "not modified" in str(exc).lower():
                return True
            return False

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        await self.call("sendChatAction", data={"chat_id": str(chat_id), "action": action}, timeout=10)

    async def send_photo(self, chat_id: int, image_base64: str, *, caption: str = "", mime: str = "image/png", parse_mode: str = "HTML") -> None:
        filename = "image.png" if mime == "image/png" else "image.jpg"
        files = {"photo": (filename, base64.b64decode(image_base64), mime)}
        formatted = markdown_to_telegram_html(caption) if parse_mode == "HTML" else caption
        data = {"chat_id": str(chat_id), "caption": formatted}
        if parse_mode:
            data["parse_mode"] = parse_mode
        await self.call("sendPhoto", data=data, files=files, timeout=30)

    async def send_document(
        self, chat_id: int, file_bytes: bytes, filename: str = "file", *, caption: str = "", parse_mode: str = "HTML"
    ) -> None:
        """Send an arbitrary document/file to a chat via sendDocument."""
        safe_name = (str(filename or "file").replace("\r", " ").replace("\n", " ").strip() or "file")
        files = {"document": (safe_name, file_bytes, "application/octet-stream")}
        formatted = markdown_to_telegram_html(caption) if (caption and parse_mode == "HTML") else caption
        data = {"chat_id": str(chat_id)}
        if formatted:
            data["caption"] = formatted
            if parse_mode:
                data["parse_mode"] = parse_mode
        await self.call("sendDocument", data=data, files=files, timeout=60)

    async def send_message_with_inline_keyboard(
        self, chat_id: int, text: str, keyboard: list[list[dict]], parse_mode: str = "HTML"
    ) -> None:
        """Send a message with an inline keyboard (list of button rows)."""
        import json as _json
        reply_markup = _json.dumps({"inline_keyboard": keyboard})
        formatted = markdown_to_telegram_html(text) if parse_mode == "HTML" else text
        data = {"chat_id": str(chat_id), "text": formatted, "reply_markup": reply_markup}
        if parse_mode:
            data["parse_mode"] = parse_mode
        await self.call(
            "sendMessage",
            data=data,
            timeout=20,
        )

    async def answer_callback_query(self, callback_query_id: str, *, text: str = "") -> None:
        """Acknowledge a callback query from an inline button press."""
        data: dict = {"callback_query_id": callback_query_id}
        if text:
            data["text"] = text
        await self.call("answerCallbackQuery", data=data, timeout=10)

    async def download_photo(self, file_id: str) -> tuple[str, str]:
        payload = await self.call("getFile", data={"file_id": file_id}, timeout=20)
        file_path = str((payload.get("result") or {}).get("file_path") or "").strip()
        if not file_path:
            raise RuntimeError("Telegram file path is missing")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self.file_base}/{file_path}")
            if response.status_code >= 400:
                raise RuntimeError(f"Telegram file download returned HTTP {response.status_code}")
            content = response.content
        mime = mimetypes.guess_type(file_path)[0] or "image/jpeg"
        return base64.b64encode(content).decode("ascii"), mime

    async def download_file(self, file_id: str) -> bytes:
        """Download an arbitrary file from Telegram servers and return its raw bytes."""
        payload = await self.call("getFile", data={"file_id": file_id}, timeout=20)
        file_path = str((payload.get("result") or {}).get("file_path") or "").strip()
        if not file_path:
            raise RuntimeError("Telegram file path is missing")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{self.file_base}/{file_path}")
            if response.status_code >= 400:
                raise RuntimeError(f"Telegram file download returned HTTP {response.status_code}")
            return response.content

    async def edit_message_text_with_inline_keyboard(
        self, chat_id: int, message_id: int, text: str, keyboard: list[list[dict]], parse_mode: str = "HTML"
    ) -> None:
        """Edit an existing Telegram message text and inline keyboard in-place."""
        import json as _json
        reply_markup = _json.dumps({"inline_keyboard": keyboard})
        formatted = markdown_to_telegram_html(text) if parse_mode == "HTML" else text
        data = {
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "text": formatted,
            "reply_markup": reply_markup,
        }
        if parse_mode:
            data["parse_mode"] = parse_mode
        try:
            await self.call("editMessageText", data=data, timeout=20)
        except Exception:
            pass  # Suppress errors if message content/keyboard was unchanged


_LOCALIZED_TEXTS = {
    "en": {
        "menu_title_strict": "🤖 **Ouroboros Control Panel**\nStrict mode is active. Commands are blocked.\n\nSelect action:",
        "menu_title": "🤖 **Ouroboros Control Centre**\nCommand Mode: `{command_mode}`\nLanguage: `{lang}`\n\nExplore and monitor the core using the buttons below:",
        "btn_metrics": "📉 Status & Metrics",
        "btn_mind": "🧠 Mind & BG",
        "btn_security": "🛡️ Security settings",
        "btn_language": "🌐 Select language",
        "btn_refresh": "🔄 Update parameter card",
        "btn_back": "⬅️ Back to main panel",
        "btn_stop_bg": "🔴 Pause background thoughts",
        "btn_start_bg": "🟢 Resume background thoughts",
        "btn_thoughts": "💭 What are you thinking about?",
        "metrics_title": "📊 **Ouroboros live metrics**\n\n{info_text}\n---",
        "mind_title": "🧠 **Background Consciousness**\n\nCurrent state: {state_str}\n\nBackground thinking processes information between your chat queries.",
        "mind_thoughts": "\n\n**Recent thoughts catalog:**\n{thoughts_text}",
        "mind_state_active": "🟢 **Thinking** (running)",
        "mind_state_sleeping": "🔴 **Sleeping** (paused)",
        "mode_title": "🛡️ Telegram Command Mode Settings\nCurrently active: **{command_mode}**\n\nSelect a mode to set:",
        "mode_strict": "🛡️ Strict (block all commands)",
        "mode_safe": "⚖️ Safe (allow status only)",
        "mode_full": "⚡ Full Access (all control buttons)",
        "lang_title": "🌐 Select chatbot bridge interface language:\nCurrently active: **English**",
        "lang_en": "🇬🇧 English",
        "lang_ru": "🇷🇺 Русский",
        "help_text": (
            "🤖 **Ouroboros Telegram Bridge Help**\n\n"
            "Available commands:\n"
            "• `/menu` — Show interactive control panel with active tabs\n"
            "• `/language` — Change bridge interface language\n"
            "• `/status` — Request live system status (if allowed)\n"
            "• `/help` — Show this friendly usage guide\n\n"
            "Modes description (changed in Web UI → Settings → Telegram Bridge):\n"
            "• **strict** — block all command injections (only `/menu`, `/help`, `/language`)\n"
            "• **safe_commands** — allow status monitoring\n"
            "• **full_access** — allow status + background loop start/stop"
        ),
        "slash_blocked_strict": "⛔ Slash commands are not allowed in strict mode. Use `/menu` to see available options, or change mode in Settings → Telegram Bridge.",
        "slash_blocked_mode": "⛔ This command is not allowed in the current mode. Use `/menu` to see available options.",
        "not_authorized": "Not authorized",
        "updating_status": "Updating status metrics...",
        "extracting_thoughts": "Extracting thoughts...",
        "injecting_consciousness": "Injecting consciousness signal...",
        "restricted_safe": "⛔ Restricted in safe mode",
        "mode_changed": "✅ Mode changed to: {new_mode}",
        "lang_changed": "✅ Language changed to English",
        "unknown_command": "Unknown command",
        "restricted_current": "⛔ Command not allowed in current mode",
        "sending": "Sending: {translated_text}",
        "blank_voice": "⚠️ **Voice message is blank.** Speak louder or closer to the microphone!",
        "voice_error": "❌ **Voice message transcription error:**\n`{exc}`",
        "metrics_budget_status": "• **Budget Status:**\n  Spent: `${spent_usd:.4f}`\n  Limit: `${total_budget:.2f}`\n  Remaining: `${rem:.4f}`\n\n• **System Environment:**\n  Branch: `{branch}`\n  BG Thoughts: `{bg_status}`",
        "bg_active_label": "ACTIVE",
        "bg_sleeping_label": "SLEEPING",
        "btn_settings": "⚙️ Settings",
        "btn_model": "🤖 Model Routing",
        "btn_budget": "💸 Budget Limits",
        "settings_title": "⚙️ **Ouroboros Settings**\nConfigure and monitor bridge parameters, model routing, and budget limits:",
        "model_title": "🤖 **Main Model Routing**\nSelect main reasoning model. Selecting a model will send an instruction to Ouroboros to update settings.json.\n\nCurrent model: `{current_model}`",
        "budget_title": "💸 **Budget & Limits**\nManage cumulative spending limits. Spending is tracked in USD.\n\nCurrent limit: `${total_budget:.2f}` | Spent: `${spent_usd:.4f}` | Remaining: `${rem:.4f}`",
        "requesting_model": "🤖 Requesting main model change to: {model}",
        "requesting_budget": "💸 Requesting budget increment of: +${amount}",
        "btn_silent_on": "🔕 Silent Mode: ON",
        "btn_silent_off": "🔔 Silent Mode: OFF",
        "silent_toggled_on": "🔕 Silent mode enabled — new thoughts will replace the last message",
        "silent_toggled_off": "🔔 Silent mode disabled — each thought becomes a new message",
        "btn_custom_model": "Enter specific model...",
        "btn_custom_budget": "Enter specific budget...",
        "prompt_model_text": "⌨️ **Enter specific model name**\n\nPlease enter the full model identifier (e.g. `anthropic/claude-3-7-sonnet` or `google/gemini-2.1-pro`):",
        "prompt_budget_text": "⌨️ **Enter specific budget limit**\n\nPlease enter the desired limit in USD (e.g. `500` or `1250.50`):",
        "invalid_budget_input": "❌ **Invalid budget**: please enter a positive number.",
        "requesting_budget_val": "💸 Requesting budget limit change to: ${amount:.2f}",
    },
    "ru": {
        "menu_title_strict": "🤖 **Панель управления Ouroboros**\nРежим Strict активен. Команды заблокированы.\n\nВыберите действие:",
        "menu_title": "🤖 **Центр управления Ouroboros**\nРежим команд: `{command_mode}`\nЯзык: `{lang}`\n\nУправляйте и следите за ядром с помощью кнопок:",
        "btn_metrics": "📉 Статус и метрики",
        "btn_mind": "🧠 Фоновое сознание",
        "btn_security": "🛡️ Настройки безопасности",
        "btn_language": "🌐 Выбор языка",
        "btn_refresh": "🔄 Обновить показатели",
        "btn_back": "⬅️ Назад в меню",
        "btn_stop_bg": "🔴 Приостановить размышления",
        "btn_start_bg": "🟢 Продолжить размышления",
        "btn_thoughts": "💭 О чём ты думаешь сейчас?",
        "metrics_title": "📊 **Живые показатели Ouroboros**\n\n{info_text}\n---",
        "mind_title": "🧠 **Фоновое Сознание**\n\nТекущее состояние: {state_str}\n\nФоновое мышление анализирует информацию между вашими запросами.",
        "mind_thoughts": "\n\n**Последние мысли из лога:**\n{thoughts_text}",
        "mind_state_active": "🟢 **Думает** (активно)",
        "mind_state_sleeping": "🔴 **Спит** (на паузе)",
        "mode_title": "🛡️ Настройки режима команд Telegram\nАктивный режим: **{command_mode}**\n\nВыберите режим для активации:",
        "mode_strict": "🛡️ Strict (блокировать все команды)",
        "mode_safe": "⚖️ Safe (только панель статуса)",
        "mode_full": "⚡ Full Access (все кнопки управления)",
        "lang_title": "🌐 Выберите язык интерфейса бота-моста:\nАктивный язык: **Русский**",
        "lang_en": "🇬🇧 English",
        "lang_ru": "🇷🇺 Русский",
        "help_text": (
            "🤖 **Справка по Telegram-мосту Ouroboros**\n\n"
            "Доступные команды:\n"
            "• `/menu` — Открыть интерактивную панель управления\n"
            "• `/language` — Изменить и настроить язык интерфейса\n"
            "• `/status` — Запросить текущий статус системы (если разрешено)\n"
            "• `/help` — Показать это руководство\n\n"
            "Описание режимов (меняется в Web UI → Settings → Telegram Bridge):\n"
            "• **strict** — блокировать ввод команд (доступны только `/menu`, `/help`, `/language`)\n"
            "• **safe_commands** — разрешить просмотр метрик и статуса\n"
            "• **full_access** — доступ ко всем кнопкам, включая запуск/паузу фонового сознания"
        ),
        "slash_blocked_strict": "⛔ Слэш-команды запрещены в режиме strict. Используйте `/menu` для вызова панели управления или измените режим в Settings → Telegram Bridge.",
        "slash_blocked_mode": "⛔ Эта команда не разрешена в текущем режиме. Используйте `/menu` для вызова управления.",
        "not_authorized": "Доступ ограничен",
        "updating_status": "Обновление метрик...",
        "extracting_thoughts": "Извлечение мыслей...",
        "injecting_consciousness": "Отправка сигнала сознания...",
        "restricted_safe": "⛔ Ограничено в режиме Safe",
        "mode_changed": "✅ Режим управления изменен на: {new_mode}",
        "lang_changed": "✅ Язык интерфейса изменен на Русский",
        "unknown_command": "Неизвестная команда",
        "restricted_current": "⛔ Команда заблокирована в текущем режиме безопасности",
        "sending": "Отправлено: {translated_text}",
        "blank_voice": "⚠️ **Аудиозапись пуста.** Пожалуйста, говорите громче или ближе к микрофону!",
        "voice_error": "❌ **Ошибка расшифровки аудио:**\n`{exc}`",
        "metrics_budget_status": "• **Бюджетный статус:**\n  Потрачено: `${spent_usd:.4f}`\n  Лимит: `${total_budget:.2f}`\n  Осталось: `${rem:.4f}`\n\n• **Окружение системы:**\n  Ветка Git: `{branch}`\n  Фоновые мысли: `{bg_status}`",
        "bg_active_label": "АКТИВНЫ",
        "bg_sleeping_label": "СПЯТ",
        "btn_settings": "⚙️ Настройки",
        "btn_model": "🤖 Выбор модели",
        "btn_budget": "💸 Лимиты бюджета",
        "settings_title": "⚙️ **Настройки Ouroboros**\nНастройте и отслеживайте параметры моста, маршрутизацию моделей и лимиты бюджета:",
        "model_title": "🤖 **Выбор основной модели**\nВыберите основную модель рассуждений. Изменение отправит запрос Ouroboros на обновление settings.json.\n\nТекущая модель: `{current_model}`",
        "budget_title": "💸 **Бюджет и лимиты**\nУправляйте лимитами расходов. Траты отслеживаются в долларах США.\n\nТекущий лимит: `${total_budget:.2f}` | Потрачено: `${spent_usd:.4f}` | Осталось: `${rem:.4f}`",
        "requesting_model": "🤖 Запрос изменения основной модели на: {model}",
        "requesting_budget": "💸 Запрос увеличения бюджета на: +${amount}",
        "btn_silent_on": "🔕 Тихий режим: ВКЛ",
        "btn_silent_off": "🔔 Тихий режим: ВЫКЛ",
        "silent_toggled_on": "🔕 Тихий режим включён — новые мысли будут заменять предыдущее сообщение",
        "silent_toggled_off": "🔔 Тихий режим выключен — каждая мысль становится отдельным сообщением",
        "btn_custom_model": "Ввести другую модель...",
        "btn_custom_budget": "Ввести конкретный бюджет...",
        "prompt_model_text": "⌨️ **Ввод конкретного имени модели**\n\nПожалуйста, введите полное название модели (например, `anthropic/claude-3-7-sonnet` или `google/gemini-2.1-pro`):",
        "prompt_budget_text": "⌨️ **Ввод конкретного лимита бюджета**\n\nПожалуйста, введите желаемое значение в USD (например, `500` или `1250.50`):",
        "invalid_budget_input": "❌ **Неверный бюджет**: пожалуйста, введите положительное число.",
        "requesting_budget_val": "💸 Запрос изменения лимита бюджета на: ${amount:.2f}",
    }
}


