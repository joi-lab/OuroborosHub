from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import date, timedelta
from typing import Any

SKILL_VERSION = "1.0.0"
# The skill may receive large restaurant tables, but it should not send large
# tables back to the LLM. Input is bounded only as a safety guard; output is
# aggressively shortlisted to keep booking flows cheap and fast.
MAX_INPUT_CHARS = 1_000_000
MAX_OUTPUT_CHARS = 20_000
MAX_INPUT_RESTAURANTS = 2_000
MAX_OUTPUT_RESTAURANTS = 20
DEFAULT_SHORTLIST_LIMIT = 8

SCENARIO_TYPES = {
    "happy_path",
    "conditional_success",
    "unavailable_slot",
    "missing_data",
    "complex_form",
    "blocker",
    "phone_only",
    "random",
}

FINAL_STATUSES = {
    "confirmed": "booking explicitly confirmed",
    "created_waiting_confirmation": "request created; SMS/call/manual confirmation pending",
    "not_available": "requested slot unavailable",
    "alternative_proposed": "nearby slot or allowed alternative proposed",
    "needs_clarification": "required input missing; form was not submitted",
    "phone_only": "online booking is unavailable; booking is phone-only",
    "technical_blocker": "captcha/login/anti-bot/phone-only/broken form/etc.",
    "unknown_manual_check": "unclear final state; no success claimed",
}

STATUS_ALIASES = {
    "success": "confirmed",
    "successful": "confirmed",
    "confirmed": "confirmed",
    "reservation_confirmed": "confirmed",
    "booked": "confirmed",
    "подтверждено": "confirmed",
    "бронь подтверждена": "confirmed",
    "забронировано": "confirmed",
    "pending": "created_waiting_confirmation",
    "created": "created_waiting_confirmation",
    "request_created": "created_waiting_confirmation",
    "waiting_confirmation": "created_waiting_confirmation",
    "ожидает подтверждения": "created_waiting_confirmation",
    "заявка создана": "created_waiting_confirmation",
    "no_slot": "not_available",
    "unavailable": "not_available",
    "not_available": "not_available",
    "нет слота": "not_available",
    "слот недоступен": "not_available",
    "alternative": "alternative_proposed",
    "alternative_proposed": "alternative_proposed",
    "альтернатива": "alternative_proposed",
    "clarification": "needs_clarification",
    "needs_clarification": "needs_clarification",
    "missing_data": "needs_clarification",
    "нужно уточнение": "needs_clarification",
    "blocker": "technical_blocker",
    "technical_blocker": "technical_blocker",
    "captcha": "technical_blocker",
    "login_required": "technical_blocker",
    "phone_only": "phone_only",
    "phone only": "phone_only",
    "call to book": "phone_only",
    "только по телефону": "phone_only",
    "бронь по телефону": "phone_only",
    "блокер": "technical_blocker",
    "капча": "technical_blocker",
    "unknown": "unknown_manual_check",
    "manual_check": "unknown_manual_check",
    "unknown_manual_check": "unknown_manual_check",
    "неясно": "unknown_manual_check",
}

REQUIRED_BOOKING_FIELDS = ("date", "time", "guests", "name", "phone")
KEY_ALIASES = {
    "name": ("restaurant", "ресторан", "название", "name", "title", "место"),
    "city": ("city", "город"),
    "cuisine": ("кухня / концепция", "кухня", "concept", "concept_type", "cuisine", "концепция"),
    "average_check": ("средний чек", "средний чек (₽)", "average_check", "avg_check", "price", "budget"),
    "rating": ("рейтинг / награды", "рейтинг", "награды", "rating", "awards"),
    "address": ("адрес", "address", "location"),
    "url": ("сайт", "сайт (онлайн-бронирование)", "website", "url", "site", "booking_url", "online_booking"),
    "phone": ("телефон", "phone", "contact_phone"),
    "note": ("примечание", "notes", "note", "comment", "комментарий"),
}


def _mode(detail: Any) -> str:
    return "full" if str(detail or "").lower().strip() == "full" else "compact"


def _dumps(payload: dict[str, Any], *, detail: str = "compact") -> str:
    if detail == "full":
        text = json.dumps(payload, ensure_ascii=False, sort_keys=False, indent=2)
    else:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    trimmed = {
        "status": payload.get("status", "ok"),
        "warning": "output_trimmed",
        "preview": text[:MAX_OUTPUT_CHARS],
    }
    return json.dumps(trimmed, ensure_ascii=False, separators=(",", ":"))


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value or len(value) > MAX_INPUT_CHARS:
            return default
        try:
            return json.loads(value)
        except Exception:
            return value
    return default


def _as_dict(value: Any) -> dict[str, Any]:
    parsed = _loads(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _norm_key(value: Any) -> str:
    text = str(value or "").strip().lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)
    return text.replace("₽", "руб")


def _clean(value: Any, max_len: int = 300) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())[:max_len]


def _lookup(row: dict[str, Any], canonical: str) -> str:
    aliases = {_norm_key(a) for a in KEY_ALIASES.get(canonical, ())}
    for key, value in row.items():
        if _norm_key(key) in aliases:
            return _clean(value)
    return ""


def _rows_to_dicts(rows: Any, header_row: Any = None) -> list[dict[str, Any]]:
    raw = _loads(rows, [])
    if isinstance(raw, dict):
        raw = raw.get("rows") if isinstance(raw.get("rows"), list) else [raw]
    if not isinstance(raw, list):
        return []
    raw = raw[:MAX_INPUT_RESTAURANTS]
    if not raw:
        return []
    if all(isinstance(item, dict) for item in raw):
        return [dict(item) for item in raw]
    if all(isinstance(item, list) for item in raw):
        headers = _loads(header_row, None)
        data_rows = raw
        if not isinstance(headers, list):
            headers, data_rows = raw[0], raw[1:]
        headers = [_clean(h, 120) for h in headers]
        return [{headers[i]: item[i] if i < len(item) else "" for i in range(len(headers))} for item in data_rows]
    return []


PHONE_ONLY_MARKERS = (
    "только по телефону",
    "бронь по телефону",
    "по телефону",
    "форма отсутствует",
    "онлайн-бронь недоступна",
    "phone only",
    "call to book",
)


def _detect_capabilities(url: str, note: str, source_text: str = "") -> dict[str, Any]:
    text = f"{url} {note} {source_text}".lower().replace("ё", "е")
    flags: list[str] = []
    blockers: list[str] = []
    channel = "unknown"
    can_book_online = True
    if any(marker in text for marker in PHONE_ONLY_MARKERS):
        return {
            "channel": "phone_only",
            "booking_channel": "phone_only",
            "can_book_online": False,
            "flags": [],
            "blockers": ["phone_only"],
        }
    if any(m in text for m in ("онлайн", "форма", "бронь на сайте", "бронирование на сайте", "resto", "reserve")):
        channel = "online_form"
    if "telegram" in text or "телеграм" in text or "бот" in text:
        flags.append("messenger_bot")
        if channel == "unknown":
            channel = "messenger_bot"
    if "онлайн временно недоступна" in text:
        channel = "phone_only_or_online_unavailable"
        blockers.append("online_booking_not_available")
    if "капча" in text or "captcha" in text:
        blockers.append("captcha")
    if "логин" in text or "login" in text or "авториза" in text:
        blockers.append("login_required")
    if "антибот" in text or "anti-bot" in text:
        blockers.append("anti_bot")
    if "не работает" in text or "ошибка" in text or "недоступ" in text:
        blockers.append("booking_widget_may_be_broken")
    if "предоплат" in text or "депозит" in text or "оплата" in text:
        flags.append("prepayment_or_deposit")
    if "террас" in text or "зал" in text or "размещение" in text:
        flags.append("placement_choice")
    if channel == "unknown" and url:
        channel = "website_needs_inspection"
    return {
        "channel": channel,
        "booking_channel": channel,
        "can_book_online": can_book_online,
        "flags": sorted(set(flags)),
        "blockers": sorted(set(blockers)),
    }


def _normalize_restaurant(row: dict[str, Any], *, compact: bool = True) -> dict[str, Any]:
    item = {k: _lookup(row, k) for k in KEY_ALIASES}
    source_text = " ".join(_clean(value) for value in row.values())
    caps = _detect_capabilities(item.get("url", ""), item.get("note", ""), source_text)
    if compact:
        out = {"name": item.get("name", "")}
        for key in ("url", "address", "phone", "city"):
            if item.get(key):
                out[key] = item[key]
        out["channel"] = caps["channel"]
        out["booking_channel"] = caps["booking_channel"]
        if caps["can_book_online"] is False:
            out["can_book_online"] = False
        if caps["flags"]:
            out["flags"] = caps["flags"]
        if caps["blockers"]:
            out["blockers"] = caps["blockers"]
        return {k: v for k, v in out.items() if v not in ("", [], None)}
    item.update({
        "booking_channel": caps["booking_channel"],
        "booking_channel_hint": caps["channel"],
        "can_book_online": caps["can_book_online"],
        "flags": caps["flags"],
        "possible_blockers": caps["blockers"],
        "blockers": caps["blockers"],
    })
    return {k: v for k, v in item.items() if v not in ("", [], None)}


def _coerce_output_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except Exception:
        value = DEFAULT_SHORTLIST_LIMIT
    return max(1, min(value, MAX_OUTPUT_RESTAURANTS))


def _normalize_all(rows: Any, header_row: Any = None, *, compact: bool = True) -> list[dict[str, Any]]:
    normalized = [_normalize_restaurant(r, compact=compact) for r in _rows_to_dicts(rows, header_row)]
    return [r for r in normalized if r.get("name") or r.get("url") or r.get("address")]


def _normalize_many(rows: Any, header_row: Any = None, limit: int = DEFAULT_SHORTLIST_LIMIT, *, compact: bool = True) -> list[dict[str, Any]]:
    # Backward-compatible helper: process the whole input table, return only a short output slice.
    return _normalize_all(rows, header_row=header_row, compact=compact)[:_coerce_output_limit(limit)]


def _score_restaurant(row: dict[str, Any], criteria: dict[str, Any]) -> int:
    score = 0
    name = _norm_key(row.get("name"))
    blob = _norm_key(" ".join(str(row.get(k, "")) for k in ("city", "cuisine", "address", "url", "channel", "booking_channel", "booking_channel_hint")))
    wanted_name = _norm_key(criteria.get("name") or criteria.get("restaurant") or "")
    if wanted_name and (wanted_name == name or wanted_name in name or name in wanted_name):
        score += 100
    for key, points in (("city", 20), ("cuisine", 15), ("address", 20), ("booking_channel", 10), ("channel", 10)):
        value = _norm_key(criteria.get(key) or "")
        if value and value in blob:
            score += points
    return score


def _select_restaurant(restaurants: list[dict[str, Any]], criteria: dict[str, Any], scenario_type: str) -> dict[str, Any]:
    if restaurants:
        candidates = list(restaurants)
        if scenario_type == "phone_only":
            candidates = [r for r in candidates if _is_phone_only_restaurant(r)] or candidates
        elif scenario_type == "blocker":
            candidates = [r for r in candidates if r.get("blockers") or r.get("possible_blockers")] or candidates
        elif scenario_type == "complex_form":
            complex_flags = {"placement_choice", "prepayment_or_deposit", "messenger_bot"}
            candidates = [r for r in candidates if complex_flags.intersection(set(r.get("flags", [])))] or candidates
        return sorted(candidates, key=lambda r: _score_restaurant(r, criteria), reverse=True)[0]
    return _normalize_restaurant({
        "Ресторан": criteria.get("name") or criteria.get("restaurant") or "Ресторан из запроса",
        "Город": criteria.get("city", ""),
        "Адрес": criteria.get("address", ""),
        "Сайт": criteria.get("url", ""),
        "Телефон": criteria.get("phone", ""),
        "Примечание": criteria.get("note", ""),
    })


# Patterns that explicitly negate success. Must beat any "success"/"confirmed" substring
# so that an agent reporting "no success, captcha" does not collapse to a confirmed status.
_STATUS_NEGATION_PATTERNS = (
    re.compile(r"\bno success(?:ful)?\b"),
    re.compile(r"\bnot success(?:ful)?\b"),
    re.compile(r"\bне\s*(?:был[оа]?\s*)?успех"),
    re.compile(r"\bнеуспех"),
)

# Blocker / phone-only markers take priority over generic success words, so a phrase like
# "successful captcha block" resolves to technical_blocker, not confirmed.
_STATUS_PRIORITY_MARKERS: tuple[tuple[str, str, bool], ...] = (
    ("captcha", "technical_blocker", True),
    ("капча", "technical_blocker", False),
    ("anti-bot", "technical_blocker", False),
    ("antibot", "technical_blocker", True),
    ("антибот", "technical_blocker", False),
    ("анти бот", "technical_blocker", False),
    ("блокер", "technical_blocker", False),
    ("login required", "technical_blocker", False),
    ("login_required", "technical_blocker", True),
    ("phone only", "phone_only", False),
    ("call to book", "phone_only", False),
    ("только по телефону", "phone_only", False),
    ("бронь по телефону", "phone_only", False),
)


def _separator_normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\-/_]+", " ", text)).strip()


def _canonical_status(value: Any) -> str:
    raw = _norm_key(value)
    if not raw:
        return "unknown_manual_check"
    # Exact-match the raw form first so canonical keys with underscores (e.g. "no_slot",
    # "phone_only", "login_required") keep working without going through fuzzy search.
    if raw in STATUS_ALIASES:
        return STATUS_ALIASES[raw]
    if raw in FINAL_STATUSES:
        return raw
    # Then treat "-", "/", and "_" as space so that "phone-only", "blocker_due_to_captcha",
    # and "анти-бот защита" reach the same markers as their space-separated forms.
    text = _separator_normalize(raw)
    if text in STATUS_ALIASES:
        return STATUS_ALIASES[text]
    for pat in _STATUS_NEGATION_PATTERNS:
        if pat.search(text):
            return "unknown_manual_check"
    for marker, canonical, word_only in _STATUS_PRIORITY_MARKERS:
        norm_marker = _separator_normalize(marker)
        if word_only:
            if re.search(rf"\b{re.escape(norm_marker)}\b", text):
                return canonical
        elif norm_marker in text:
            return canonical
    # Longest aliases first: "successful" must win over "success", "needs_clarification" over
    # "clarification". Single-word ASCII markers match on word boundaries so "no success ticket"
    # cannot be coerced into "confirmed" via a stray substring.
    for marker in sorted(STATUS_ALIASES, key=len, reverse=True):
        canonical = STATUS_ALIASES[marker]
        norm_marker = _separator_normalize(marker)
        if " " not in norm_marker and norm_marker.isascii():
            if re.search(rf"\b{re.escape(norm_marker)}\b", text):
                return canonical
        elif norm_marker in text:
            return canonical
    return "unknown_manual_check"


def _is_phone_only_restaurant(restaurant: dict[str, Any]) -> bool:
    blockers = restaurant.get("blockers") or restaurant.get("possible_blockers") or []
    if isinstance(blockers, str):
        blockers = [blockers]
    channel = _norm_key(restaurant.get("booking_channel") or restaurant.get("channel") or restaurant.get("booking_channel_hint"))
    return (
        channel == "phone_only"
        or restaurant.get("can_book_online") is False
        or "phone_only" in {_norm_key(blocker) for blocker in blockers}
    )


def _normalize_phone(value: Any) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    return "7" + digits[1:] if digits.startswith("8") and len(digits) == 11 else digits


def _normalize_time(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"(\d{1,2})[:.](\d{2})", text)
    if match:
        return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
    match = re.search(r"\b(\d{1,2})\b", text)
    return f"{int(match.group(1)):02d}:00" if match else text


def _missing_fields(booking: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for field in REQUIRED_BOOKING_FIELDS:
        value = booking.get(field)
        if value is None or str(value).strip() == "":
            missing.append(field)
    if "guests" in booking:
        try:
            if int(booking.get("guests")) <= 0 and "guests" not in missing:
                missing.append("guests")
        except Exception:
            if "guests" not in missing:
                missing.append("guests")
    return missing


def _default_future_date() -> str:
    return (date.today() + timedelta(days=7)).isoformat()


def _scenario_id(payload: dict[str, Any], seed: str = "") -> str:
    digest = hashlib.sha256((seed + json.dumps(payload, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()[:10]
    return f"rbp_{digest}"


def _expected_statuses(scenario_type: str) -> list[str]:
    return {
        "missing_data": ["needs_clarification"],
        "conditional_success": ["created_waiting_confirmation"],
        "unavailable_slot": ["not_available", "alternative_proposed", "created_waiting_confirmation", "confirmed"],
        "blocker": ["technical_blocker", "unknown_manual_check"],
        "phone_only": ["phone_only"],
    }.get(scenario_type, ["confirmed", "created_waiting_confirmation"])


def _task_text(scenario_type: str, restaurant: dict[str, Any], booking: dict[str, Any]) -> str:
    if scenario_type == "phone_only":
        chunks = [f"Проверь онлайн-бронирование: {restaurant.get('name','ресторан')}"]
    else:
        chunks = [f"Забронируй: {restaurant.get('name','ресторан')}"]
    if restaurant.get("url"):
        chunks.append(f"сайт {restaurant['url']}")
    if restaurant.get("address"):
        chunks.append(f"адрес сверить: {restaurant['address']}")
    if scenario_type == "phone_only" and restaurant.get("phone"):
        chunks.append(f"телефон ресторана: {restaurant['phone']}")
    parts = []
    for label, key in (("дата", "date"), ("время", "time"), ("гостей", "guests"), ("имя", "name"), ("телефон", "phone"), ("комментарий", "comment"), ("зал/место", "placement")):
        if scenario_type == "phone_only" and key in {"phone", "comment", "placement"}:
            continue
        if booking.get(key):
            parts.append(f"{label}: {booking[key]}")
    if parts:
        chunks.append("; ".join(parts))
    rule = {
        "missing_data": "если данных не хватает — не отправляй, запроси уточнение",
        "unavailable_slot": "если нет слота — предложи ближайший слот/разрешённую альтернативу",
        "conditional_success": "заявка без явного подтверждения = waiting, не confirmed",
        "phone_only": "онлайн-бронь недоступна, бронь только по телефону; не продолжай онлайн-бронь",
        "blocker": "капча/логин/антибот/сломанная форма = technical_blocker, не успех",
        "complex_form": "обработай dropdown/iframe/зал/депозит; перед submit сверяй поля",
    }.get(scenario_type, "после submit классифицируй финальный экран")
    chunks.append(rule)
    if scenario_type == "phone_only":
        chunks.append('верни JSON: final_status="phone_only", success=false, phone=<телефон ресторана>, reason=<наблюдаемая причина недоступности онлайн-брони>, next_step=<рекомендованное действие>')
    else:
        chunks.append("верни JSON: final_status, restaurant_name, filled_values, evidence, errors/blockers, timing")
    return ". ".join(chunks) + "."


def _compact_scenario(full: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": full["id"],
        "type": full["type"],
        "restaurant": full["restaurant"],
        "booking": full["booking_expected"],
        "input": full["booking_for_agent"],
        "task": full["agent_task"],
        "expect": full["expected_statuses"],
        "pool": full.get("candidate_stats", {}),
        "rules": ["verify_restaurant", "echo_fields", "precheck_submit", "no_fake_success", "safe_stop_on_blocker"],
        "report_keys": full.get("report_keys", ["final_status", "restaurant_name", "filled_values", "evidence", "errors", "timing"]),
    }


def normalize_restaurants(
    ctx: Any = None,
    *,
    rows: Any = None,
    header_row: Any = None,
    restaurant_criteria: Any = None,
    limit: int = DEFAULT_SHORTLIST_LIMIT,
    detail: str = "compact",
) -> str:
    """Normalize all restaurant rows, but return only a ranked shortlist to save tokens."""
    mode = _mode(detail)
    output_limit = _coerce_output_limit(limit)
    criteria = _as_dict(restaurant_criteria)
    all_items = _normalize_all(rows, header_row=header_row, compact=(mode == "compact"))
    ranked = sorted(all_items, key=lambda r: _score_restaurant(r, criteria), reverse=True) if criteria else all_items
    items = ranked[:output_limit]
    payload: dict[str, Any] = {
        "status": "ok",
        "total_count": len(all_items),
        "returned_count": len(items),
        "output_limit": output_limit,
        "selection": "ranked_all_candidates_then_shortlisted" if criteria else "normalized_all_then_shortlisted",
        "restaurants": items,
    }
    if mode == "full":
        payload["schema"] = {
            "name": "restaurant name", "city": "city", "address": "address", "url": "site/booking url",
            "phone": "phone", "channel": "legacy booking channel", "booking_channel": "detected booking channel",
            "can_book_online": "false for phone-only restaurants", "flags": "complexity hints", "blockers": "detected blockers",
        }
    return _dumps(payload, detail=mode)


def make_scenario(
    ctx: Any = None,
    *,
    scenario_type: str = "happy_path",
    restaurants: Any = None,
    restaurant_criteria: Any = None,
    booking: Any = None,
    allowed_alternatives: Any = None,
    seed: str = "",
    detail: str = "compact",
) -> str:
    """Generate a reproducible booking-agent test. Default output is compact and cheap."""
    mode = _mode(detail)
    scenario_type = _norm_key(scenario_type or "happy_path")
    if scenario_type not in SCENARIO_TYPES:
        return _dumps({"error": "unknown_scenario_type", "allowed": sorted(SCENARIO_TYPES)}, detail=mode)
    if scenario_type == "random":
        scenario_type = random.Random(seed or "restaurant_booking").choice([
            "happy_path", "conditional_success", "unavailable_slot", "missing_data", "complex_form", "blocker", "phone_only"
        ])

    criteria = _as_dict(restaurant_criteria)
    restaurant_pool = _normalize_all(restaurants, compact=True)
    restaurant = _select_restaurant(restaurant_pool, criteria, scenario_type)
    booking_expected = _as_dict(booking)
    if not booking_expected.get("date"):
        booking_expected["date"] = _default_future_date()
    booking_expected.setdefault("time", "19:30")
    booking_expected.setdefault("guests", 2)
    booking_expected.setdefault("comment", "")
    booking_expected.setdefault("placement", "")

    missing = _missing_fields(booking_expected)
    if missing and scenario_type not in ("missing_data", "phone_only"):
        return _dumps({
            "status": "needs_input",
            "missing": missing,
            "required": list(REQUIRED_BOOKING_FIELDS),
            "message": "ask user for missing fields; do not submit a booking form",
            "restaurant": restaurant,
        }, detail=mode)

    booking_for_agent = dict(booking_expected)
    omitted: list[str] = []
    if scenario_type == "missing_data":
        omitted = missing or ["phone"]
        for field in omitted:
            booking_for_agent.pop(field, None)

    alternatives = _loads(allowed_alternatives, [])
    if not isinstance(alternatives, list):
        alternatives = []

    base = {
        "type": scenario_type,
        "restaurant": restaurant,
        "booking_expected": booking_expected,
        "booking_for_agent": booking_for_agent,
        "omitted": omitted,
        "alternatives": alternatives[:8],
        "candidate_stats": {
            "total": len(restaurant_pool),
            "considered": len(restaurant_pool),
            "returned_to_agent": 1,
            "selection": "ranked_all_candidates_then_selected_one",
        },
    }
    sid = _scenario_id(base, seed)
    full = {
        "id": sid,
        "scenario_id": sid,
        "skill": "restaurant_booking",
        "version": SKILL_VERSION,
        "type": scenario_type,
        "scenario_type": scenario_type,
        "restaurant": restaurant,
        "booking_expected": booking_expected,
        "booking_for_agent": booking_for_agent,
        "omitted": omitted,
        "allowed_alternatives": alternatives[:8],
        "candidate_stats": base["candidate_stats"],
        "agent_task": _task_text(scenario_type, restaurant, booking_for_agent),
        "expected_statuses": _expected_statuses(scenario_type),
        "expected_final_statuses": _expected_statuses(scenario_type),
        "rules": {
            "verify_restaurant": True,
            "read_back_fields": True,
            "precheck_before_submit": True,
            "safe_retry_limit": "1-2 idempotent retries",
            "do_not_claim_success_without_evidence": True,
        },
        "required_artifacts": ["input", "url", "steps", "timing", "final_evidence", "status", "errors"],
        "report_keys": (
            ["final_status", "success", "phone", "reason", "next_step"]
            if scenario_type == "phone_only"
            else ["final_status", "restaurant_name", "filled_values", "evidence", "errors", "timing"]
        ),
        "critical_errors": ["wrong_restaurant", "wrong_date", "wrong_time", "wrong_guest_count", "wrong_name", "wrong_phone", "false_success", "unsafe_submit", "claimed_success_for_phone_only_restaurant"],
    }
    scenario = full if mode == "full" else _compact_scenario(full)
    return _dumps({"status": "ok", "scenario": scenario}, detail=mode)


def _extract_result_values(result: dict[str, Any]) -> dict[str, Any]:
    for key in ("filled_values", "input_parameters", "booking", "reservation", "submitted_values"):
        value = result.get(key)
        if isinstance(value, dict):
            return value
    return result


def _has_any(result: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(result.get(k) not in (None, "", [], {}) for k in keys)


def _validate_values(expected: dict[str, Any], observed: dict[str, Any]) -> tuple[int, list[str], list[str]]:
    score = 30
    warnings: list[str] = []
    critical: list[str] = []
    checks = [
        ("date", lambda a, b: str(a)[:10] == str(b)[:10], "wrong_date"),
        ("time", lambda a, b: _normalize_time(a) == _normalize_time(b), "wrong_time"),
        ("guests", lambda a, b: str(int(a)) == str(int(b)), "wrong_guest_count"),
        ("name", lambda a, b: _norm_key(a) == _norm_key(b), "wrong_name"),
        ("phone", lambda a, b: _normalize_phone(a) == _normalize_phone(b), "wrong_phone"),
    ]
    for field, cmp_fn, err in checks:
        if expected.get(field) in (None, ""):
            continue
        if observed.get(field) in (None, ""):
            warnings.append(f"missing_echo_{field}")
            score -= 3
            continue
        try:
            if not cmp_fn(expected.get(field), observed.get(field)):
                critical.append(err)
                score -= 8
        except Exception:
            critical.append(err)
            score -= 8
    return max(score, 0), warnings, critical


def _recommendation(verdict: str, critical: list[str], warnings: list[str], scenario_type: str) -> str:
    if verdict == "PASS":
        return "ok_controlled_stop" if scenario_type in ("blocker", "missing_data", "phone_only") else "ok"
    if critical:
        return "rerun_or_manual_check_required"
    if warnings:
        return "add_missing_evidence_or_echo_fields"
    return "improve_status_classification_and_report"


def validate_result(ctx: Any = None, *, scenario: Any, result: Any, detail: str = "compact") -> str:
    """Validate a booking-agent report. Default response is compact."""
    mode = _mode(detail)
    s = _as_dict(scenario)
    if "scenario" in s and isinstance(s["scenario"], dict):
        s = s["scenario"]
    r = _as_dict(result)
    if not s:
        return _dumps({"error": "scenario_is_required"}, detail=mode)
    if not r:
        return _dumps({"error": "result_is_required"}, detail=mode)

    scenario_type = s.get("scenario_type") or s.get("type") or "happy_path"
    expected_statuses = s.get("expected_final_statuses") or s.get("expected_statuses") or s.get("expect") or _expected_statuses(scenario_type)
    final_status = _canonical_status(r.get("final_status", r.get("status", r.get("classification", ""))))
    score = 0
    warnings: list[str] = []
    critical: list[str] = []
    checks: dict[str, Any] = {}

    restaurant = s.get("restaurant", {}) if isinstance(s.get("restaurant"), dict) else {}
    restaurant_phone_only = scenario_type == "phone_only" or _is_phone_only_restaurant(restaurant)
    expected_name = _norm_key(restaurant.get("name"))
    observed_name = _norm_key(r.get("restaurant") or r.get("restaurant_name") or r.get("selected_restaurant"))
    if expected_name and observed_name and (expected_name == observed_name or expected_name in observed_name or observed_name in expected_name):
        score += 20
        checks["restaurant"] = "ok"
    elif expected_name and observed_name:
        critical.append("wrong_restaurant")
        checks["restaurant"] = "wrong"
    else:
        score += 12
        warnings.append("restaurant_not_echoed")
        checks["restaurant"] = "weak"

    claimed_success = final_status in ("confirmed", "created_waiting_confirmation") or r.get("success") is True
    if restaurant_phone_only and claimed_success:
        critical.append("claimed_success_for_phone_only_restaurant")

    if scenario_type == "phone_only":
        if final_status in expected_statuses:
            score += 35
            checks["status"] = "ok"
        else:
            warnings.append(f"expected_{expected_statuses}_got_{final_status}")
            checks["status"] = "wrong_or_weak"

        contract_score = 0
        if r.get("success") is False:
            contract_score += 10
        else:
            warnings.append("phone_only_success_not_false")
        if restaurant.get("phone"):
            if _has_any(r, ("phone", "restaurant_phone")):
                contract_score += 5
            else:
                warnings.append("phone_only_phone_not_echoed")
        else:
            contract_score += 5
        if _has_any(r, ("reason",)):
            contract_score += 10
        else:
            warnings.append("phone_only_reason_missing")
        if _has_any(r, ("next_step", "recommendation")):
            contract_score += 10
        else:
            warnings.append("phone_only_next_step_missing")
        score += contract_score
        checks["phone_only_contract"] = contract_score

        safe_score = 0 if critical else 10
        score += safe_score
        checks["safe"] = safe_score
        score = max(0, min(100, score))
        verdict = "FAIL" if critical else ("PASS" if score >= 85 else "PARTIAL" if score >= 60 else "FAIL")
        payload: dict[str, Any] = {
            "status": "ok",
            "verdict": verdict,
            "score": score,
            "final_status": final_status,
            "critical": sorted(set(critical)),
            "warnings": sorted(set(warnings)),
            "recommendation": _recommendation(verdict, critical, warnings, scenario_type),
        }
        if mode == "full":
            payload["checks"] = checks
            payload["expected_statuses"] = expected_statuses
        return _dumps(payload, detail=mode)

    expected_booking = s.get("booking_expected") or s.get("booking") or {}
    expected_booking = expected_booking if isinstance(expected_booking, dict) else {}
    value_score, value_warnings, value_critical = _validate_values(expected_booking, _extract_result_values(r))
    score += value_score
    warnings.extend(value_warnings)
    critical.extend(value_critical)
    checks["values"] = value_score

    if final_status in expected_statuses:
        score += 25
        checks["status"] = "ok"
    else:
        warnings.append(f"expected_{expected_statuses}_got_{final_status}")
        checks["status"] = "wrong_or_weak"
        if final_status in ("confirmed", "created_waiting_confirmation") and scenario_type in ("missing_data", "blocker"):
            critical.append("false_success_reported")

    if scenario_type == "missing_data":
        if bool(r.get("submitted") or r.get("form_submitted")):
            critical.append("submitted_form_with_missing_required_data")
        if not _has_any(r, ("asked_missing_fields", "clarification_fields", "question_to_user")):
            warnings.append("missing_data_no_clarification_reported")
    elif scenario_type == "unavailable_slot" and final_status == "alternative_proposed":
        if not _has_any(r, ("alternatives", "alternative_slots", "alternative_restaurants", "recommendation")):
            warnings.append("alternative_without_details")
    elif scenario_type == "blocker" and final_status == "technical_blocker":
        if not _has_any(r, ("blocker", "blockers", "error", "errors", "reason")):
            warnings.append("blocker_without_reason")

    artifact_checks = {
        "input": _has_any(r, ("input_parameters", "booking", "reservation", "filled_values")),
        "url": _has_any(r, ("restaurant_url", "url", "site", "link")),
        "steps": _has_any(r, ("step_logs", "logs", "steps")),
        "timing": _has_any(r, ("timing", "duration_ms", "duration_sec", "started_at")),
        "evidence": _has_any(r, ("final_screen_screenshot", "screenshot", "final_screen_text", "evidence")),
        "status": final_status in FINAL_STATUSES,
    }
    artifact_score = round(15 * sum(artifact_checks.values()) / len(artifact_checks))
    score += artifact_score
    checks["artifacts"] = artifact_score

    safe_score = 0 if {"false_success_reported", "submitted_form_with_missing_required_data"}.intersection(critical) else (8 if final_status == "unknown_manual_check" else 10)
    score += safe_score
    checks["safe"] = safe_score

    score = max(0, min(100, score))
    verdict = "FAIL" if critical else ("PASS" if score >= 85 else "PARTIAL" if score >= 60 else "FAIL")
    payload: dict[str, Any] = {
        "status": "ok",
        "verdict": verdict,
        "score": score,
        "final_status": final_status,
        "critical": sorted(set(critical)),
        "warnings": sorted(set(warnings)),
        "recommendation": _recommendation(verdict, critical, warnings, scenario_type),
    }
    if mode == "full":
        payload["checks"] = checks
        payload["expected_statuses"] = expected_statuses
    return _dumps(payload, detail=mode)


def register(api: Any) -> None:
    api.register_tool(
        "normalize_restaurants",
        normalize_restaurants,
        description="Cheaply normalize restaurant rows and detect channel/blocker hints. Default detail=compact.",
        schema={
            "type": "object",
            "properties": {
                "rows": {"description": "Restaurant rows: list of objects, list of arrays, or {rows:[...]}."},
                "header_row": {"description": "Optional header row for array rows."},
                "restaurant_criteria": {"type": "object", "description": "Optional name/city/cuisine/address/url/channel for ranking the shortlist."},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_OUTPUT_RESTAURANTS, "default": DEFAULT_SHORTLIST_LIMIT},
                "detail": {"type": "string", "enum": ["compact", "full"], "default": "compact"},
            },
            "required": ["rows"],
        },
        timeout_sec=5,
    )
    api.register_tool(
        "make_scenario",
        make_scenario,
        description="Generate a lightweight restaurant-booking test task. Default detail=compact.",
        schema={
            "type": "object",
            "properties": {
                "scenario_type": {"type": "string", "enum": sorted(SCENARIO_TYPES), "default": "happy_path"},
                "restaurants": {"description": "Optional restaurant rows/list. Example source, not a canon."},
                "restaurant_criteria": {"type": "object", "description": "name, city, cuisine, address, url, channel."},
                "booking": {"type": "object", "description": "date, time, guests, name, phone, comment, placement."},
                "allowed_alternatives": {"description": "Allowed slots/restaurants for unavailable_slot."},
                "seed": {"type": "string", "description": "Reproducible randomization/scenario id."},
                "detail": {"type": "string", "enum": ["compact", "full"], "default": "compact"},
            },
            "required": [],
        },
        timeout_sec=3,
    )
    api.register_tool(
        "validate_result",
        validate_result,
        description="Validate booking-agent result with compact PASS/PARTIAL/FAIL output. Default detail=compact.",
        schema={
            "type": "object",
            "properties": {
                "scenario": {"description": "Scenario object or full make_scenario response."},
                "result": {"description": "Agent report: final_status, restaurant_name, filled_values, evidence, errors/blockers, timing."},
                "detail": {"type": "string", "enum": ["compact", "full"], "default": "compact"},
            },
            "required": ["scenario", "result"],
        },
        timeout_sec=3,
    )
    api.log("info", "restaurant_booking extension registered")
