"""Sber Ring (Life Balance) — Ouroboros extension skill.

Registers three tools:
  • sber_ring_fetch   — universal single-type data fetch
  • sber_ring_summary — recent data sample across all sensors
  • sber_ring_check   — connectivity and authentication check
"""

from __future__ import annotations

import time
from typing import Any

from .client import fetch, VALID_TYPES  # noqa: relative import inside staged package


def register(api: Any) -> None:
    """Called by the Ouroboros extension loader."""

    log = api.log

    def _get_token() -> str:
        """Resolve the bearer token from granted settings."""
        s = api.get_settings(["SBER_RING_TOKEN"])
        token = s.get("SBER_RING_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "SBER_RING_TOKEN не настроен. "
                "Перейдите в Settings → Skills → sber-ring → Grant "
                "и вставьте Bearer-токен от Life Balance."
            )
        return token

    # ── Tool 1: sber_ring_fetch ─────────────────────────────────────

    def tool_fetch(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
        """Fetch one data type for a date range."""
        # Accept both dict-style and keyword-style dispatch.
        if args is None:
            args = kwargs
        elif kwargs:
            args = {**args, **kwargs}
        data_type = args.get("data_type", "HEART_RATE")
        days_back = max(1, min(int(args.get("days_back", 7)), 90))
        page = max(0, int(args.get("page", 0)))
        page_size = max(1, min(int(args.get("page_size", 100)), 100))

        now = int(time.time())
        ts_from = now - days_back * 86400
        ts_to = now

        token = _get_token()

        try:
            result = fetch(
                token=token,
                data_type=data_type,
                ts_from=ts_from,
                ts_to=ts_to,
                page=page,
                page_size=page_size,
            )
        except (ValueError, RuntimeError) as exc:
            return f"❌ Ошибка: {exc}"

        import json as _json

        return _json.dumps(result, ensure_ascii=False, indent=2)

    api.register_tool(
        name="sber_ring_fetch",
        description=(
            "Получить данные с кольца Сбера (Life Balance) за указанный период. "
            "Типы: HEART_RATE, HRV, SPO2, SLEEP, STEP, STRESS, TEMPERATURE."
        ),
        handler=tool_fetch,
        schema={
            "type": "object",
            "properties": {
                "data_type": {
                    "type": "string",
                    "enum": sorted(VALID_TYPES),
                    "description": "Тип данных: HEART_RATE, HRV, SPO2, SLEEP, STEP, STRESS, TEMPERATURE",
                },
                "days_back": {
                    "type": "integer",
                    "description": "Количество дней назад (по умолчанию 7)",
                    "default": 7,
                },
                "page": {
                    "type": "integer",
                    "description": "Номер страницы (по умолчанию 0)",
                    "default": 0,
                },
                "page_size": {
                    "type": "integer",
                    "description": "Размер страницы (по умолчанию 100)",
                    "default": 100,
                },
            },
            "required": ["data_type"],
        },
    )

    # ── Tool 2: sber_ring_summary ───────────────────────────────────

    def tool_summary(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
        """First-page data sample from the last 24 hours across all sensor types."""
        token = _get_token()

        now = int(time.time())
        ts_from = now - 86400
        ts_to = now

        sections: list[str] = []
        import json as _json

        for dtype in sorted(VALID_TYPES):
            try:
                result = fetch(
                    token=token,
                    data_type=dtype,
                    ts_from=ts_from,
                    ts_to=ts_to,
                    page=0,
                    page_size=20,
                    timeout_sec=8,
                )
                records = result if isinstance(result, list) else result.get("data", result.get("content", []))
                count = len(records) if isinstance(records, list) else "?"
                sections.append(f"### {dtype}\nПолучено на первой странице: {count} записей (лимит 20); ниже показаны первые 5.\n```json\n{_json.dumps(records[:5], ensure_ascii=False, indent=2)}\n```")
            except Exception as exc:
                sections.append(f"### {dtype}\n❌ {exc}")

        header = f"## 🩺 Сводка здоровья с кольца Сбера\nПериод: последние 24 часа ({time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts_from))} — {time.strftime('%Y-%m-%d %H:%M', time.gmtime(ts_to))} UTC)\n"
        note = "\nЭто выборка, а не полная статистика за сутки. Для следующих страниц используйте sber_ring_fetch с days_back=1, page_size=20 и параметром page.\n"
        return header + note + "\n\n".join(sections)

    api.register_tool(
        name="sber_ring_summary",
        description=(
            "Сводка здоровья с кольца Сбера за последние 24 часа: "
            "пульс, HRV, SpO2, сон, шаги, стресс, температура. "
            "Возвращает выборку первой страницы каждого типа (до 20 записей, "
            "показаны первые 5), а не полную статистику за сутки."
        ),
        handler=tool_summary,
        schema={
            "type": "object",
            "properties": {},
        },
    )

    # ── Tool 3: sber_ring_check ────────────────────────────────────

    def tool_check(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
        """Quick connectivity and auth check — one tiny API call."""
        try:
            token = _get_token()
        except RuntimeError as exc:
            return f"❌ {exc}"

        now = int(time.time())
        try:
            result = fetch(
                token=token,
                data_type="HEART_RATE",
                ts_from=now - 3600,
                ts_to=now,
                page=0,
                page_size=1,
                timeout_sec=10,
            )
            records = result if isinstance(result, list) else result.get("data", result.get("content", []))
            count = len(records) if isinstance(records, list) else 0
            return f"✅ Подключение к Life Balance API успешно. Записей HEART_RATE за последний час: {count}"
        except RuntimeError as exc:
            return f"❌ Ошибка подключения: {exc}"

    api.register_tool(
        name="sber_ring_check",
        description=(
            "Быстрая проверка подключения к кольцу Сбера: "
            "валидирует токен и делает один тестовый запрос."
        ),
        handler=tool_check,
        schema={
            "type": "object",
            "properties": {},
        },
    )

    log("info", "sber-ring: registered 3 tools (sber_ring_fetch, sber_ring_summary, sber_ring_check)")
