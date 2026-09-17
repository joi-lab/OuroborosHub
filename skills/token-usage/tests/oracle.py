"""Independent exact arithmetic for frozen, public numeric-only snapshots.

This module deliberately does not import the implementation. Decimal is used for
recorded money and integer token equality is strict: None never equals zero.
Frozen fixture: PYTHONDONTWRITEBYTECODE=1 python tests/oracle.py
Downloaded export: PYTHONDONTWRITEBYTECODE=1 python tests/oracle.py path/to/token-observatory-snapshot.json

An export check verifies checksum, row selection membership and arithmetic. It
cannot independently prove canonical-source completeness from a selected export.
"""
from __future__ import annotations

import json
import hashlib
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens")
PHYSICAL_STATES = frozenset(("dispatched", "settled", "unresolved"))


def is_token(value):
    return type(value) is int and value >= 0


def is_money(value):
    return type(value) in (int, float) and value >= 0 and Decimal(str(value)).is_finite()


def metric(rows, field, money=False):
    predicate = is_money if money else is_token
    values = [row.get(field) for row in rows if predicate(row.get(field))]
    if money:
        total = float(sum((Decimal(str(value)) for value in values), Decimal(0))) if values else None
    else:
        total = sum(values) if values else None
    return {"value": total, "known": len(values), "missing": len(rows) - len(values)}


def physical_summary(rows):
    physical = [row for row in rows if row.get("kind") == "attempt" and row.get("state") in PHYSICAL_STATES]
    metrics = {field: metric(physical, field) for field in TOKEN_FIELDS}
    confirmed = [row for row in physical if row["state"] == "settled" and row.get("cost_final") is True]
    estimated = [row for row in physical if row["state"] == "settled" and row.get("cost_final") is False]
    held = [row for row in physical if row["state"] in ("dispatched", "unresolved")]
    metrics["cost_confirmed_usd"] = metric(confirmed, "cost_usd", money=True)
    metrics["cost_estimated_usd"] = metric(estimated, "cost_usd", money=True)
    metrics["cost_held_usd"] = metric(held, "reservation_upper_bound_usd", money=True)
    input_output = [value for row in physical for field in TOKEN_FIELDS[:2] if is_token(value := row.get(field))]
    unknown_money = [row for row in physical if (
        row["state"] == "settled" and (type(row.get("cost_final")) is not bool or not is_money(row.get("cost_usd")))
    ) or (
        row["state"] != "settled" and not is_money(row.get("reservation_upper_bound_usd"))
    )]
    return {
        "rows": len(physical),
        "physical_calls": len(physical),
        "fully_reported_rows": sum(all(is_token(row.get(field)) for field in TOKEN_FIELDS[:2]) for row in physical),
        "metrics": metrics,
        "reported_tokens": sum(input_output) if input_output else None,
        "unknown_cost_rows": len(unknown_money),
    }


def parsed_time(value):
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp.astimezone(timezone.utc) if timestamp.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def independent_summary(rows):
    result = physical_summary(rows)
    sessions = [row for row in rows if row.get("kind") == "subscription_session"]
    physical = [row for row in rows if row.get("kind") == "attempt" and row.get("state") in PHYSICAL_STATES]
    effective = []
    normalized_rows = []
    for row in sessions:
        value = row.get("input_token_usage")
        valid = (isinstance(value, dict)
                 and sorted(value) == ["cache_read_tokens", "cache_write_tokens", "total_tokens"]
                 and all(item is None or is_token(item) for item in value.values()))
        normalized_rows.append(value if valid else {})
        effective.append({
            "prompt_tokens": value["total_tokens"] if valid else row.get("prompt_tokens"),
            "completion_tokens": row.get("completion_tokens"),
            "cached_tokens": value["cache_read_tokens"] if valid else row.get("cached_tokens"),
            "cache_write_tokens": value["cache_write_tokens"] if valid else row.get("cache_write_tokens"),
        })
    session_values = [value for row in effective for field in TOKEN_FIELDS[:2] if is_token(value := row.get(field))]
    known = sum(bool(row) for row in normalized_rows)
    result.update({
        "subscription_sessions": len(sessions),
        "unknown_subscription_sessions": sum(not all(is_token(row.get(field)) for field in TOKEN_FIELDS[:2]) for row in effective),
        "undated_rows": sum(parsed_time(row.get("ts")) is None for row in physical),
        "subscription_summary": {
            "rows": len(sessions),
            "fully_reported_rows": sum(all(is_token(row.get(field)) for field in TOKEN_FIELDS[:2]) for row in effective),
            "metrics": {field: metric(effective, field) for field in TOKEN_FIELDS},
            "reported_tokens": sum(session_values) if session_values else None,
            "normalized_input_usage": {"known": known, "missing": len(sessions) - known},
            "normalized_metrics": {field: metric(normalized_rows, field)
                                   for field in ("total_tokens", "cache_read_tokens", "cache_write_tokens")},
            "legacy_metrics": {field: metric(sessions, field) for field in TOKEN_FIELDS},
            "legacy_input_sessions": len(sessions) - known,
            "undated_rows": sum(parsed_time(row.get("ts")) is None for row in sessions),
        },
    })
    return result


def verify_selection(rows, query):
    """Reject even one exported row outside the declared normalized selection."""
    if not isinstance(query, dict) or query.get("period") not in ("all", "1h", "24h", "7d", "30d", "custom"):
        raise AssertionError("Export must contain its normalized query selection")
    if query["period"] == "all":
        if query.get("start") is not None or query.get("end") is not None:
            raise AssertionError("All-history query must not hide a time cutoff")
        start = end = None
    else:
        start, end = parsed_time(query.get("start")), parsed_time(query.get("end"))
        if start is None or end is None or start >= end:
            raise AssertionError("Export query must declare a valid UTC half-open window")
    for index, row in enumerate(rows):
        address = f"rows[{index}]"
        if start is not None:
            timestamp = parsed_time(row.get("ts"))
            if timestamp is None or not start <= timestamp < end:
                raise AssertionError(address + " falls outside the selected time window")
        for query_key, row_key, unknown in (("model", "model", "__unknown__"), ("route", "subscription_route", "__unknown__"), ("project", "project_id", "__unassigned__")):
            selected = query.get(query_key)
            if selected and (row.get(row_key) or unknown) != selected:
                raise AssertionError(address + " does not match selected " + query_key)
        task = query.get("task")
        if task:
            own = row.get("task_id") == task
            descendant = row.get("root_task_id") == task and not own
            if task == "__unassigned__":
                own = not row.get("task_id") and not row.get("root_task_id")
                descendant = False
            scope = query.get("scope", "all")
            matches = own if scope == "own" else descendant if scope == "descendants" else own or descendant
            if not matches:
                raise AssertionError(address + " does not match selected task and scope")


def verify_summary_exact(summary, path="summary"):
    """Check supplied lossless representations after independent numeric equality."""
    if "reported_tokens_exact" in summary:
        count = summary.get("reported_tokens")
        if summary["reported_tokens_exact"] != (str(count) if is_token(count) else None):
            raise AssertionError(path + ".reported_tokens_exact does not match arithmetic")
    for key, value in summary.items():
        if isinstance(value, dict):
            if "exact" in value:
                count = value.get("value")
                if value["exact"] != (str(count) if is_token(count) else None):
                    raise AssertionError(path + "." + key + ".exact does not match arithmetic")
            verify_summary_exact(value, path + "." + key)


def verify_export(document):
    if not isinstance(document.get("snapshot_id"), str) or not document["snapshot_id"]:
        raise AssertionError("Export is missing its snapshot ID")
    content = {key: value for key, value in document.items() if key != "content_sha256"}
    checksum = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    if document.get("content_sha256") != checksum:
        raise AssertionError("Export checksum does not match its frozen contents")
    verify_selection(document["rows"], document["query"])
    for row in document["rows"]:
        if "token_exact" in row:
            for field in TOKEN_FIELDS:
                expected = str(row[field]) if is_token(row.get(field)) else None
                if row["token_exact"].get(field) != expected:
                    raise AssertionError("Incorrect original exact token string: " + field)
        if row.get("input_token_usage") is not None:
            value = row["input_token_usage"]
            if sorted(value) != ["cache_read_tokens", "cache_write_tokens", "total_tokens"]:
                raise AssertionError("Invalid normalized input object in export")
            for field, count in value.items():
                if count is not None and not is_token(count):
                    raise AssertionError("Invalid normalized input count")
                if row.get("input_token_usage_exact", {}).get(field) != (str(count) if count is not None else None):
                    raise AssertionError("Incorrect normalized exact token string: " + field)
        if row.get("kind") == "subscription_session" and "session_token_exact" in row:
            normalized = row.get("input_token_usage")
            fields = {"prompt_tokens": "total_tokens", "cached_tokens": "cache_read_tokens",
                      "cache_write_tokens": "cache_write_tokens"}
            for field in TOKEN_FIELDS:
                count = normalized[fields[field]] if normalized is not None and field in fields else row.get(field)
                wanted = str(count) if is_token(count) else None
                if row["session_token_exact"].get(field) != wanted:
                    raise AssertionError("Incorrect effective session exact token string: " + field)
    expected = independent_summary(document["rows"])
    assert_subset_equal(expected, document["summary"])
    verify_summary_exact(document["summary"])
    return expected


def assert_subset_equal(expected, actual, path="summary"):
    """Compare every frozen field, preserving null equality and missing counts."""
    for key, wanted in expected.items():
        address = f"{path}.{key}"
        if key not in actual:
            raise AssertionError(f"Missing {address}")
        got = actual[key]
        if isinstance(wanted, dict):
            if not isinstance(got, dict):
                raise AssertionError(f"{address}: expected object, got {got!r}")
            assert_subset_equal(wanted, got, address)
        elif wanted is None:
            if got is not None:
                raise AssertionError(f"{address}: expected unknown/null, got {got!r}")
        elif isinstance(wanted, float):
            if type(got) not in (int, float) or abs(Decimal(str(got)) - Decimal(str(wanted))) > Decimal("0.000001"):
                raise AssertionError(f"{address}: expected {wanted!r}, got {got!r}")
        elif type(got) is not type(wanted) or got != wanted:
            raise AssertionError(f"{address}: expected {wanted!r} ({type(wanted).__name__}), got {got!r} ({type(got).__name__})")


def verify_fixture(path):
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    actual = physical_summary(fixture["rows"])
    frozen = fixture["oracle"]["all"]
    expected = {key: frozen[key] for key in actual}
    assert_subset_equal(expected, actual)
    return actual


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "fixtures" / "numeric_snapshot.json"
    document = json.loads(target.read_text(encoding="utf-8"))
    summary = verify_fixture(target) if "oracle" in document else verify_export(document)
    print(json.dumps({"oracle": "independent-decimal-v1", "snapshot_id": document.get("snapshot_id", document.get("revision")), "query": document.get("query", {"period": "all", "source": "frozen synthetic fixture"}), "summary": summary}, indent=2))
