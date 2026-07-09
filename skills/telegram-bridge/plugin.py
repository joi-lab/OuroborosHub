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
from .lib.telegram_state import (
    _state_file, _load_settings, _save_settings_dict, _is_silent_mode_enabled,
    _get_silent_msg, _set_silent_msg, _clear_silent_msg, _subagent_cards_enabled,
    _mirror_progress_enabled, _render_subagent_card, _data_dir,
    _set_pending_input, _get_pending_input, _clear_pending_input,
)
from .lib.telegram_health import _collect_health, _build_menu_tasks
from .lib.telegram_notifier import _make_notifier

_SLASH_COMMAND_RE = re.compile(r"^\s*/[A-Za-z]")

# In strict/safe modes slash commands are still controlled locally. In
# full_access mode a reviewed+granted chat transport is allowed to forward the
# same raw owner commands that the local UI accepts.
_COMMAND_TRANSLATIONS: dict[str, str] = {
    "/status": "/status",
    "/bg status": "/bg status",
    "/bg start": "/bg start",
    "/bg stop": "/bg stop",
    "/bg": "/bg",
}

_COMMAND_MODE_STRICT = "strict"
_COMMAND_MODE_SAFE = "safe_commands"
_COMMAND_MODE_FULL = "full_access"
_VALID_COMMAND_MODES = frozenset({_COMMAND_MODE_STRICT, _COMMAND_MODE_SAFE, _COMMAND_MODE_FULL})


# Which translation keys are available in safe mode (full_access forwards raw)
_SAFE_TRANSLATION_KEYS = frozenset({"/status", "/bg status", "/bg"})

# Callback data → (translated_text, minimum_required_mode) for inline keyboard
# buttons. These are intentionally non-slash strings so no slash command ever
# reaches _inject from a button press.
_CALLBACK_MAP: dict[str, tuple[str, str]] = {
    "cmd:status":    ("show status", _COMMAND_MODE_SAFE),
    "cmd:bg_status": ("background consciousness status", _COMMAND_MODE_SAFE),
    "cmd:bg_start":  ("start background consciousness", _COMMAND_MODE_FULL),
    "cmd:bg_stop":   ("stop background consciousness", _COMMAND_MODE_FULL),
}


def _setting_int(settings: Dict[str, Any], key: str, default: int, *, minimum: int = 1, maximum: int = 100) -> int:
    try:
        value = int(settings.get(key) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _translate_command(text: str, command_mode: str) -> str | None:
    """Return allowed chat text for a Telegram command, or None to reject."""
    if not _SLASH_COMMAND_RE.match(str(text or "")):
        return text  # Not a slash command — pass through unchanged
    normalized = str(text or "").strip().lower()
    if command_mode == _COMMAND_MODE_STRICT:
        return None  # All slash commands blocked
    if command_mode == _COMMAND_MODE_FULL:
        return str(text or "").strip()
    # Determine which translations are available for this mode
    for cmd_key in sorted(_SAFE_TRANSLATION_KEYS, key=len, reverse=True):
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
            {"text": "📋 Задачи" if lang == "ru" else "📋 Tasks", "callback_data": "nav:tasks"},
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
    progress_file = _data_dir(api) / "logs" / "progress.jsonl"
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
    settings_file = _data_dir(api) / "settings.json"
    if settings_file.exists():
        try:
            sett = json.loads(settings_file.read_text(encoding="utf-8"))
            val = sett.get("OUROBOROS_MODEL")
            if val:
                return str(val)
        except Exception:
            pass
    return "unavailable"


# Models offered as buttons when TELEGRAM_MODEL_CHOICES is not set. Model IDs go
# stale fast, so the real list is owner-configurable (a setting), not hardcoded.
_DEFAULT_MODEL_CHOICES = (
    "anthropic/claude-opus-4.8",
    "anthropic/claude-sonnet-4.6",
    "openai/gpt-5.5",
    "google/gemini-3.5-flash",
)


def _get_current_budget(api) -> float:
    """Read TOTAL_BUDGET from parent settings.json (0.0 if unset/unreadable)."""
    settings_file = _data_dir(api) / "settings.json"
    if settings_file.exists():
        try:
            sett = json.loads(settings_file.read_text(encoding="utf-8"))
            val = sett.get("TOTAL_BUDGET")
            if val is not None:
                return float(val)
        except Exception:
            pass
    return 0.0


def _model_choices(api) -> list:
    """Models shown as buttons: owner-set TELEGRAM_MODEL_CHOICES (comma-separated)
    or the default list. Empty/garbage entries are dropped."""
    raw = str(_load_settings(api).get("TELEGRAM_MODEL_CHOICES") or "").strip()
    if raw:
        items = [m.strip() for m in raw.split(",") if m.strip()]
        if items:
            return items
    return list(_DEFAULT_MODEL_CHOICES)


def _model_change_command(model_id: str, lang: str = "en") -> str:
    """Owner-facing NL command the Model button injects. The skill never writes
    the core settings.json itself (path-confinement) — it forwards an owner
    request through the already-approved inject_chat path and the agent applies
    the change via the host's guarded settings flow."""
    return (
        f"Смени основную модель (OUROBOROS_MODEL) на {model_id}, остальные слоты не трогай."
        if lang == "ru"
        else f"Change the main model (OUROBOROS_MODEL) to {model_id}; leave the other slots unchanged."
    )


def _budget_change_command(new_budget: float, lang: str = "en") -> str:
    """Owner-facing NL command the Budget button injects (absolute target, the
    skill having read the current value). Applied by the agent, not written by
    the skill — same path-confinement rationale as _model_change_command."""
    return (
        f"Подними общий бюджет (TOTAL_BUDGET) до ${new_budget:.2f}."
        if lang == "ru"
        else f"Set the total budget (TOTAL_BUDGET) to ${new_budget:.2f}."
    )


def _parse_budget_amount(text: str):
    """Parse an owner-typed custom budget amount ($). Tolerates a leading $,
    spaces, and comma decimals. Returns a positive float capped at a sane max,
    or None if it is not a valid number — the value is only ever injected as an
    owner NL request, never written to settings.json directly."""
    raw = str(text or "").strip().lstrip("$").replace(" ", "")
    # Disambiguate a single comma: '250,50' (RU decimal) vs '1,000' (thousands).
    # A lone comma with 1-2 trailing digits and no dot is a decimal point;
    # otherwise commas are thousands separators and are stripped.
    if raw.count(",") == 1 and "." not in raw and len(raw.split(",")[1]) in (1, 2):
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        val = round(float(raw), 2)   # round BEFORE validating so $0.001 → 0.00 is rejected
    except (TypeError, ValueError):
        return None
    if not (0 < val <= 1_000_000):
        return None
    return val


def _bot_commands(command_mode: str) -> list:
    """Telegram '/' menu command list. Owner-control commands appear only in
    full_access — they already forward via the raw-command path, so listing them
    here just makes them discoverable/tappable (no new authority)."""
    cmds = [
        {"command": "menu", "description": "Interactive panel / Меню"},
        {"command": "language", "description": "Select language / Выбор языка"},
        {"command": "status", "description": "Request status / Статус"},
        {"command": "help", "description": "Usage guide / Справка"},
    ]
    if command_mode == _COMMAND_MODE_FULL:
        cmds += [
            {"command": "evolve", "description": "Start an evolution campaign / Запустить эволюцию"},
            {"command": "bg", "description": "Background consciousness on/off / Фоновое сознание"},
            {"command": "review", "description": "Run a self-review / Само-ревью"},
            {"command": "restart", "description": "Restart the agent / Перезапуск"},
            {"command": "panic", "description": "Emergency stop / Аварийный стоп"},
        ]
    return cmds


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
            {"text": silent_label, "callback_data": "cmd_act:toggle_silent"},
        ],
        [
            {"text": ("🤖 Модель" if lang == "ru" else "🤖 Model"), "callback_data": "nav:model"},
            {"text": ("💰 Бюджет" if lang == "ru" else "💰 Budget"), "callback_data": "nav:budget"},
        ],
        [{"text": t["btn_back"], "callback_data": "nav:menu"}]
    ]
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


def _build_model_keyboard(api, lang: str = "en") -> tuple[str, list[list[dict]]]:
    """Model picker: one button per TELEGRAM_MODEL_CHOICES entry (✓ on the active
    one). callback_data is the INDEX, never the model id, so it stays under
    Telegram's 64-byte callback_data cap even for long slugs."""
    current = _get_current_model(api)
    choices = _model_choices(api)
    header = ("🤖 Выбор модели\nТекущая: " if lang == "ru" else "🤖 Choose model\nCurrent: ") + current
    rows = []
    for idx, model_id in enumerate(choices):
        short = model_id.split("/", 1)[-1]
        mark = "✓ " if model_id == current else ""
        rows.append([{"text": f"{mark}{short}", "callback_data": f"set_model:{idx}"}])
    rows.append([{"text": _LOCALIZED_TEXTS[lang]["btn_back"], "callback_data": "nav:settings"}])
    return header, rows


def _build_budget_keyboard(api, lang: str = "en") -> tuple[str, list[list[dict]]]:
    """Budget picker: bump the current TOTAL_BUDGET by a preset increment."""
    current = _get_current_budget(api)
    header = ("💰 Бюджет (TOTAL_BUDGET)\nТекущий: $" if lang == "ru" else "💰 Budget (TOTAL_BUDGET)\nCurrent: $") + f"{current:.2f}"
    rows = [
        [
            {"text": "+$50", "callback_data": "set_budget:50"},
            {"text": "+$100", "callback_data": "set_budget:100"},
            {"text": "+$500", "callback_data": "set_budget:500"},
        ],
        [{"text": ("✏️ Своя сумма" if lang == "ru" else "✏️ Custom amount"), "callback_data": "set_budget:custom"}],
        [{"text": _LOCALIZED_TEXTS[lang]["btn_back"], "callback_data": "nav:settings"}],
    ]
    return header, rows


def _make_settings_save(api):
    async def _settings_save(request):
        data = await request.json()
        allowed = {"TELEGRAM_CHAT_ID", "TELEGRAM_MAX_UPDATES_PER_POLL", "TELEGRAM_MIRROR_MODE", "TELEGRAM_COMMAND_MODE", "TELEGRAM_LANGUAGE", "TELEGRAM_SILENT_MODE", "TELEGRAM_SUBAGENT_CARDS", "TELEGRAM_MIRROR_PROGRESS", "TELEGRAM_NOTIFY_TASKS", "TELEGRAM_NOTIFY_BUDGET", "TELEGRAM_MODEL_CHOICES"}
        payload = {key: data.get(key) for key in allowed if key in data}
        path = _state_file(api, "settings.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Merge into existing settings rather than overwrite, so a partial save
        # (e.g. setting only TELEGRAM_COMMAND_MODE) never wipes a pinned chat or
        # other preferences on a re-run.
        current: Dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    current = loaded
            except Exception:
                current = {}
        current.update(payload)
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
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
    settings = _load_settings(api)
    pinned_chat = str(settings.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not pinned_chat:
        api.log("warning", "Host inject refused: TELEGRAM_CHAT_ID is not configured or bound.")
        return
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
    state_file = _data_dir(api) / "state" / "state.json"
    if state_file.exists():
        try:
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
            return bool(state_data.get("bg_consciousness_enabled") or False)
        except Exception:
            pass
    return False


def _compile_status_text(api, lang: str = "en") -> str:
    """Generate a clean HTML metrics block from state/settings."""
    spent_usd = None
    branch = "unavailable"
    bg_enabled = None
    
    state_file = _data_dir(api) / "state" / "state.json"
    if state_file.exists():
        try:
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
            if state_data.get("spent_usd") is not None:
                spent_usd = float(state_data["spent_usd"])
            branch = str(state_data.get("current_branch") or "unavailable")
            if state_data.get("bg_consciousness_enabled") is not None:
                bg_enabled = bool(state_data["bg_consciousness_enabled"])
        except Exception:
            pass
            
    settings_file = _data_dir(api) / "settings.json"
    total_budget = None
    if settings_file.exists():
        try:
            sett = json.loads(settings_file.read_text(encoding="utf-8"))
            if sett.get("TOTAL_BUDGET") is not None:
                total_budget = float(sett["TOTAL_BUDGET"])
        except Exception:
            pass
            
    t = _LOCALIZED_TEXTS[lang]
    
    bg_status_raw = "unavailable"
    if bg_enabled is not None:
        bg_status_raw = t["bg_active_label"] if bg_enabled else t["bg_sleeping_label"]
        
    spent_spec = "unavailable" if spent_usd is None else f"{spent_usd:.4f}"
    total_spec = "unavailable" if total_budget is None else f"{total_budget:.2f}"
    rem_spec = "unavailable" if (spent_usd is None or total_budget is None) else f"{max(0.0, total_budget - spent_usd):.4f}"
    
    template = t["metrics_budget_status"]
    template = template.replace("{spent_usd:.4f}", "{spent_usd_str}")
    template = template.replace("{total_budget:.2f}", "{total_budget_str}")
    template = template.replace("{rem:.4f}", "{rem_str}")
    
    status_str = template.format(
        spent_usd_str=spent_spec,
        total_budget_str=total_spec,
        rem_str=rem_spec,
        branch=branch,
        bg_status=bg_status_raw
    )
    # Current model folded into the status view (the standalone Model panel was a
    # read-only display and is removed). Change the model in the Web UI settings.
    status_str += f"\n🤖 {_get_current_model(api)}"
    status_str += "\n" + _collect_health(api, lang)
    return status_str


def _make_poller(api):
    async def poller() -> None:
        protected_settings = api.get_settings(["TELEGRAM_BOT_TOKEN"])
        local_settings = _load_settings(api)
        client = TelegramClient(protected_settings.get("TELEGRAM_BOT_TOKEN", ""))
        pinned_chat = str(local_settings.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        max_updates = _setting_int(local_settings, "TELEGRAM_MAX_UPDATES_PER_POLL", 20, minimum=1, maximum=100)
        command_mode = str(local_settings.get("TELEGRAM_COMMAND_MODE") or _COMMAND_MODE_FULL).strip().lower()
        if command_mode not in _VALID_COMMAND_MODES:
            command_mode = _COMMAND_MODE_STRICT
        lang = str(local_settings.get("TELEGRAM_LANGUAGE") or "en").strip().lower()
        if lang not in ("en", "ru"):
            lang = "en"
        offset = _load_offset(api)

        # Drop any stale one-shot pending-input (e.g. a "custom budget" the owner
        # armed but never completed before a restart) so the first post-restart
        # message is not mis-consumed as a budget amount.
        if pinned_chat:
            try:
                _clear_pending_input(api, int(pinned_chat))
            except (ValueError, TypeError):
                pass

        # Validate token and configure commands before entering poll loop
        try:
            await client.call("getMe")
            
            # Set the command menu list for the blue bottom-left Menu button.
            # The owner-control commands (evolve/bg/review/restart/panic) only
            # appear in full_access — they already forward via the raw-command
            # path; listing them here just makes them discoverable/tappable.
            try:
                await client.call("setMyCommands", data={"commands": json.dumps(_bot_commands(command_mode))})
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
                    pinned_chat = str(local_settings.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
                    command_mode = str(local_settings.get("TELEGRAM_COMMAND_MODE") or _COMMAND_MODE_FULL).strip().lower()
                    if command_mode not in _VALID_COMMAND_MODES:
                        command_mode = _COMMAND_MODE_STRICT
                    lang = str(local_settings.get("TELEGRAM_LANGUAGE") or "en").strip().lower()
                    if lang not in ("en", "ru"):
                        lang = "en"

                for update in updates[:max_updates]:
                    update_id = int(update.get("update_id") or 0)
                    if update_id >= offset:
                        offset = update_id + 1

                    # Owner binding (TOFU) for ALL command modes: the first chat
                    # to interact pins as the owner channel; thereafter only that
                    # chat is served. Without this, strict/safe mode with no
                    # TELEGRAM_CHAT_ID would let arbitrary chats reach _inject.
                    # Covers both the message and callback paths.
                    _cb = update.get("callback_query") or {}
                    _msg = update.get("message") or {}
                    _inbound_chat = int(
                        ((_cb.get("message") or {}).get("chat") or {}).get("id")
                        or (_msg.get("chat") or {}).get("id") or 0
                    )
                    # No resolvable chat (non-message/callback update type, or a
                    # malformed chat.id) → drop it: never reach _inject unbound.
                    if not _inbound_chat:
                        continue
                    if not pinned_chat:
                        local_settings["TELEGRAM_CHAT_ID"] = str(_inbound_chat)
                        _save_settings_dict(api, local_settings)
                        pinned_chat = str(_inbound_chat)
                    if str(_inbound_chat) != pinned_chat:
                        if _cb:
                            try:
                                await client.answer_callback_query(
                                    str(_cb.get("id") or ""),
                                    text=_LOCALIZED_TEXTS[lang]["not_authorized"],
                                )
                            except Exception:
                                pass
                        continue

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
                        if not pinned_chat or str(cb_chat_id) != pinned_chat:
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
                            elif target == "tasks":
                                header, keyboard = _build_menu_tasks(api, command_mode, lang)
                                await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                            elif target == "settings":
                                header, keyboard = _build_menu_settings(api, command_mode, lang)
                                await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                            elif target == "model":
                                header, keyboard = _build_model_keyboard(api, lang)
                                await client.edit_message_text_with_inline_keyboard(cb_chat_id, cb_message_id, header, keyboard)
                            elif target == "budget":
                                header, keyboard = _build_budget_keyboard(api, lang)
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
                                translated = "/bg start" if action == "bg_start" else "/bg stop"
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

                        # --- Model selection buttons ---
                        # The skill does NOT write the core settings.json itself
                        # (path-confinement). It injects an owner-visible request
                        # via the already-approved inject_chat path; the agent
                        # applies the change through the guarded settings flow.
                        # Gated to full_access + the pinned owner chat (verified
                        # above), same envelope as the /bg control buttons.
                        if cb_data.startswith("set_model:"):
                            if command_mode != _COMMAND_MODE_FULL:
                                await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang]["restricted_safe"])
                                continue
                            choices = _model_choices(api)
                            try:
                                model_id = choices[int(cb_data.split(":", 1)[1])]
                            except (ValueError, IndexError):
                                await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang]["unknown_command"])
                                continue
                            await client.answer_callback_query(cb_id, text=f"📨 {model_id}")
                            sender_name = _extract_sender_label(cb_sender, cb_chat_id)
                            sender_label = f"Telegram ({sender_name})"
                            await _inject(api, {
                                "text": _model_change_command(model_id, lang),
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

                        # --- Budget increment buttons (inject owner request; no core write) ---
                        if cb_data.startswith("set_budget:"):
                            if command_mode != _COMMAND_MODE_FULL:
                                await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang]["restricted_safe"])
                                continue
                            arg = cb_data.split(":", 1)[1]
                            if arg == "custom":
                                # Arm a one-shot: the owner's NEXT message is read
                                # as a $ amount and injected as an owner request
                                # (no direct write). Cleared on the next message.
                                _set_pending_input(api, cb_chat_id, "budget")
                                await client.answer_callback_query(cb_id)
                                await client.send_message(cb_chat_id, (
                                    "Введи сумму бюджета в $ (например 250) или «отмена»."
                                    if lang == "ru" else
                                    "Send the budget amount in $ (e.g. 250), or 'cancel'."))
                                continue
                            try:
                                delta = float(arg)
                            except ValueError:
                                await client.answer_callback_query(cb_id, text=_LOCALIZED_TEXTS[lang]["unknown_command"])
                                continue
                            new_budget = round(_get_current_budget(api) + delta, 2)
                            await client.answer_callback_query(cb_id, text=f"📨 ${new_budget:.2f}")
                            sender_name = _extract_sender_label(cb_sender, cb_chat_id)
                            sender_label = f"Telegram ({sender_name})"
                            await _inject(api, {
                                "text": _budget_change_command(new_budget, lang),
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
                    # Owner binding + filtering is already enforced at the top of
                    # the update loop (TOFU for all command modes), so chat_id is
                    # guaranteed to equal the pinned owner chat here.
                    text = str(message.get("text") or message.get("caption") or "").strip()
                    caption = str(message.get("caption") or "").strip()

                    # --- Pending one-shot input (e.g. custom budget amount) ---
                    # Armed by the "Custom amount" budget button (full_access only).
                    # The amount is injected as an owner request, never written to
                    # settings.json directly (path-confinement).
                    if _get_pending_input(api, chat_id) == "budget":
                        _clear_pending_input(api, chat_id)  # one-shot: always consume, never trap the chat
                        low = text.strip().lower()
                        if low in ("cancel", "/cancel", "отмена", "отменить"):
                            await client.send_message(chat_id, ("Отменено." if lang == "ru" else "Cancelled."))
                            continue
                        # A non-cancel slash command or media while armed is an
                        # implicit cancel: fall through (no continue) so /menu,
                        # voice, photos, etc. still work instead of being swallowed.
                        if not (_SLASH_COMMAND_RE.match(text) or message.get("voice") or message.get("photo")):
                            if command_mode != _COMMAND_MODE_FULL:  # defensive: arming is full_access-gated
                                continue
                            amount = _parse_budget_amount(text)
                            if amount is None:
                                await client.send_message(chat_id, (
                                    "Ожидал число — ввод бюджета отменён. Нажми «Своя сумма» снова."
                                    if lang == "ru" else
                                    "Expected a number — custom budget cancelled. Tap Custom again."))
                                continue
                            sender_name = _extract_sender_label(sender, chat_id)
                            sender_label = f"Telegram ({sender_name})"
                            await _inject(api, {
                                "text": _budget_change_command(amount, lang),
                                "chat_id": chat_id,
                                "user_id": int(sender.get("id") or chat_id or 1),
                                "source": "telegram-bridge",
                                "sender_label": sender_label,
                                "transport": {"kind": "telegram", "conversation_id": str(chat_id), "sender_label": sender_label},
                                "image_base64": "",
                                "image_mime": "",
                                "image_caption": "",
                            })
                            await client.send_message(chat_id, f"📨 ${amount:.2f}")
                            continue

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
                        # Acknowledge unsupported inbound attachments instead of
                        # silently swallowing them. Inbound file INGESTION is not
                        # yet wired on the host side (only text/voice/photo), so
                        # tell the user rather than leaving them wondering.
                        if message.get("document") or message.get("video") or message.get("audio") or message.get("sticker"):
                            await client.send_message(
                                chat_id,
                                ("Пока я не умею принимать файлы/видео/аудио — поддерживаются текст, голос и фото."
                                 if lang == "ru" else
                                 "I can't accept files/video/audio yet — supported input: text, voice, and photos."),
                            )
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
            lang = str(local_settings.get("TELEGRAM_LANGUAGE") or "en").strip().lower()

            # Subagent lifecycle → one dedicated bubble per subagent, edited in
            # place across its lifecycle (not a flood of new messages). Since 6.22
            # the supervisor emits one is_progress chat.outbound per subagent state
            # transition; mirroring them raw spams the chat / collapses over the
            # real reply in silent mode.
            sub_event = str(event.get("subagent_event") or "").strip().lower()
            if sub_event:
                if _subagent_cards_enabled(local_settings):
                    await _render_subagent_card(api, client, chat_id, event, sub_event, lang)
                return

            # Generic (non-subagent) progress telemetry → dropped by default; the
            # typing indicator already signals "working". Opt in via the toggle.
            if event.get("is_progress") and not _mirror_progress_enabled(local_settings):
                return

            text = str(event.get("text") or "").strip()
            if not text:
                return
            # Honor the host's markdown hint: plain text (markdown=False) is sent
            # verbatim so literal *, _, `, [] aren't mis-parsed as Telegram
            # formatting; an absent/True hint renders markdown→HTML as before.
            parse_mode = "" if event.get("markdown") is False else "HTML"

            silent_on = _is_silent_mode_enabled(local_settings)
            tracked_msg_id = _get_silent_msg(api, chat_id) if silent_on else 0

            # Silent mode: try to edit the previously tracked message in-place.
            # editMessageText returns False on any failure (too old, deleted,
            # identical content, parse error) so we fall back to sendMessage.
            if silent_on and tracked_msg_id:
                edited = await client.edit_message_text(chat_id, tracked_msg_id, text, parse_mode=parse_mode)
                if not edited and parse_mode:
                    edited = await client.edit_message_text(chat_id, tracked_msg_id, text, parse_mode="")
                if edited:
                    return
                # Edit failed (likely too old or already identical) — clear
                # tracking and fall through to sendMessage path.
                _clear_silent_msg(api, chat_id)

            try:
                msg_id = await client.send_message(chat_id, text, parse_mode=parse_mode)
            except Exception as format_exc:
                api.log("warning", f"Telegram outbound send failed ({format_exc}), retrying with plain text...")
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


def _make_document(api):
    async def handle(event: Dict[str, Any]) -> None:
        try:
            protected_settings = api.get_settings(["TELEGRAM_BOT_TOKEN"])
            local_settings = _load_settings(api)
            client = TelegramClient(protected_settings.get("TELEGRAM_BOT_TOKEN", ""))
            chat_id = _target_chat(local_settings, event)
            file_base64 = str(event.get("file_base64") or "").strip()
            if chat_id and file_base64:
                # Media/files cannot replace a text bubble — reset silent tracking.
                _clear_silent_msg(api, chat_id)
                import base64 as _base64
                filename = str(event.get("filename") or "file")
                caption = str(event.get("caption") or "")
                await client.send_document(
                    chat_id,
                    _base64.b64decode(file_base64),
                    filename=filename,
                    caption=caption,
                )
        except Exception as exc:
            api.log("error", f"Telegram document error: {exc}")
    return handle


def register(api):
    api.register_supervised_task("poller", _make_poller(api), restart_policy="on_failure", max_restarts=10)
    api.register_supervised_task("notifier", _make_notifier(api), restart_policy="on_failure", max_restarts=10)
    api.subscribe_event("chat.outbound", _make_outbound(api))
    api.subscribe_event("chat.typing", _make_typing(api))
    api.subscribe_event("chat.photo", _make_photo(api))
    try:
        api.subscribe_event("chat.video", _make_video(api))
    except Exception as exc:
        api.log("warning", f"Could not subscribe to chat.video: {exc}")
    try:
        api.subscribe_event("chat.document", _make_document(api))
    except Exception as exc:
        api.log("warning", f"Could not subscribe to chat.document: {exc}")
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
                             {"value": "full_access", "label": "Full access (default) — raw owner commands incl. /panic, /restart"},
                             {"value": "safe_commands", "label": "Safe — allow /status, /bg status only"},
                             {"value": "strict", "label": "Strict — block all slash commands from Telegram"},
                         ],
                         "placeholder": "full_access"},
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
                        {"name": "TELEGRAM_SUBAGENT_CARDS", "label": "Subagent cards", "type": "select",
                         "options": [
                             {"value": "on", "label": "On — one updating message per subagent"},
                             {"value": "off", "label": "Off — hide subagent activity"},
                         ],
                         "placeholder": "on"},
                        {"name": "TELEGRAM_MIRROR_PROGRESS", "label": "Mirror progress telemetry", "type": "select",
                         "options": [
                             {"value": "on", "label": "On (default) — stream the main agent's progress"},
                             {"value": "off", "label": "Off — replies only (clean chat)"},
                         ],
                         "placeholder": "on"},
                        {"name": "TELEGRAM_NOTIFY_TASKS", "label": "Notify on task completion", "type": "select",
                         "options": [
                             {"value": "off", "label": "Off"},
                             {"value": "on", "label": "On — ✅ Task done · cost · rounds"},
                         ],
                         "placeholder": "off"},
                        {"name": "TELEGRAM_NOTIFY_BUDGET", "label": "Notify on budget thresholds", "type": "select",
                         "options": [
                             {"value": "off", "label": "Off"},
                             {"value": "on", "label": "On — ⚠️ at 80% / 90% / 100%"},
                         ],
                         "placeholder": "off"},
                        {"name": "TELEGRAM_MODEL_CHOICES", "label": "Model buttons (comma-separated model IDs)", "type": "text",
                         "placeholder": "anthropic/claude-opus-4.8, anthropic/claude-sonnet-4.6, openai/gpt-5.5, google/gemini-3.5-flash"},
                    ],
                    "submit_label": "Save Telegram settings",
                }
            ]
        },
    )
