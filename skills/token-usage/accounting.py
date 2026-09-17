"""Pure arithmetic over the disposable numeric projection. No runtime I/O."""
from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
NORMALIZED_FIELDS = ("total_tokens", "cache_read_tokens", "cache_write_tokens")
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens")
PHYSICAL_STATES = frozenset(("dispatched", "settled", "unresolved"))
PERIODS = {"1h": timedelta(hours=1), "24h": timedelta(days=1),
           "7d": timedelta(days=7), "30d": timedelta(days=30)}
UNKNOWN = "__unknown__"
UNASSIGNED = "__unassigned__"
QUERY_KEYS = frozenset(("period", "start", "end", "model", "route", "project", "task",
                        "scope", "page", "page_size"))
NOTICES = [
    "Reported input and generated output are recorded counters, not unique documents or all processing. Missing values are unknown, never zero.",
    "Harness sessions use valid normalized input totals and caches when recorded, otherwise legacy input counters. A normalized null stays unknown. Normalized and legacy counters are never added together.",
    "Cache read and cache write are non-additive: do not add them to input. Retained legacy input counters can use different or inconsistent semantics; uncached input is not derived.",
    "Reasoning: not separately reported. Requested and observed executors are not available as separate dimensions. Recorded model and recorded route are metadata, not proof of the executor.",
    "Only physical attempt records in dispatched, settled or unresolved states enter the main metrics. Subscription sessions are aggregate records and are shown separately; legacy totals are excluded.",
    "Costs use recorded confirmed, estimated and held amounts only. Held amounts are reservation upper bounds, not spending; missing finality or price is unknown. No tariffs or savings are inferred.",
    "All filters apply before charts, rankings, totals and detail pagination. Time windows are UTC [start, end); undated records are included only in All retained history.",
]


class QueryError(ValueError):
    """A client supplied an unsupported or invalid selection."""


def number(value, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if integer:
        return value if isinstance(value, int) and value >= 0 else None
    try:
        if not math.isfinite(value) or value < 0:
            return None
    except OverflowError:
        return None
    return value


def parsed_time(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def metric(rows, field, *, integer=False):
    values = [number(row.get(field), integer=integer) for row in rows]
    reported = [value for value in values if value is not None]
    value = sum(reported) if reported else None
    result = {"value": value, "known": len(reported), "missing": len(values) - len(reported)}
    if integer:
        result["exact"] = str(value) if value is not None else None
    return result


def token_summary(rows):
    metrics = {field: metric(rows, field, integer=True) for field in TOKEN_FIELDS}
    known = [metrics[field]["value"] for field in TOKEN_FIELDS[:2]
             if metrics[field]["value"] is not None]
    return {"rows": len(rows), "metrics": metrics,
            "reported_tokens": sum(known) if known else None,
            "reported_tokens_exact": str(sum(known)) if known else None,
            "fully_reported_rows": sum(all(number(row.get(field), integer=True) is not None
                                            for field in TOKEN_FIELDS[:2]) for row in rows)}


def normalized_input_usage(row):
    """Mirror the source contract: malformed objects are absent as a whole."""
    value = row.get("input_token_usage")
    if not isinstance(value, dict) or set(value) != set(NORMALIZED_FIELDS):
        return None
    if any(item is not None and (type(item) is not int or item < 0) for item in value.values()):
        return None
    return value


def session_projection(row):
    result = {field: row.get(field) for field in TOKEN_FIELDS}
    normalized = normalized_input_usage(row)
    if normalized is not None:
        for target, source in zip(("prompt_tokens", "cached_tokens", "cache_write_tokens"), NORMALIZED_FIELDS):
            result[target] = normalized[source]
    return result


def session_summary(rows):
    effective = [session_projection(row) for row in rows]
    normalized = [normalized_input_usage(row) for row in rows]
    known = sum(value is not None for value in normalized)
    result = token_summary(effective)
    result.update(normalized_input_usage={"known": known, "missing": len(rows) - known},
                  normalized_metrics={field: metric([value or {} for value in normalized], field, integer=True)
                                      for field in NORMALIZED_FIELDS},
                  legacy_metrics={field: metric(rows, field, integer=True) for field in TOKEN_FIELDS},
                  legacy_input_sessions=len(rows) - known,
                  undated_rows=sum(parsed_time(row.get("ts")) is None for row in rows))
    return result


def exact_row(row):
    """Lossless companions cover original, effective and nested input counters."""
    result = dict(row, token_exact={field: str(row[field])
                                   if number(row.get(field), integer=True) is not None else None
                                   for field in TOKEN_FIELDS})
    if row.get("kind") == "subscription_session":
        normalized = normalized_input_usage(row)
        result["input_token_usage"] = dict(normalized) if normalized is not None else None
        result["input_token_usage_exact"] = ({field: str(normalized[field]) if normalized[field] is not None else None
                                              for field in NORMALIZED_FIELDS} if normalized is not None else None)
        effective = session_projection(row)
        result["session_token_exact"] = {field: str(effective[field])
                                          if number(effective[field], integer=True) is not None else None
                                          for field in TOKEN_FIELDS}
    return result


def summarize(rows):
    """Never combines aggregate sessions, legacy totals or reservations with calls."""
    physical = [row for row in rows if row.get("kind") == "attempt"
                and row.get("state") in PHYSICAL_STATES]
    sessions = [row for row in rows if row.get("kind") == "subscription_session"]
    result = token_summary(physical)
    result.update(physical_calls=len(physical), subscription_sessions=len(sessions),
                  subscription_summary=session_summary(sessions),
                  unknown_subscription_sessions=sum(
                      any(number(session_projection(row).get(field), integer=True) is None for field in TOKEN_FIELDS[:2])
                      for row in sessions),
                  undated_rows=sum(parsed_time(row.get("ts")) is None for row in physical),
                  excluded_legacy_rows=sum(row.get("kind") in ("legacy_metadata", "legacy_delta") for row in rows),
                  reservations=sum(row.get("kind") == "attempt" and row.get("state") == "reserved" for row in rows))
    confirmed = [r for r in physical if r.get("state") == "settled" and r.get("cost_final") is True]
    estimated = [r for r in physical if r.get("state") == "settled" and r.get("cost_final") is False]
    held = [r for r in physical if r.get("state") in ("dispatched", "unresolved")]
    result["metrics"].update(cost_confirmed_usd=metric(confirmed, "cost_usd"),
                             cost_estimated_usd=metric(estimated, "cost_usd"),
                             cost_held_usd=metric(held, "reservation_upper_bound_usd"))
    result["unknown_cost_rows"] = sum(
        (r.get("cost_final") not in (True, False) or not isinstance(r.get("cost_final"), bool)
         or number(r.get("cost_usd")) is None) if r.get("state") == "settled"
        else number(r.get("reservation_upper_bound_usd")) is None for r in physical)
    result["reserved_hold_usd"] = metric([r for r in rows if r.get("kind") == "attempt"
                                         and r.get("state") == "reserved"], "reservation_upper_bound_usd")
    return result


def dimension(row, kind):
    if kind == "model":
        return row.get("model") or UNKNOWN
    if kind == "route":
        return row.get("subscription_route") or UNKNOWN
    if kind == "project":
        return row.get("project_id") or UNASSIGNED
    if kind == "task":
        return row.get("root_task_id") or row.get("task_id") or UNASSIGNED
    raise ValueError(kind)


def label_for(row, kind):
    key = dimension(row, kind)
    if key == UNKNOWN:
        return "Unknown"
    if key == UNASSIGNED:
        return "Unassigned"
    if kind == "project":
        return row.get("project_name") or key
    return key


def normalize_query(params, now=None):
    unknown = set(params) - QUERY_KEYS
    if unknown:
        raise QueryError("Unsupported query parameter: " + sorted(unknown)[0])
    query = {"period": "all", "model": "", "route": "", "project": "", "task": "",
             "scope": "all", "page": 1, "page_size": 25}
    for key, value in params.items():
        if key in ("page", "page_size"):
            try:
                if isinstance(value, bool) or str(int(value)) != str(value):
                    raise ValueError()
                value = int(value)
            except (ValueError, TypeError):
                raise QueryError(key + " must be an integer") from None
        elif not isinstance(value, str) or len(value) > 256:
            raise QueryError(key + " must be a short string")
        query[key] = value
    if query["period"] not in (*PERIODS, "all", "custom"):
        raise QueryError("Unknown period")
    if query["scope"] not in ("all", "own", "descendants"):
        raise QueryError("Unknown task scope")
    if not 1 <= query["page"] <= 1_000_000 or not 1 <= query["page_size"] <= 100:
        raise QueryError("Page must be positive; page_size must be between 1 and 100")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise QueryError("now must have a timezone")
    if query["period"] == "custom":
        start, end = parsed_time(query.get("start")), parsed_time(query.get("end"))
        if start is None or end is None or start >= end:
            raise QueryError("Custom start and end must be timezone-aware ISO dates with start < end")
        query.update(start=iso(start), end=iso(end))
    elif query["period"] in PERIODS:
        query.update(start=iso(now - PERIODS[query["period"]]), end=iso(now))
    else:
        query.update(start=None, end=None)
    return query


def select_rows(rows, query):
    start, end = parsed_time(query["start"]), parsed_time(query["end"])
    selected = []
    for row in rows:
        timestamp = parsed_time(row.get("ts"))
        if start is not None and (timestamp is None or not start <= timestamp < end):
            continue
        if any(query[kind] and dimension(row, kind) != query[kind]
               for kind in ("model", "route", "project")):
            continue
        task = query["task"]
        if task:
            own = (row.get("task_id") or UNASSIGNED) == task
            descendant = row.get("root_task_id") == task and not own
            if task == UNASSIGNED:
                own = not row.get("task_id") and not row.get("root_task_id")
                descendant = False
            if query["scope"] == "own" and not own:
                continue
            if query["scope"] == "descendants" and not descendant:
                continue
            if query["scope"] == "all" and not (own or descendant):
                continue
        selected.append(row)
    return selected


def groups(rows, kind, summarizer=summarize):
    grouped = defaultdict(list)
    for row in rows:
        grouped[dimension(row, kind)].append(row)
    result = [{"id": key, "label": label_for(items[0], kind), "summary": summarizer(items)}
              for key, items in grouped.items()]
    return sorted(result, key=lambda item: (-(item["summary"]["reported_tokens"] or 0), item["id"]))


def trend(rows, query, summarizer=summarize):
    dated = [(parsed_time(row.get("ts")), row) for row in rows]
    dated = [(ts, row) for ts, row in dated if ts is not None]
    if not dated:
        return []
    start = parsed_time(query["start"]) or min(ts for ts, _ in dated)
    end = parsed_time(query["end"]) or max(ts for ts, _ in dated) + timedelta(microseconds=1)
    count = {"1h": 12, "24h": 24, "7d": 28, "30d": 30}.get(query["period"], 30)
    span = end - start
    microseconds = (span.days * 86400 + span.seconds) * 1_000_000 + span.microseconds
    count = min(count, microseconds)
    boundaries = [start + timedelta(microseconds=microseconds * i // count) for i in range(count + 1)]
    buckets = [[] for _ in range(count)]
    for ts, row in dated:
        bucket = min(count - 1, max(0, bisect_right(boundaries, ts) - 1))
        buckets[bucket].append(row)
    return [{"start": iso(boundaries[i]), "end": iso(boundaries[i + 1]),
             "label": boundaries[i].strftime("%m-%d %H:%M UTC"),
             "summary": summarizer(items)} for i, items in enumerate(buckets)]


def snapshot_identity(snapshot):
    if snapshot.get("snapshot_id"):
        return str(snapshot["snapshot_id"])
    # A standalone test/export identity; the running index supplies its own stable revision ID.
    payload = json.dumps(snapshot.get("rows", []), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def query_snapshot(snapshot, params, now=None):
    now = now or datetime.now(UTC)
    query = normalize_query(params, now)
    rows = snapshot.get("rows", [])
    selected = select_rows(rows, query)
    retained_dates = [ts for row in rows if (ts := parsed_time(row.get("ts"))) is not None]
    coverage = dict(snapshot.get("coverage", {}))
    coverage.update(retained_start=iso(min(retained_dates)) if retained_dates else None,
                    retained_end=iso(max(retained_dates)) if retained_dates else None,
                    retained_undated_rows=len(rows) - len(retained_dates))
    physical = [row for row in selected if row.get("kind") == "attempt" and row.get("state") in PHYSICAL_STATES]
    sessions = [row for row in selected if row.get("kind") == "subscription_session"]
    # Facets use the entire indexed projection so an empty selection can be corrected.
    facets = {}
    for plural, kind in (("models", "model"), ("routes", "route"), ("projects", "project"), ("tasks", "task")):
        labels = {dimension(row, kind): label_for(row, kind) for row in rows}
        facets[plural] = [{"id": key, "label": labels[key]} for key in sorted(labels)]
    # Null timestamps sort last and never acquire invented dates.
    ordered = sorted(selected, key=lambda row: (parsed_time(row.get("ts")) or datetime.min.replace(tzinfo=UTC),
                                                 str(row.get("attempt_id", ""))), reverse=True)
    offset = (query["page"] - 1) * query["page_size"]
    query_hash = hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()[:16]
    return {"schema_version": 1, "snapshot_id": snapshot_identity(snapshot) + ":" + query_hash,
            "generated_at": iso(now), "query": query, "coverage": coverage,
            "summary": summarize(selected), "trend": trend(physical, query),
            "session_trend": trend(sessions, query, session_summary),
            "session_rankings": {plural: groups(sessions, kind, session_summary) for plural, kind in
                                 (("models", "model"), ("routes", "route"), ("projects", "project"), ("tasks", "task"))}, "facets": facets,
            "rankings": {plural: groups(physical, kind) for plural, kind in
                         (("models", "model"), ("routes", "route"), ("projects", "project"), ("tasks", "task"))},
            "details": {"rows": [exact_row(row) for row in ordered[offset:offset + query["page_size"]]], "total": len(selected),
                        "page": query["page"], "page_size": query["page_size"]}, "notices": NOTICES}


def export_snapshot(snapshot, params, now=None):
    """A frozen selection for an independent oracle; metadata and numbers only."""
    result = query_snapshot(snapshot, params, now)
    rows = select_rows(snapshot.get("rows", []), result["query"])
    content = {"schema_version": 1, "snapshot_id": result["snapshot_id"], "query": result["query"],
               "generated_at": result["generated_at"], "coverage": result["coverage"],
               "rows": [exact_row(row) for row in rows], "summary": result["summary"]}
    content["content_sha256"] = hashlib.sha256(json.dumps(content, sort_keys=True,
                                                         separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return content
