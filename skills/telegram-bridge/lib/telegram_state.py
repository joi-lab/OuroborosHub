"""Telegram bridge: settings/state file helpers + subagent-card rendering."""
from __future__ import annotations

import json
import pathlib
from typing import Any, Dict


def _data_dir(api) -> pathlib.Path:
    """Main Ouroboros data dir (state/logs live here), obtained safely from the PluginAPI."""
    try:
        data_dir = api.get_runtime_info().get("data_dir")
        if data_dir:
            return pathlib.Path(data_dir)
    except Exception:
        pass
    return pathlib.Path(api.get_state_dir()).parent.parent.parent


def _read_json_file(path: pathlib.Path) -> Dict[str, Any]:
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _state_file(api, name: str) -> pathlib.Path:
    return pathlib.Path(api.get_state_dir()) / name


def _load_settings(api) -> Dict[str, Any]:
    path = _state_file(api, "settings.json")
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def _save_settings_dict(api, settings: Dict[str, Any]) -> None:
    path = _state_file(api, "settings.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


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


def _load_subagent_state(api) -> Dict[str, int]:
    """Per-(chat, subagent) Telegram message-id map so each subagent gets ONE
    bubble that is edited in place across its lifecycle instead of spamming."""
    path = _state_file(api, "subagent_state.json")
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


def _save_subagent_state(api, data: Dict[str, int]) -> None:
    path = _state_file(api, "subagent_state.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _get_subagent_msg(api, chat_id: int, child_id: str) -> int:
    return int(_load_subagent_state(api).get(f"{chat_id}:{child_id}") or 0)


def _set_subagent_msg(api, chat_id: int, child_id: str, message_id: int) -> None:
    state = _load_subagent_state(api)
    state[f"{chat_id}:{child_id}"] = int(message_id)
    # Bound growth: one entry per subagent ever seen would leak unbounded. Keep
    # the most recent 200 (dict preserves insertion order).
    if len(state) > 200:
        for stale in list(state.keys())[:-200]:
            state.pop(stale, None)
    _save_subagent_state(api, state)


def _subagent_cards_enabled(settings: Dict[str, Any]) -> bool:
    """Render each subagent's lifecycle as one updating Telegram message. Default: on."""
    raw = str(settings.get("TELEGRAM_SUBAGENT_CARDS") or "on").strip().lower()
    return raw in ("on", "true", "1", "yes")


def _mirror_progress_enabled(settings: Dict[str, Any]) -> bool:
    """Mirror generic (non-subagent) progress telemetry to Telegram. Default: off —
    Telegram shows replies + a typing indicator, not the raw progress firehose."""
    raw = str(settings.get("TELEGRAM_MIRROR_PROGRESS") or "off").strip().lower()
    return raw in ("on", "true", "1", "yes")


_SUBAGENT_ICONS = {
    "scheduled": "🔵", "running": "🟡", "update": "🟡", "progress": "🟡",
    "completed": "✅", "completed_warn": "⚠️", "failed": "❌",
    "cancelled": "🚫", "rejected": "♻️", "interrupted": "⏸️",
}
# Terminal states lock the bubble's final look; 'interrupted' is retryable so its
# bubble stays editable for the resumed run.
_SUBAGENT_TERMINAL = {"completed", "completed_warn", "failed", "cancelled", "rejected"}
_SUBAGENT_LABELS = {
    "en": {"scheduled": "queued", "running": "running", "update": "running", "progress": "running",
           "completed": "done", "completed_warn": "done (warn)", "failed": "failed",
           "cancelled": "cancelled", "rejected": "rejected (duplicate)",
           "interrupted": "interrupted", "_title": "subagent"},
    "ru": {"scheduled": "в очереди", "running": "работает", "update": "работает", "progress": "работает",
           "completed": "готов", "completed_warn": "готов (warn)", "failed": "ошибка",
           "cancelled": "отменён", "rejected": "отклонён (дубль)",
           "interrupted": "прерван", "_title": "субагент"},
}


def _subagent_card_text(event: Dict[str, Any], sub_event: str, lang: str) -> str:
    """One subagent's card: a status header plus the latest work note.

    The card is a SINGLE Telegram message edited in place across the subagent's
    lifecycle. The header line carries icon/role/status/cost; the body mirrors
    the live progress commentary shown in the Ouroboros UI (the "I will read
    lines …" notes). Because the note changes every event, the edit always has
    fresh content — so the message updates in place instead of Telegram
    rejecting an identical edit and forcing a brand-new bubble. Sent verbatim
    as plain text (no HTML parse risk)."""
    labels = _SUBAGENT_LABELS.get(lang, _SUBAGENT_LABELS["en"])
    icon = _SUBAGENT_ICONS.get(sub_event, "🟡")
    role = str(event.get("subagent_role") or "").strip()
    child = str(event.get("subagent_task_id") or event.get("task_id") or "").strip()[:8]
    who = role or labels["_title"]
    header = f"{icon} {who} — {labels.get(sub_event, sub_event)}"
    if child:
        header += f" [{child}]"
    try:
        cost = float(event.get("cost_usd") or 0)
    except (TypeError, ValueError):
        cost = 0.0
    if sub_event in _SUBAGENT_TERMINAL and cost > 0:
        header += f" · ${cost:.2f}"
    # Live work commentary: the in-flight note, or the result/summary on finish.
    note = str(event.get("text") or event.get("result") or event.get("trace_summary") or "").strip()
    if note:
        if len(note) > 700:
            note = note[:700].rstrip() + "…"
        return f"{header}\n{note}"
    return header


async def _render_subagent_card(api, client, chat_id: int, event: Dict[str, Any], sub_event: str, lang: str) -> None:
    """One Telegram message per subagent, edited in place across its lifecycle."""
    child = str(event.get("subagent_task_id") or event.get("task_id") or "").strip()
    if not child:
        return
    text = _subagent_card_text(event, sub_event, lang)
    msg_id = _get_subagent_msg(api, chat_id, child)
    if msg_id:
        # Edit the subagent's existing bubble (plain text — no HTML parse risk).
        if await client.edit_message_text(chat_id, msg_id, text, parse_mode=""):
            return
        # Edit failed (message too old / deleted) → post a fresh bubble below.
    new_id = await client.send_message(chat_id, text, parse_mode="")
    if new_id:
        _set_subagent_msg(api, chat_id, child, new_id)
