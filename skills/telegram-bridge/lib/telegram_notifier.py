"""Telegram bridge: periodic push notifications (task done / budget thresholds)."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict

from .telegram_api import TelegramClient
from .telegram_state import _data_dir, _load_settings, _read_json_file, _state_file


def _notify_enabled(settings: Dict[str, Any], key: str) -> bool:
    return str(settings.get(key) or "off").strip().lower() in ("on", "true", "1", "yes")


def _load_notif_state(api) -> Dict[str, Any]:
    return _read_json_file(_state_file(api, "notif_state.json"))


def _save_notif_state(api, data: Dict[str, Any]) -> None:
    path = _state_file(api, "notif_state.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _pinned_chat_id(settings: Dict[str, Any]) -> int:
    raw = str(settings.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    try:
        return int(raw)
    except ValueError:
        return 0


async def _push_notification(api, chat_id: int, text: str) -> None:
    protected = api.get_settings(["TELEGRAM_BOT_TOKEN"])
    client = TelegramClient(protected.get("TELEGRAM_BOT_TOKEN", ""))
    try:
        await client.send_message(int(chat_id), text, parse_mode="")
    except Exception as exc:
        api.log("error", f"Telegram notify failed: {exc}")


_BUDGET_THRESHOLDS = (100, 90, 80)  # checked high → low


async def _check_budget_notify(api, settings: Dict[str, Any], chat_id: int, state: Dict[str, Any], lang: str) -> None:
    if not _notify_enabled(settings, "TELEGRAM_NOTIFY_BUDGET"):
        return
    data = _data_dir(api)
    spent = float(_read_json_file(data / "state" / "state.json").get("spent_usd") or 0.0)
    total = float(_read_json_file(data / "settings.json").get("TOTAL_BUDGET") or 800.0)
    if total <= 0:
        return
    pct = spent / total * 100.0
    crossed = 0
    for thr in _BUDGET_THRESHOLDS:
        if pct >= thr:
            crossed = thr
            break
    notified = int(state.get("budget_threshold") or 0)
    if crossed > notified:
        msg = (f"⚠️ Бюджет: {pct:.0f}% (${spent:.2f} / ${total:.2f})" if lang == "ru"
               else f"⚠️ Budget: {pct:.0f}% (${spent:.2f} / ${total:.2f})")
        await _push_notification(api, chat_id, msg)
        state["budget_threshold"] = crossed
    elif crossed < notified:
        state["budget_threshold"] = crossed  # budget raised / spend reset → re-arm


def _summary_ids_in_tail(api, limit: int = 200) -> list:
    try:
        lines = (_data_dir(api) / "logs" / "chat.jsonl").read_text(encoding="utf-8").splitlines()[-limit:]
    except Exception:
        return []
    ids = []
    for line in lines:
        try:
            e = json.loads(line)
        except Exception:
            continue
        if str(e.get("type") or "") == "task_summary" and e.get("task_id"):
            ids.append((str(e.get("task_id")), e))
    return ids


async def _check_tasks_notify(api, settings: Dict[str, Any], chat_id: int, state: Dict[str, Any], lang: str) -> None:
    if not _notify_enabled(settings, "TELEGRAM_NOTIFY_TASKS"):
        return
    summaries = _summary_ids_in_tail(api)
    if "notified_task_ids" not in state:
        # First run with task notifications on → treat the existing backlog as seen
        # so enabling the toggle doesn't blast a notification for every old task.
        state["notified_task_ids"] = [tid for tid, _ in summaries][-300:]
        return
    seen = list(state.get("notified_task_ids") or [])
    seen_set = set(seen)
    for tid, e in summaries:
        if tid in seen_set:
            continue
        parts = []
        rounds = e.get("rounds")
        if rounds is not None:
            parts.append(f"{rounds}r")
        tr = _read_json_file(_data_dir(api) / "task_results" / f"{tid}.json")
        try:
            if tr.get("cost_usd") is not None:
                parts.append(f"${float(tr.get('cost_usd')):.2f}")
        except (TypeError, ValueError):
            pass
        oa = e.get("outcome_axes")
        outcome = str(oa.get("lifecycle") or "") if isinstance(oa, dict) else ""
        if outcome and outcome not in ("completed", "done"):
            parts.append(outcome)
        tail = (" · " + " · ".join(parts)) if parts else ""
        icon = "✅" if outcome in ("", "completed", "done") else "⚠️"
        msg = (f"{icon} Задача {tid[:8]} готова{tail}" if lang == "ru" else f"{icon} Task {tid[:8]} done{tail}")
        await _push_notification(api, chat_id, msg)
        seen.append(tid)
        seen_set.add(tid)
    state["notified_task_ids"] = seen[-300:]


def _make_notifier(api):
    """Periodic, file-based proactive notifications (task done / budget threshold).
    Read-only over durable files; sends only when a pinned chat + toggle are set."""
    async def notifier() -> None:
        while True:
            try:
                settings = _load_settings(api)
                chat_id = _pinned_chat_id(settings)
                want = _notify_enabled(settings, "TELEGRAM_NOTIFY_TASKS") or _notify_enabled(settings, "TELEGRAM_NOTIFY_BUDGET")
                if chat_id and want:
                    lang = str(settings.get("TELEGRAM_LANGUAGE") or "en").strip().lower()
                    state = _load_notif_state(api)
                    await _check_budget_notify(api, settings, chat_id, state, lang)
                    await _check_tasks_notify(api, settings, chat_id, state, lang)
                    _save_notif_state(api, state)
            except Exception as exc:
                api.log("error", f"Telegram notifier error: {exc}")
            await asyncio.sleep(30)
    return notifier
