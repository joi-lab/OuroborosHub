from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
from typing import Any, Dict

import httpx
from starlette.responses import JSONResponse

from .lib.telegram_api import TelegramClient, markdown_to_telegram_html, _LOCALIZED_TEXTS

_SLASH_COMMAND_RE = re.compile(r"^\s*/[A-Za-z]")

# Slash commands are NEVER injected as-is into the Host Service.
# Instead, allowed commands are translated to natural-language text that the
# LLM can interpret, keeping the bridge on the right side of the
# inject_chat_minimization checklist item.
_COMMAND_TRANSLATIONS: dict[str, str] = {
    "/status": "show status",
    "/bg status": "background consciousness status",
    "/bg start": "start background consciousness",
    "/bg stop": "stop background consciousness",
    "/bg": "background consciousness status",
}

# Commands that are NEVER forwarded even as translations — too dangerous.
_DANGEROUS_COMMANDS = frozenset({"/panic", "/restart", "/review", "/evolve on", "/evolve off", "/evolve"})

_COMMAND_MODE_STRICT = "strict"
_COMMAND_MODE_SAFE = "safe_commands"
_COMMAND_MODE_FULL = "full_access"
_VALID_COMMAND_MODES = frozenset({_COMMAND_MODE_STRICT, _COMMAND_MODE_SAFE, _COMMAND_MODE_FULL})


# Which translation keys are available in each mode
_SAFE_TRANSLATION_KEYS = frozenset({"/status", "/bg status", "/bg"})
_FULL_TRANSLATION_KEYS = frozenset(_COMMAND_TRANSLATIONS.keys())

# Callback data → (translated_text, minimum_required_mode) for inline keyboard
# buttons. These are intentionally non-slash strings so no slash command ever
# reaches _inject from a button press.
_CALLBACK_MAP: dict[str, tuple[str, str]] = {
    "cmd:status":    ("show status", _COMMAND_MODE_SAFE),
    "cmd:bg_status": ("background consciousness status", _COMMAND_MODE_SAFE),
    "cmd:bg_start":  ("start background consciousness", _COMMAND_MODE_FULL),
    "cmd:bg_stop":   ("stop background consciousness", _COMMAND_MODE_FULL),
}


def _state_file(api, name: str) -> pathlib.Path:
    return pathlib.Path(api.get_state_dir()) / name


def _load_settings(api) -> Dict[str, Any]:
    path = _state_file(api, "settings.json")
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def _is_silent_mode_enabled(settings: Dict[str, Any]) -> bool:
    """Silent mode replaces successive outbound thoughts via editMessageText
    rather than spamming new messages. Default: off."""
    raw = str(settings.get("TELEGRAM_SILENT_MODE") or "off").strip().lower()
    return raw in ("on", "true", "1", "yes")


def _load_silent_state(api) -> Dict[str, int]:
    """Load per-chat last outbound message id mapping. Returns {} if missing/corrupt."""
    path = _state_file(api, "silent_state.json")
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out: Dict[str, int] = {}
                for key, value in data.items():
                    try:
                        out[str(key)] = int(value)
                    except (TypeError, ValueError):
                        continue
                return out
    except Exception:
        pass
    return {}


def _save_silent_state(api, data: Dict[str, int]) -> None:
    path = _state_file(api, "silent_state.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _get_silent_msg(api, chat_id: int) -> int:
    return int(_load_silent_state(api).get(str(chat_id)) or 0)


def _set_silent_msg(api, chat_id: int, message_id: int) -> None:
    state = _load_silent_state(api)
    state[str(chat_id)] = int(message_id)
    _save_silent_state(api, state)


def _clear_silent_msg(api, chat_id: int) -> None:
    state = _load_silent_state(api)
    if str(chat_id) in state:
        state.pop(str(chat_id), None)
        _save_silent_state(api, state)


def _setting_int(settings: Dict[str, Any], key: str, default: int, *, minimum: int = 1, maximum: int = 100) -> int:
    try:
        value = int(settings.get(key) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _translate_command(text: str, command_mode: str) -> str | None:
    """Translate a slash command to safe natural-language text, or return None to reject.

    Slash commands are NEVER injected as-is. Instead, allowed commands are
    converted to plain text that the LLM interprets without hitting the
    reserved supervisor command path. Returns the original text unchanged
    if it is not a slash command.
    """
    if not _SLASH_COMMAND_RE.match(str(text or "")):
        return text  # Not a slash command — pass through unchanged
    normalized = str(text or "").strip().lower()
    # Dangerous commands are always rejected
    for dangerous in _DANGEROUS_COMMANDS:
        if normalized == dangerous or normalized.startswith(dangerous + " "):
            return None
    if command_mode == _COMMAND_MODE_STRICT:
        return None  # All slash commands blocked
    # Determine which translations are available for this mode
    allowed_keys = _SAFE_TRANSLATION_KEYS if command_mode == _COMMAND_MODE_SAFE else _FULL_TRANSLATION_KEYS
    for cmd_key in sorted(allowed_keys, key=len, reverse=True):
        if normalized == cmd_key or normalized.startswith(cmd_key + " "):
            return _COMMAND_TRANSLATIONS[cmd_key]
    return None  # Unrecognized slash command — reject


def _build_menu_keyboard(command_mode: str, lang: str = "en") -> tuple[str, list[list[dict]]]:
    """Return (header_text, inline_keyboard_rows) for the /menu command."""
    t = _LOCALIZED_TEXTS[lang]
    if command_mode == _COMMAND_MODE_STRICT:
        return (
            t["menu_title_strict"],
            [[{"text": t["btn_settings"], "callback_data": "nav:settings"}]],
        )

    header = t["menu_title"].format(command_mode=command_mode, lang=lang.upper())
    keyboard = [
        [
            {"text": t["btn_metrics"], "callback_data": "nav:status"},
            {"text": t["btn_mind"], "callback_data": "nav:mind"},
        ],
        [
            {"text": t["btn_settings"], "callback_data": "nav:settings"},
        ]
    ]
    return header, keyboard


def _build_menu_status(command_mode: str, lang: str = "en", info_text: str = "") -> tuple[str, list[list[dict]]]:
    """Return status header and keyboard with Refresh and Back button."""
    t = _LOCALIZED_TEXTS[lang]
    header = t["metrics_title"].format(info_text=info_text)
    keyboard = [
        [{"text": t["btn_refresh"], "callback_data": "cmd_act:update_status"}],
        [{"text": t["btn_back"], "callback_data": "nav:menu"}]
    ]
    return header, keyboard


def _build_menu_mind(command_mode: str, lang: str = "en", bg_enabled: bool = False, thoughts_text: str = "") -> tuple[str, list[list[dict]]]:
    """Return mind controlling header and buttons."""
    t = _LOCALIZED_TEXTS[lang]
    state_str = t["mind_state_active"] if bg_enabled else t["mind_state_sleeping"]
    header = t["mind_title"].format(state_str=state_str)
    if thoughts_text:
        header += t["mind_thoughts"].format(thoughts_text=thoughts_text)

    row = []
    if command_mode == _COMMAND_MODE_FULL:
        if bg_enabled:
            row.append({"text": t["btn_stop_bg"], "callback_data": "cmd_act:bg_stop"})
        else:
            row.append({"text": t["btn_start_bg"], "callback_data": "cmd_act:bg_start"})

    keyboard = []
    if row:
        keyboard.append(row)
    keyboard.append([{"text": t["btn_thoughts"], "callback_data": "cmd_act:bg_thoughts"}])
    keyboard.append([{"text": t["btn_back"], "callback_data": "nav:menu"}])
    return header, keyboard


def _load_recent_thoughts(api) -> str:
    """Read the last few blocks from progress.jsonl and build a text snapshot."""
    progress_file = pathlib.Path(api.get_state_dir()).parent.parent.parent / "logs" / "progress.jsonl"
    if not progress_file.exists():
        return "_No thoughts log created yet._"
    try:
        lines = progress_file.read_text(encoding="utf-8").splitlines()
        recent = []
        # Extract last 40 lines to find JSON objects
        for line in reversed(lines[-40:]):
            if not line.strip():
                continue
            try:
                elem = json.loads(line)
                # Look for values in message, text, thoughts or raw content
                text = str(elem.get("text") or elem.get("message") or elem.get("thoughts") or "").strip()
                if text and len(text) > 10:
                    # Clean up technical markdown elements
                    text = text.replace("`", "").replace("*", "").replace("#", "")
                    if len(text) > 100:
                        text = text[:97] + "..."
                    timestamp = str(elem.get("timestamp") or elem.get("created_at") or "")
                    if timestamp:
                        # Extract hours:minutes
                        time_match = re.search(r"T(\d{2}:\d{2})", timestamp)
                        time_str = f"[{time_match.group(1)}] " if time_match else f"[{timestamp[:10]}] "
                    else:
                        time_str = ""
                    recent.append(f"• {time_str}{text}")
                    if len(recent) >= 4:
                        break
            except Exception:
                pass
        return "\n".join(recent) if recent else "_Thoughts log is empty or waiting for next cycle._"
    except Exception as exc:
        return f"_Failed to read log: {exc}_"


async def _transcribe_voice(api, ogg_bytes: bytes) -> str:
    """Send voice bytes to OpenAI Whisper API for transcriptions."""
    protected_settings = api.get_settings(["OPENAI_API_KEY"])
    api_key = str(protected_settings.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        api.log("warning", "Voice message transcription skipped: OPENAI_API_KEY is not configured")
        return ""
    headers = {"Authorization": f"Bearer {api_key}"}
    files = {"file": ("voice.ogg", ogg_bytes, "audio/ogg")}
    data = {"model": "whisper-1"}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers=headers,
            files=files,
            data=data,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Whisper API transcription returned HTTP {response.status_code}")
        res = response.json()
        return str(res.get("text") or "").strip()


def _get_current_model(api) -> str:
    """Read the active model from parent settings.json."""
    settings_file = pathlib.Path(api.get_state_dir()).parent.parent.parent / "settings.json"
    if settings_file.exists():
        try:
            sett = json.loads(settings_file.read_text(encoding="utf-8"))
            return str(sett.get("OUROBOROS_MODEL") or "google/gemini-3.5-flash")
        except Exception:
            pass
    return "google/gemini-3.5-flash"


def _build_menu_settings(api, command_mode: str, lang: str = "en") -> tuple[str, list[list[dict]]]:
    """Return (header_text, inline_keyboard_rows) for the Settings panel."""
    t = _LOCALIZED_TEXTS[lang]
    header = t["settings_title"]
    silent_on = _is_silent_mode_enabled(_load_settings(api))
    silent_label = t["btn_silent_on"] if silent_on else t["btn_silent_off"]
    keyboard = [
        [
            {"text": t["btn_language"], "callback_data": "nav:language"},
        ],
        [
            {"text": t["btn_model"], "callback_data": "nav:model"},
            {"text": t["btn_budget"], "callback_data": "nav:budget"},
        ],
        [
            {"text": silent_label, "callback_data": "cmd_act:toggle_silent"},
        ],
        [{"text": t["btn_back"], "callback_data": "nav:menu"}]
    ]
    return header, keyboard


def _build_menu_model(api, command_mode: str, lang: str = "en") -> tuple[str, list[list[dict]]]:
    """Return model selection panel."""
    t = _LOCALIZED_TEXTS[lang]
    current_model = _get_current_model(api)
    header = t["model_title"].format(current_model=current_model)
    keyboard = []
    # NOTE: Model-switch buttons disabled by owner decision (v2.1.2).
    # Routing core settings mutations through inject_chat from Telegram
    # callbacks turns the bot into an owner-control surface — GPT-5.5 review
    # flagged this as inject_chat_minimization critical. Panel stays as a
    # read-only view of the current model; switch in Web UI → Settings.
    # if command_mode == _COMMAND_MODE_FULL:
    #     keyboard.append([
    #         {"text": "Gemini 3.5 Flash", "callback_data": "cmd_act:set_model:google/gemini-3.5-flash"},
    #         {"text": "Claude 3.5 Sonnet", "callback_data": "cmd_act:set_model:anthropic/claude-sonnet-4.6"},
    #     ])
    #     keyboard.append([
    #         {"text": "GPT-5.5 Pro", "callback_data": "cmd_act:set_model:openai/gpt-5.5-pro"},
    #         {"text": "GPT-5.5 Mini", "callback_data": "cmd_act:set_model:openai/gpt-5.5-mini"},
    #     ])
    keyboard.append([{"text": t["btn_back"], "callback_data": "nav:settings"}])
    return header, keyboard


def _build_menu_budget(api, command_mode: str, lang: str = "en") -> tuple[str, list[list[dict]]]:
    """Return spending limit panel."""
    t = _LOCALIZED_TEXTS[lang]
    spent_usd = 0.0
    state_file = pathlib.Path(api.get_state_dir()).parent.parent.parent / "state" / "state.json"
    if state_file.exists():
        try:
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
            spent_usd = float(state_data.get("spent_usd") or 0.0)
        except Exception:
            pass
            
    settings_file = pathlib.Path(api.get_state_dir()).parent.parent.parent / "settings.json"
    total_budget = 800.0
    if settings_file.exists():
        try:
            sett = json.loads(settings_file.read_text(encoding="utf-8"))
            total_budget = float(sett.get("TOTAL_BUDGET") or 800.0)
        except Exception:
            pass
            
    rem = max(0.0, total_budget - spent_usd)
    header = t["budget_title"].format(total_budget=total_budget, spent_usd=spent_usd, rem=rem)
    
    keyboard = []
    # NOTE: Budget-increase buttons disabled by owner decision (v2.1.2).
    # Same reason as set_model: injecting "please raise TOTAL_BUDGET" via
    # inject_chat from a remote Telegram button is owner-control through a
    # user-facing transport channel. Panel stays read-only; change budget in
    # Web UI → Settings → Advanced → Runtime Limits.
    # if command_mode == _COMMAND_MODE_FULL:
    #     keyboard.append([
    #         {"text": "+$10", "callback_data": "cmd_act:add_budget:10"},
    #         {"text": "+$50", "callback_data": "cmd_act:add_budget:50"},
    #         {"text": "+$100", "callback_data": "cmd_act:add_budget:100"},
    #     ])
    keyboard.append([{"text": t["btn_back"], "callback_data": "nav:settings"}])
    return header, keyboard


def _build_language_keyboard(lang: str = "en") -> tuple[str, list[list[dict]]]:
    """Return (header_text, inline_keyboard_rows) for language selection."""
    t = _LOCALIZED_TEXTS[lang]
    header = t["lang_title"]
    rows = [
        [
            {"text": t["lang_en"], "callback_data": "set_lang:en"},
            {"text": t["lang_ru"], "callback_data": "set_lang:ru"}
        ],
        [{"text": t["btn_back"], "callback_data": "nav:menu"}]
    ]
    return header, rows


def _make_settings_save(api):
    async def _settings_save(request):
        data = await request.json()
        allowed = {"TELEGRAM_CHAT_ID", "TELEGRAM_MAX_UPDATES_PER_POLL", "TELEGRAM_MIRROR_MODE", "TELEGRAM_COMMAND_MODE", "TELEGRAM_LANGUAGE", "TELEGRAM_SILENT_MODE"}
        payload = {key: data.get(key) for key in allowed if key in data}
        path = _state_file(api, "settings.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return JSONResponse({"ok": True, "message": "Telegram settings saved. Toggle the skill to restart polling."})
    return _settings_save


def _host_headers(api) -> Dict[str, str]:
    return {"X-Skill-Token": api.get_skill_token().use_in_request()}


def _target_chat(settings: Dict[str, Any], event: Dict[str, Any]) -> int:
    mirror_mode = str(settings.get("TELEGRAM_MIRROR_MODE") or "all").strip().lower()
    configured = str(settings.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if configured:
        try:
            chat_id = int(configured)
        except ValueError:
            return 0
        if mirror_mode == "all":
            # Mirror everything (web UI + Telegram) to the pinned chat
            return chat_id
        # telegram_only: only forward events that originate from Telegram transport
        transport = event.get("transport") if isinstance(event.get("transport"), dict) else {}
        if transport.get("kind") == "telegram":
            return chat_id
        return 0
    # No pinned chat configured — only forward events that originate from
    # a Telegram transport conversation so local UI events are never leaked.
    transport = event.get("transport") if isinstance(event.get("transport"), dict) else {}
    if transport.get("kind") != "telegram":
        return 0
    try:
        return int(transport.get("conversation_id") or 0)
    except (TypeError, ValueError):
        return 0


async def _inject(api, payload: Dict[str, Any]) -> None:
    port = os.environ.get("OUROBOROS_HOST_SERVICE_PORT", "8767")
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"http://127.0.0.1:{port}/chat/inject",
            headers=_host_headers(api),
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Host inject returned HTTP {response.status_code}")
    # A new user turn starts here — break the silent-mode chain so the next
    # outbound message begins a fresh bubble rather than overwriting the last.
    try:
        chat_id = int(payload.get("chat_id") or 0)
        if chat_id:
            _clear_silent_msg(api, chat_id)
    except (TypeError, ValueError):
        pass


def _load_offset(api) -> int:
    path = _state_file(api, "poll_offset.json")
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return int(data.get("offset") or 0)
    except Exception:
        pass
    return 0


def _save_offset(api, offset: int) -> None:
    path = _state_file(api, "poll_offset.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"offset": offset}), encoding="utf-8")
    tmp.replace(path)


def _extract_sender_label(sender: dict, fallback_chat_id: int) -> str:
    """Build a human-readable sender label from a Telegram user dict."""
    return (
        str(sender.get("username") or "").strip()
        or " ".join(
            str(part).strip()
            for part in (sender.get("first_name"), sender.get("last_name"))
            if part
        )
        or f"Telegram {sender.get('id') or fallback_chat_id}"
    )


def _is_bg_consciousness_active(api) -> bool:
    """Check if background consciousness is actively enabled in state.json."""
    state_file = pathlib.Path(api.get_state_dir()).parent.parent.parent / "state" / "state.json"
    if state_file.exists():
        try:
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
            return bool(state_data.get("bg_consciousness_enabled") or False)
        except Exception:
            pass
    return False


def _compile_status_text(api, lang: str = "en") -> str:
    """Generate a clean HTML metrics block from state/settings."""
    spent_usd = 0.0
    branch = "ouroboros"
    bg_enabled = False
    
    state_file = pathlib.Path(api.get_state_dir()).parent.parent.parent / "state" / "state.json"
    if state_file.exists():
        try:
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
            spent_usd = float(state_data.get("spent_usd") or 0.0)
            branch = str(state_data.get("current_branch") or "ouroboros")
            bg_enabled = bool(state_data.get("bg_consciousness_enabled") or False)
        except Exception:
            pass
            
    settings_file = pathlib.Path(api.get_state_dir()).parent.parent.parent / "settings.json"
    total_budget = 800.0
    if settings_file.exists():
        try:
            sett = json.loads(settings_file.read_text(encoding="utf-8"))
            total_budget = float(sett.get("TOTAL_BUDGET") or 800.0)
        except Exception:
            pass
            
    rem = max(0.0, total_budget - spent_usd)
    t = _LOCALIZED_TEXTS[lang]
    status_str = t["metrics_budget_status"].format(
        spent_usd=spent_usd,
        total_budget=total_budget,
        rem=rem,
        branch=branch,
        bg_status=t["bg_active_label"] if bg_enabled else t["bg_sleeping_label"]
    )
    return status_str


def _make_poller(api):
    async def poller() -> None:
        protected_settings = api.get_settings(["TELEGRAM_BOT_TOKEN"])
        local_settings = _load_settings(api)
        client = TelegramClient(protected_settings.get("TELEGRAM_BOT_TOKEN", ""))
        pinned_chat = str(local_settings.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        max_updates = _setting_int(local_settings, "TELEGRAM_MAX_UPDATES_PER_POLL", 20, minimum=1, maximum=100)
        command_mode = str(local_settings.get("TELEGRAM_COMMAND_MODE") or _COMMAND_MODE_STRICT).strip().lower()
        if command_mode not in _VALID_COMMAND_MODES:
            command_mode = _COMMAND_MODE_STRICT
        lang = str(local_settings.get("TELEGRAM_LANGUAGE") or "en").strip().lower()
        if lang not in ("en", "ru"):
            lang = "en"
        offset = _load_offset(api)

        # Validate token and configure commands before entering poll loop
        try:
            await client.call("getMe")
            
            # Set the command menu list for the blue bottom-left Menu button
            try:
                await client.call("setMyCommands", data={
                    "commands": json.dumps([
                        {"command": "menu", "description": "Interactive panel / Меню"},
                        {"command": "language", "description": "Select language / Выбор языка"},
                        {"command": "status", "description": "Request status / Статус"},
                        {"command": "help", "description": "Usage guide / Справка"}
                    ])
                })
                api.log("info", "Telegram bot commands configured successfully")
            except Exception as exc:
                api.log("warning", f"Failed to set Telegram bot commands: {exc}")
                
            api.log("info", f"Telegram poller started (command_mode={command_mode}, lang={lang})")
        except Exception as exc:
            api.log("error", f"Telegram token validation failed: {exc}")
            raise

        while True:
            try:
                updates = await client.get_updates(offset)
                if updates:
                    local_settings = _load_settings(api)
                    command_mode = str(local_settings.get("TELEGRAM_COMMAND_MODE") or _COMMAND_MODE_STRICT).strip().lower()
                    if command_mode not in _VALID_COMMAND_MODES:
                        command_mode = _COMMAND_MODE_STRICT
                    lang = str(local_settings.get("TELEGRAM_LANGUAGE") or "en").strip().lower()
                    if lang not in ("en", "ru"):
                        lang = "en"

                for update in updates[:max_updates]:
                    update_id = int(update.get("update_id") or 0)
                    if update_id >= offset:
                        offset = update_id + 1

                    # --- Handle callback queries (inline button presses) ---
                    callback_query = update.get("callback_query")
                    if callback_query:
                        cb_id = str(callback_query.get("id") or "")
                        cb_data = str(callback_query.get("data") or "").strip()
                        cb_message = callback_query.get("message") or {}
                        cb_message_id = int(cb_message.get("message_id") or 0)
                        cb_chat = cb_message.get("chat") or {}
                        cb_chat_id = int(cb_chat.get("id") or 0)
                        cb_sender = callback_query.get("from") or {}
                        if pinned_chat and str(cb_chat_id) != pinned_chat:
                            await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang]["not_authorized"])
                            continue

                        # --- Dynamic Tab Navigation (Category 1) ---
                        if cb_data.startswith("nav:"):
                            target = cb_data.split(":", 1)[1]
                            await client.answer_callback_query(cb_id)
                            if target == "menu":
                                header, keyboard = _build_menu_keyboard(command_mode, lang)
                                await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                            elif target == "status":
                                info_text = _compile_status_text(api, lang)
                                header, keyboard = _build_menu_status(command_mode, lang, info_text)
                                await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                            elif target == "mind":
                                bg_enabled = _is_bg_consciousness_active(api)
                                header, keyboard = _build_menu_mind(command_mode, lang, bg_enabled)
                                await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                            elif target == "language":
                                header, keyboard = _build_language_keyboard(lang)
                                await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                            elif target == "settings":
                                header, keyboard = _build_menu_settings(api, command_mode, lang)
                                await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                            elif target == "model":
                                header, keyboard = _build_menu_model(api, command_mode, lang)
                                await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                            elif target == "budget":
                                header, keyboard = _build_menu_budget(api, command_mode, lang)
                                await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                            continue

                        # --- Command Actions / Control (Category 2) ---
                        if cb_data.startswith("cmd_act:"):
                            action = cb_data.split(":", 1)[1]
                            
                            if action == "update_status":
                                await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang]["updating_status"])
                                info_text = _compile_status_text(api, lang)
                                header, keyboard = _build_menu_status(command_mode, lang, info_text)
                                await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                                continue

                            elif action == "toggle_silent":
                                # Toggle TELEGRAM_SILENT_MODE and refresh the Settings panel.
                                # This is a display preference (no LLM injection), so it is
                                # allowed in every command_mode including strict.
                                local_settings = _load_settings(api)
                                currently_on = _is_silent_mode_enabled(local_settings)
                                new_value = "off" if currently_on else "on"
                                local_settings["TELEGRAM_SILENT_MODE"] = new_value
                                path = _state_file(api, "settings.json")
                                path.parent.mkdir(parents=True, exist_ok=True)
                                path.write_text(json.dumps(local_settings, ensure_ascii=False, indent=2), encoding="utf-8")
                                # Clear any stale tracked message id for this chat so the
                                # next outbound starts a fresh bubble in either direction.
                                _clear_silent_msg(api, cb_chat_id)
                                toast_key = "silent_toggled_on" if new_value == "on" else "silent_toggled_off"
                                await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang][toast_key])
                                header, keyboard = _build_menu_settings(api, command_mode, lang)
                                await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                                continue
                                
                            elif action == "bg_thoughts":
                                await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang]["extracting_thoughts"])
                                bg_enabled = _is_bg_consciousness_active(api)
                                thoughts = _load_recent_thoughts(api)
                                header, keyboard = _build_menu_mind(command_mode, lang, bg_enabled, thoughts)
                                await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                                continue
                                
                            elif action in ("bg_start", "bg_stop"):
                                if command_mode != _COMMAND_MODE_FULL:
                                    await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang]["restricted_safe"])
                                    continue
                                await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang]["injecting_consciousness"])
                                translated = "start background consciousness" if action == "bg_start" else "stop background consciousness"
                                sender_name = _extract_sender_label(cb_sender, cb_chat_id)
                                sender_label = f"Telegram ({sender_name})"
                                await _inject(api, {
                                    "text": translated,
                                    "chat_id": cb_chat_id,
                                    "user_id": int(cb_sender.get("id") or cb_chat_id or 1),
                                    "source": "telegram-bridge",
                                    "sender_label": sender_label,
                                    "transport": {
                                        "kind": "telegram",
                                        "conversation_id": str(cb_chat_id),
                                        "sender_label": sender_label,
                                    },
                                    "image_base64": "",
                                    "image_mime": "",
                                    "image_caption": "",
                                })
                                # Give it a tiny moment to commit setting then refresh mind panel
                                await asyncio.sleep(0.8)
                                bg_enabled = _is_bg_consciousness_active(api)
                                header, keyboard = _build_menu_mind(command_mode, lang, bg_enabled)
                                await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                                continue

                            # NOTE: set_model: and add_budget: callback handlers disabled
                            # (v2.1.2). Buttons that produced these callbacks are also
                            # commented out in _build_menu_model / _build_menu_budget.
                            # Reason: routing settings.json mutations through inject_chat
                            # from a remote Telegram button is owner-control through a
                            # user-facing transport channel (GPT-5.5 critical finding,
                            # inject_chat_minimization). Stale callbacks fall through to
                            # the unknown-action path and are silently ignored.
                            #
                            # elif action.startswith("set_model:"):
                            #     if command_mode != _COMMAND_MODE_FULL:
                            #         await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang]["restricted_safe"])
                            #         continue
                            #     model_name = action.split(":", 1)[1]
                            #     await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang]["requesting_model"].format(model=model_name))
                            #     sender_name = _extract_sender_label(cb_sender, cb_chat_id)
                            #     sender_label = f"Telegram ({sender_name})"
                            #     await _inject(api, {
                            #         "text": f"Please change the main Ouroboros model (OUROBOROS_MODEL, and also OUROBOROS_MODEL_CODE since the user wants a powerful editing model) to '{model_name}' inside settings.json, apply settings, and let me know.",
                            #         "chat_id": cb_chat_id,
                            #         "user_id": int(cb_sender.get("id") or cb_chat_id or 1),
                            #         "source": "telegram-bridge",
                            #         "sender_label": sender_label,
                            #         "transport": {
                            #             "kind": "telegram",
                            #             "conversation_id": str(cb_chat_id),
                            #             "sender_label": sender_label,
                            #         },
                            #         "image_base64": "",
                            #         "image_mime": "",
                            #         "image_caption": "",
                            #     })
                            #     await asyncio.sleep(0.8)
                            #     header, keyboard = _build_menu_model(api, command_mode, lang)
                            #     await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                            #     continue
                            #
                            # elif action.startswith("add_budget:"):
                            #     if command_mode != _COMMAND_MODE_FULL:
                            #         await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang]["restricted_safe"])
                            #         continue
                            #     amount_str = action.split(":", 1)[1]
                            #     try:
                            #         amount = float(amount_str)
                            #     except ValueError:
                            #         amount = 10.0
                            #     await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang]["requesting_budget"].format(amount=amount))
                            #     settings_file = pathlib.Path(api.get_state_dir()).parent.parent.parent / "settings.json"
                            #     current_budget = 800.0
                            #     if settings_file.exists():
                            #         try:
                            #             sett = json.loads(settings_file.read_text(encoding="utf-8"))
                            #             current_budget = float(sett.get("TOTAL_BUDGET") or 800.0)
                            #         except Exception:
                            #             pass
                            #     new_budget = current_budget + amount
                            #     sender_name = _extract_sender_label(cb_sender, cb_chat_id)
                            #     sender_label = f"Telegram ({sender_name})"
                            #     await _inject(api, {
                            #         "text": f"Please update settings.json to increase the TOTAL_BUDGET limit from {current_budget} to {new_budget} (adding {amount}), and reload the settings so it takes effect.",
                            #         "chat_id": cb_chat_id,
                            #         "user_id": int(cb_sender.get("id") or cb_chat_id or 1),
                            #         "source": "telegram-bridge",
                            #         "sender_label": sender_label,
                            #         "transport": {
                            #             "kind": "telegram",
                            #             "conversation_id": str(cb_chat_id),
                            #             "sender_label": sender_label,
                            #         },
                            #         "image_base64": "",
                            #         "image_mime": "",
                            #         "image_caption": "",
                            #     })
                            #     await asyncio.sleep(0.8)
                            #     header, keyboard = _build_menu_budget(api, command_mode, lang)
                            #     await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                            #     continue

                        # --- Handle language selection buttons ---
                        if cb_data.startswith("set_lang:"):
                            new_lang = cb_data.split(":", 1)[1]
                            if new_lang in ("en", "ru"):
                                local_settings = _load_settings(api)
                                local_settings["TELEGRAM_LANGUAGE"] = new_lang
                                path = _state_file(api, "settings.json")
                                path.parent.mkdir(parents=True, exist_ok=True)
                                path.write_text(json.dumps(local_settings, ensure_ascii=False, indent=2), encoding="utf-8")
                                
                                lang = new_lang
                                await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang]["lang_changed"])
                                
                                # Smoothly return to menu panel in updated language
                                header, keyboard = _build_menu_keyboard(command_mode, lang)
                                await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                                continue

                        # Look up the button in the safe callback map — only
                        # pre-translated natural-language text can reach _inject.
                        mapping = _CALLBACK_MAP.get(cb_data)
                        if not mapping:
                            await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang]["unknown_command"])
                            continue
                        translated_text, required_mode = mapping
                        # Mode hierarchy: full_access > safe_commands > strict
                        mode_ok = (
                            command_mode == _COMMAND_MODE_FULL
                            or (command_mode == _COMMAND_MODE_SAFE and required_mode == _COMMAND_MODE_SAFE)
                        )
                        if not mode_ok:
                            await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang]["restricted_current"])
                            continue
                        await client.answer_callback_query(cb_id, text=f"Sending: {translated_text}")
                        sender_name = _extract_sender_label(cb_sender, cb_chat_id)
                        sender_label = f"Telegram ({sender_name})"
                        await _inject(api, {
                            "text": translated_text,
                            "chat_id": cb_chat_id,
                            "user_id": int(cb_sender.get("id") or cb_chat_id or 1),
                            "source": "telegram-bridge",
                            "sender_label": sender_label,
                            "transport": {
                                "kind": "telegram",
                                "conversation_id": str(cb_chat_id),
                                "sender_label": sender_label,
                            },
                            "image_base64": "",
                            "image_mime": "",
                            "image_caption": "",
                        })
                        continue

                    # --- Handle regular messages ---
                    message = update.get("message") or {}
                    chat = message.get("chat") or {}
                    sender = message.get("from") or {}
                    chat_id = int(chat.get("id") or 0)
                    if pinned_chat and str(chat_id) != pinned_chat:
                        continue
                    text = str(message.get("text") or message.get("caption") or "").strip()
                    caption = str(message.get("caption") or "").strip()

                    # Handle /menu command locally — always allowed
                    cleaned_text = text.lower().strip()
                    is_menu_cmd = cleaned_text == "/menu" or cleaned_text.startswith("/menu ") or (cleaned_text.startswith("/menu@") and cleaned_text.split("@")[0] == "/menu")
                    if is_menu_cmd:
                        header, keyboard = _build_menu_keyboard(command_mode, lang)
                        if keyboard:
                            await client.send_message_with_inline_keyboard(chat_id, header, keyboard)
                        else:
                            await client.send_message(chat_id, header)
                        continue

                    # Handle /language command locally — always allowed
                    is_lang_cmd = cleaned_text == "/language" or cleaned_text.startswith("/language ") or (cleaned_text.startswith("/language@") and cleaned_text.split("@")[0] == "/language")
                    if is_lang_cmd:
                        header, keyboard = _build_language_keyboard(lang)
                        await client.send_message_with_inline_keyboard(chat_id, header, keyboard)
                        continue

                    # Handle /help command locally — always allowed
                    is_help_cmd = cleaned_text == "/help" or cleaned_text.startswith("/help ") or (cleaned_text.startswith("/help@") and cleaned_text.split("@")[0] == "/help")
                    if is_help_cmd:
                        help_text = _LOCALIZED_TEXTS[lang]["help_text"]
                        await client.send_message(chat_id, help_text)
                        continue

                    # --- Handle voice messages (Category 4) ---
                    voice = message.get("voice")
                    if voice:
                        file_id = str(voice.get("file_id") or "").strip()
                        if file_id:
                            await client.send_chat_action(chat_id, "record_voice")
                            try:
                                api.log("info", f"Downloading voice message: {file_id}")
                                ogg_bytes = await client.download_file(file_id)
                                
                                await client.send_chat_action(chat_id, "typing")
                                api.log("info", "Transcribing audio via OpenAI Whisper API...")
                                voice_text = await _transcribe_voice(api, ogg_bytes)
                                
                                if voice_text:
                                    api.log("info", f"Whisper transcription success: '{voice_text}'")
                                    await client.send_message(chat_id, f"🎙 **[Voice transcribed]:**\n_\"{voice_text}\"_")
                                    text = voice_text
                                else:
                                    await client.send_message(chat_id, _LOCALIZED_TEXTS[lang]["blank_voice"])
                                    continue
                            except Exception as exc:
                                api.log("error", f"Voice message transcription failed: {exc}")
                                await client.send_message(chat_id, _LOCALIZED_TEXTS[lang]["voice_error"].format(exc=exc))
                                continue

                    # Translate commands to safe natural-language text.
                    # _translate_command returns None when the command is rejected.
                    safe_text = _translate_command(text, command_mode)
                    safe_caption = _translate_command(caption, command_mode) if caption else caption
                    if safe_text is None or safe_caption is None:
                        if command_mode == _COMMAND_MODE_STRICT:
                            await client.send_message(
                                chat_id,
                                _LOCALIZED_TEXTS[lang]["slash_blocked_strict"],
                            )
                        else:
                            await client.send_message(
                                chat_id,
                                _LOCALIZED_TEXTS[lang]["slash_blocked_mode"],
                            )
                        continue

                    photos = message.get("photo") or []
                    image_base64 = ""
                    image_mime = ""
                    if photos:
                        file_id = str((photos[-1] or {}).get("file_id") or "").strip()
                        if file_id:
                            image_base64, image_mime = await client.download_photo(file_id)
                    if not safe_text and not image_base64:
                        continue
                    sender_name = _extract_sender_label(sender, chat_id)
                    sender_label = f"Telegram ({sender_name})"
                    await _inject(api, {
                        "text": safe_text,
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
                        "image_caption": safe_caption,
                    })
                if updates:
                    _save_offset(api, offset)
                await asyncio.sleep(0.1)
            except Exception as exc:
                api.log("warning", f"Telegram poller transient error: {exc}")
                await asyncio.sleep(5)
    return poller


def _make_outbound(api):
    async def handle(event: Dict[str, Any]) -> None:
        try:
            protected_settings = api.get_settings(["TELEGRAM_BOT_TOKEN"])
            local_settings = _load_settings(api)
            client = TelegramClient(protected_settings.get("TELEGRAM_BOT_TOKEN", ""))
            chat_id = _target_chat(local_settings, event)
            if not chat_id:
                return
            text = str(event.get("text") or "").strip()
            if not text:
                return

            silent_on = _is_silent_mode_enabled(local_settings)
            tracked_msg_id = _get_silent_msg(api, chat_id) if silent_on else 0

            # Silent mode: try to edit the previously tracked message in-place.
            # editMessageText returns False on any failure (too old, deleted,
            # identical content, parse error) so we fall back to sendMessage.
            if silent_on and tracked_msg_id:
                edited = await client.edit_message_text(chat_id, tracked_msg_id, text)
                if not edited:
                    edited = await client.edit_message_text(chat_id, tracked_msg_id, text, parse_mode="")
                if edited:
                    return
                # Edit failed (likely too old or already identical) — clear
                # tracking and fall through to sendMessage path.
                _clear_silent_msg(api, chat_id)

            try:
                msg_id = await client.send_message(chat_id, text)
            except Exception as format_exc:
                api.log("warning", f"Telegram outbound HTML send failed ({format_exc}), retrying with plain text...")
                msg_id = await client.send_message(chat_id, text, parse_mode="")

            if silent_on and msg_id:
                _set_silent_msg(api, chat_id, msg_id)
        except Exception as exc:
            api.log("error", f"Telegram outbound error: {exc}")
    return handle


def _make_typing(api):
    async def handle(event: Dict[str, Any]) -> None:
        try:
            protected_settings = api.get_settings(["TELEGRAM_BOT_TOKEN"])
            local_settings = _load_settings(api)
            client = TelegramClient(protected_settings.get("TELEGRAM_BOT_TOKEN", ""))
            chat_id = _target_chat(local_settings, event)
            if chat_id:
                await client.send_chat_action(chat_id, "typing")
        except Exception as exc:
            api.log("error", f"Telegram typing error: {exc}")
    return handle


def _make_photo(api):
    async def handle(event: Dict[str, Any]) -> None:
        try:
            protected_settings = api.get_settings(["TELEGRAM_BOT_TOKEN"])
            local_settings = _load_settings(api)
            client = TelegramClient(protected_settings.get("TELEGRAM_BOT_TOKEN", ""))
            chat_id = _target_chat(local_settings, event)
            image_base64 = str(event.get("image_base64") or "").strip()
            if chat_id and image_base64:
                # Media cannot replace a text bubble in Telegram — break the
                # silent chain so the next outbound starts a fresh message.
                _clear_silent_msg(api, chat_id)
                await client.send_photo(
                    chat_id,
                    image_base64,
                    caption=str(event.get("caption") or ""),
                    mime=str(event.get("mime") or "image/png"),
                )
        except Exception as exc:
            api.log("error", f"Telegram photo error: {exc}")
    return handle


def _make_video(api):
    async def handle(event: Dict[str, Any]) -> None:
        try:
            protected_settings = api.get_settings(["TELEGRAM_BOT_TOKEN"])
            local_settings = _load_settings(api)
            client = TelegramClient(protected_settings.get("TELEGRAM_BOT_TOKEN", ""))
            chat_id = _target_chat(local_settings, event)
            video_base64 = str(event.get("video_base64") or "").strip()
            if chat_id and video_base64:
                # Media cannot replace a text bubble — reset silent tracking.
                _clear_silent_msg(api, chat_id)
                caption = str(event.get("caption") or "")
                mime = str(event.get("mime") or "video/mp4")
                import base64 as _base64
                files = {"video": ("video.mp4", _base64.b64decode(video_base64), mime)}
                data = {"chat_id": str(chat_id), "caption": markdown_to_telegram_html(caption), "parse_mode": "HTML"}
                await client.call("sendVideo", data=data, files=files, timeout=40)
        except Exception as exc:
            api.log("error", f"Telegram video error: {exc}")
    return handle


def register(api):
    api.register_supervised_task("poller", _make_poller(api), restart_policy="on_failure", max_restarts=10)
    api.subscribe_event("chat.outbound", _make_outbound(api))
    api.subscribe_event("chat.typing", _make_typing(api))
    api.subscribe_event("chat.photo", _make_photo(api))
    try:
        api.subscribe_event("chat.video", _make_video(api))
    except Exception as exc:
        api.log("warning", f"Could not subscribe to chat.video: {exc}")
    api.register_route("settings/save", handler=_make_settings_save(api), methods=("POST",))
    api.register_settings_section(
        "telegram",
        title="Telegram Bridge",
        schema={
            "components": [
                {
                    "type": "markdown",
                    "text": (
                        "Set TELEGRAM_BOT_TOKEN in Settings → Secrets, grant it to this skill, then configure the options below.\n\n"
                        "**Command mode**: controls which slash commands can be sent from Telegram. "
                        "Use `/menu` in Telegram to see available commands as inline buttons.\n\n"
                        "**Mirror mode**: *all* mirrors every chat message (including web UI) to Telegram — requires Chat ID. "
                        "*Telegram only* mirrors only Telegram-originated conversations."
                    ),
                },
                {
                    "type": "form",
                    "route": "settings/save",
                    "method": "POST",
                    "fields": [
                        {"name": "TELEGRAM_LANGUAGE", "label": "Language / Язык", "type": "select",
                         "options": [
                             {"value": "en", "label": "🇬🇧 English"},
                             {"value": "ru", "label": "🇷🇺 Русский"},
                         ],
                         "placeholder": "en"},
                        {"name": "TELEGRAM_COMMAND_MODE", "label": "Command mode", "type": "select",
                         "options": [
                             {"value": "strict", "label": "Strict — block all slash commands from Telegram"},
                             {"value": "safe_commands", "label": "Safe — allow /status, /bg status only"},
                             {"value": "full_access", "label": "Full access — safe commands + bg start/stop"},
                         ],
                         "placeholder": "strict"},
                        {"name": "TELEGRAM_MIRROR_MODE", "label": "Mirror mode", "type": "select",
                         "options": [
                             {"value": "all", "label": "Mirror all messages (web + Telegram)"},
                             {"value": "telegram_only", "label": "Telegram conversations only"},
                         ],
                         "placeholder": "all"},
                        {"name": "TELEGRAM_CHAT_ID", "label": "Telegram Chat ID", "type": "text", "placeholder": "required for 'all' mode"},
                        {"name": "TELEGRAM_MAX_UPDATES_PER_POLL", "label": "Max updates per poll", "type": "number", "placeholder": "20"},
                        {"name": "TELEGRAM_SILENT_MODE", "label": "Silent mode (edit-in-place)", "type": "select",
                         "options": [
                             {"value": "off", "label": "Off — each thought is a new message"},
                             {"value": "on", "label": "On — replace the previous thought in-place"},
                         ],
                         "placeholder": "off"},
                    ],
                    "submit_label": "Save Telegram settings",
                }
            ]
        },
    )
