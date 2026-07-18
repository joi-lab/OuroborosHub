"""Bounded, statistically honest cache-efficiency widget.

The widget deliberately reports only a sampled suffix of the append-only usage
ledger. One normalized sample projection is the sole authority for the headline,
tables, and both declarative Chart.js views.
"""
from __future__ import annotations

import asyncio
import json
import math
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

LEDGER_FALLBACK = Path("state") / "usage_attempts.jsonl"
MAX_TAIL_BYTES = 192 * 1024
MAX_PARSED_LINES = 2_000
MAX_BUCKETS = 8
MAX_RECENT_ROWS = 5
MAX_MODEL_ROWS = 4
MAX_DIAGNOSTICS = 7
POLL_INTERVAL_MS = 10_000
POLL_MAX_TICKS = 3
TERMINAL_STATE = "settled"

_DATA_DIR: Path | None = None


def _ledger_rel() -> Path:
    try:
        from ouroboros import usage_accounting

        value = getattr(usage_accounting, "LEDGER_REL", None)
        if value:
            return Path(value)
    except Exception:
        pass
    return LEDGER_FALLBACK


def _runtime_data_dir(api: Any) -> Path | None:
    try:
        info = api.get_runtime_info()
    except Exception:
        return None
    raw = info.get("data_dir") if isinstance(info, Mapping) else None
    return Path(raw).expanduser() if isinstance(raw, str) and raw.strip() else None


def _lookup(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value: Any = row
        for part in key.split("."):
            if not isinstance(value, Mapping):
                value = None
                break
            value = value.get(part)
        if value is not None:
            return value
    return None


def _whole_number(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        return None
    return int(number)


def _timestamp(row: Mapping[str, Any]) -> datetime | None:
    raw = _lookup(row, "ts", "settled_at", "finished_at", "updated_at", "timestamp")
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _read_suffix(path: Path) -> tuple[list[str], dict[str, Any]]:
    coverage: dict[str, Any] = {
        "exists": path.is_file(), "bytes_total": 0, "bytes_read": 0,
        "suffix_only": False, "partial_first_line_discarded": False,
        "whole_suffix_discarded": False,
    }
    if not coverage["exists"]:
        return [], coverage
    try:
        size = path.stat().st_size
        offset = max(0, size - MAX_TAIL_BYTES)
        coverage.update(bytes_total=size, bytes_read=min(size, MAX_TAIL_BYTES), suffix_only=offset > 0)
        with path.open("rb") as handle:
            if offset:
                handle.seek(offset - 1)
                preceding = handle.read(1)
                handle.seek(offset)
                blob = handle.read(MAX_TAIL_BYTES)
                if preceding != b"\n":
                    newline = blob.find(b"\n")
                    coverage["partial_first_line_discarded"] = newline >= 0
                    coverage["whole_suffix_discarded"] = newline < 0
                    blob = blob[newline + 1 :] if newline >= 0 else b""
            else:
                blob = handle.read(MAX_TAIL_BYTES)
    except OSError as exc:
        coverage["read_error"] = type(exc).__name__
        return [], coverage
    lines = blob.decode("utf-8", errors="replace").splitlines()
    coverage["tail_lines_seen"] = len(lines)
    coverage["line_cap_applied"] = len(lines) > MAX_PARSED_LINES
    return lines[-MAX_PARSED_LINES:], coverage


def _latest_attempts(lines: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep the latest observed record for each id; raw failures remain separate."""
    latest: "OrderedDict[str, tuple[int, dict[str, Any]]]" = OrderedDict()
    stats = {"rows_seen": 0, "malformed": 0, "non_object": 0, "missing_id": 0, "duplicates_discarded": 0}
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        stats["rows_seen"] += 1
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            stats["malformed"] += 1
            continue
        if not isinstance(row, dict):
            stats["non_object"] += 1
            continue
        attempt_id = _lookup(row, "attempt_id")
        if attempt_id is None or not str(attempt_id).strip():
            stats["missing_id"] += 1
            continue
        key = str(attempt_id)
        if key in latest:
            stats["duplicates_discarded"] += 1
        latest[key] = (index, row)
    return [row for _, row in sorted(latest.values(), key=lambda item: item[0])], stats


def _percent(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 2) if denominator else None


def _unavailable(message: str, coverage: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "summary": {"cache_read_rate": None, "valid_samples": 0, "cached_tokens_millions": None, "prompt_tokens_millions": None},
        "window_rows": [{"key": key, "value": value} for key, value in coverage.items()],
        "quality_rows": [], "recent_rows": [], "model_rows": [],
        "trend": {"labels": [], "datasets": []},
        "composition": {"labels": [], "datasets": []},
        "diagnostics_md": f"- {message}",
        "integrity": {"ledger_unavailable": True},
    }


def _sample_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Classify each deduplicated record once and return comparable observations.

    A sample with a known cache measurement contributes to the weighted rate;
    an otherwise usable sample with no cache measurement contributes only to the
    composition chart's explicit unknown-token stack.
    """
    quality = {
        "nonterminal": 0, "missing_timestamp": 0, "no_denominator": 0,
        "unknown_cache": 0, "invalid_tokens": 0, "valid_samples": 0,
    }
    samples: list[dict[str, Any]] = []
    for row in rows:
        if str(_lookup(row, "state") or "").lower() != TERMINAL_STATE:
            quality["nonterminal"] += 1
            continue
        stamp = _timestamp(row)
        if stamp is None:
            quality["missing_timestamp"] += 1
            continue
        prompt = _whole_number(_lookup(row, "prompt_tokens", "usage.prompt_tokens", "input_tokens"))
        if prompt is None or prompt <= 0:
            quality["no_denominator"] += 1
            continue
        raw_cached = _lookup(row, "cached_tokens", "usage.cached_tokens", "cache_read_input_tokens")
        cached = _whole_number(raw_cached)
        if raw_cached is None:
            quality["unknown_cache"] += 1
        elif cached is None or cached > prompt:
            quality["invalid_tokens"] += 1
            continue
        else:
            quality["valid_samples"] += 1
        samples.append({
            "at": stamp,
            "model": str(_lookup(row, "model") or "unknown")[:120],
            "prompt_tokens": prompt,
            "cached_tokens": cached,
        })
    return samples, quality


def _bucket_rows(samples: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Create bounded chronological buckets at a truthful observed-window scale."""
    sample_list = list(samples)
    if not sample_list:
        return []
    oldest = min(sample["at"] for sample in sample_list)
    newest = max(sample["at"] for sample in sample_list)
    span_seconds = (newest - oldest).total_seconds()
    if span_seconds <= 2 * 60 * 60:
        minutes = 15
    elif span_seconds <= 2 * 24 * 60 * 60:
        minutes = 60
    else:
        minutes = 24 * 60
    grouped: dict[str, dict[str, Any]] = {}
    for sample in sample_list:
        stamp = sample["at"]
        if minutes == 24 * 60:
            label = stamp.strftime("%Y-%m-%d UTC")
        else:
            bucket_minute = (stamp.minute // minutes) * minutes
            label = stamp.replace(minute=bucket_minute, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M UTC")
        bucket = grouped.setdefault(label, {"label": label, "prompt": 0, "cached": 0, "unknown": 0, "valid": 0, "samples": 0})
        prompt = int(sample["prompt_tokens"])
        bucket["samples"] += 1
        if sample["cached_tokens"] is None:
            bucket["unknown"] += prompt
        else:
            bucket["prompt"] += prompt
            bucket["cached"] += int(sample["cached_tokens"])
            bucket["valid"] += 1
    buckets = [grouped[key] for key in sorted(grouped)[-MAX_BUCKETS:]]
    for bucket in buckets:
        bucket["uncached"] = bucket["prompt"] - bucket["cached"]
        bucket["rate"] = _percent(bucket["cached"], bucket["prompt"])
    return buckets


def _chart_payload(buckets: list[Mapping[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    labels = [str(bucket["label"]) for bucket in buckets]
    trend = {
        "labels": labels,
        "datasets": [
            {"label": "Cache-read rate (%)", "data": [bucket["rate"] for bucket in buckets]},
        ],
    }
    composition = {
        "labels": labels,
        "datasets": [
            {"label": "Cached prompt tokens", "data": [bucket["cached"] for bucket in buckets]},
            {"label": "Uncached prompt tokens", "data": [bucket["uncached"] for bucket in buckets]},
            {"label": "Unknown cache measurement", "data": [bucket["unknown"] for bucket in buckets]},
        ],
    }
    return trend, composition


def project_ledger(path: Path) -> dict[str, Any]:
    """Return one complete fixed-cardinality snapshot from a bounded suffix."""
    lines, coverage = _read_suffix(path)
    if not coverage.get("exists"):
        return _unavailable("Usage ledger is not available yet; no cache metric can be calculated.", coverage)
    if coverage.get("read_error"):
        return _unavailable(f"Usage ledger could not be read ({coverage['read_error']}).", coverage)

    rows, raw_stats = _latest_attempts(lines)
    samples, quality = _sample_rows(rows)
    buckets = _bucket_rows(samples)
    trend, composition = _chart_payload(buckets)
    comparable = [sample for sample in samples if sample["cached_tokens"] is not None]
    cached_total = sum(int(sample["cached_tokens"] or 0) for sample in comparable)
    prompt_total = sum(int(sample["prompt_tokens"]) for sample in comparable)
    oldest = min((sample["at"] for sample in samples), default=None)
    newest = max((sample["at"] for sample in samples), default=None)

    models: dict[str, dict[str, int | str]] = {}
    for sample in comparable:
        model = str(sample["model"])
        item = models.setdefault(model, {"model": model, "cached": 0, "prompt": 0, "samples": 0})
        item["cached"] = int(item["cached"]) + int(sample["cached_tokens"] or 0)
        item["prompt"] = int(item["prompt"]) + int(sample["prompt_tokens"])
        item["samples"] = int(item["samples"]) + 1
    model_rows = [
        {"model": item["model"], "cache_read_rate": _percent(int(item["cached"]), int(item["prompt"])), "prompt_tokens": item["prompt"], "samples": item["samples"]}
        for item in sorted(models.values(), key=lambda value: (int(value["prompt"]), str(value["model"])), reverse=True)[:MAX_MODEL_ROWS]
    ]
    recent_rows = [
        {"at": sample["at"].strftime("%Y-%m-%d %H:%M UTC"), "model": sample["model"], "prompt_tokens": sample["prompt_tokens"], "cache": "unknown" if sample["cached_tokens"] is None else _percent(int(sample["cached_tokens"]), int(sample["prompt_tokens"]))}
        for sample in samples[-MAX_RECENT_ROWS:]
    ]
    quality_rows = [
        {"key": "Malformed JSONL rows", "value": raw_stats["malformed"]},
        {"key": "Duplicate rows discarded", "value": raw_stats["duplicates_discarded"]},
        {"key": "Non-terminal attempts", "value": quality["nonterminal"]},
        {"key": "Missing UTC timestamp", "value": quality["missing_timestamp"]},
        {"key": "No prompt-token denominator", "value": quality["no_denominator"]},
        {"key": "Unknown cache measurement", "value": quality["unknown_cache"]},
        {"key": "Invalid token relationship", "value": quality["invalid_tokens"]},
    ]
    diagnostics = []
    if coverage["suffix_only"]:
        diagnostics.append(f"Observed suffix only: {coverage['bytes_read']:,} of {coverage['bytes_total']:,} ledger bytes.")
    if coverage["partial_first_line_discarded"]:
        diagnostics.append("Partial first suffix line was discarded, not counted as malformed.")
    if coverage["whole_suffix_discarded"]:
        diagnostics.append("The bounded suffix contained no complete JSONL line and was discarded; no cache metric is available from this read.")
    if coverage.get("line_cap_applied"):
        diagnostics.append(f"Line cap applied: parsed the last {MAX_PARSED_LINES:,} of {coverage['tail_lines_seen']:,} suffix lines.")
    if not prompt_total:
        diagnostics.append("No comparable prompt-token denominator: cache-read rate is unavailable, not 0%.")
    if quality["unknown_cache"]:
        diagnostics.append("Unknown cache measurements are kept outside the cache-rate denominator and shown separately in composition.")
    diagnostics = diagnostics[:MAX_DIAGNOSTICS]
    window_rows = [
        {"key": "Observed bytes", "value": coverage["bytes_read"]},
        {"key": "Suffix only", "value": bool(coverage["suffix_only"])},
        {"key": "Whole suffix discarded", "value": bool(coverage["whole_suffix_discarded"])},
        {"key": "Rows inspected", "value": raw_stats["rows_seen"]},
        {"key": "Line cap applied", "value": bool(coverage.get("line_cap_applied"))},
        {"key": "Valid cache samples", "value": quality["valid_samples"]},
        {"key": "Cached prompt tokens (exact)", "value": cached_total},
        {"key": "Prompt-token denominator (exact)", "value": prompt_total},
        {"key": "Observed UTC window", "value": f"{oldest.isoformat() if oldest else '—'} → {newest.isoformat() if newest else '—'}"},
    ]
    return {
        "status": "done",
        "summary": {"cache_read_rate": _percent(cached_total, prompt_total), "valid_samples": quality["valid_samples"], "cached_tokens_millions": round(cached_total / 1_000_000, 2), "prompt_tokens_millions": round(prompt_total / 1_000_000, 2)},
        "window_rows": window_rows,
        "quality_rows": quality_rows,
        "recent_rows": recent_rows,
        "model_rows": model_rows,
        "trend": trend,
        "composition": composition,
        "diagnostics_md": "\n".join(f"- {line}" for line in diagnostics) or "- No data-quality warnings in the observed suffix.",
        "integrity": {**raw_stats, **quality, "bucket_count": len(buckets), "max_buckets": MAX_BUCKETS, "chart_or_canvas_used": True},
    }


def _render() -> dict[str, Any]:
    return {
        "kind": "declarative", "schema_version": 1, "span": 2,
        "components": [
            {"id": "snapshot-poll", "type": "poll", "route": "data", "target": "result", "auto_start": True, "interval_ms": POLL_INTERVAL_MS, "max_ticks": POLL_MAX_TICKS, "label": "Refresh snapshot"},
            {"id": "summary", "type": "group", "title": "Observed cache efficiency", "description": "Token-weighted cache-read rate in the bounded ledger suffix — not lifetime efficiency.", "layout": "grid", "columns": 2, "target": "result", "components": [
                {"id": "cache-read-rate", "type": "metric", "label": "Cache-read rate", "path": "summary.cache_read_rate", "unit": "%", "precision": 2},
                {"id": "valid-samples", "type": "metric", "label": "Valid cache samples", "path": "summary.valid_samples", "precision": 0},
                {"id": "cached-tokens", "type": "metric", "label": "Cached tokens (M)", "path": "summary.cached_tokens_millions", "precision": 2, "tone": "success"},
                {"id": "prompt-denominator", "type": "metric", "label": "Prompt denominator (M)", "path": "summary.prompt_tokens_millions", "precision": 2},
            ]},
            {"id": "trend-chart", "type": "chart", "target": "result", "path": "trend", "chart_type": "line", "aria_label": "Token-weighted cache-read rate by observed UTC time bucket", "label": "Cache-read rate by observed UTC time bucket", "unit": "%"},
            {"id": "composition-chart", "type": "chart", "target": "result", "path": "composition", "chart_type": "bar", "aria_label": "Cached, uncached, and unknown-cache prompt tokens by observed UTC time bucket", "label": "Prompt-token composition by observed UTC time bucket", "unit": "tokens"},
            {"id": "window", "type": "key_value", "target": "result", "path": "window_rows"},
            {"id": "quality", "type": "table", "target": "result", "path": "quality_rows", "columns": [{"label": "Data-quality fact", "path": "key"}, {"label": "Count", "path": "value"}]},
            {"id": "recent", "type": "table", "target": "result", "path": "recent_rows", "columns": [{"label": "Observed at", "path": "at"}, {"label": "Model", "path": "model"}, {"label": "Prompt tokens", "path": "prompt_tokens"}, {"label": "Cache rate", "path": "cache"}]},
            {"id": "models", "type": "table", "target": "result", "path": "model_rows", "columns": [{"label": "Model", "path": "model"}, {"label": "Cache-read rate", "path": "cache_read_rate"}, {"label": "Prompt tokens", "path": "prompt_tokens"}, {"label": "Samples", "path": "samples"}]},
            {"id": "diagnostics", "type": "markdown", "target": "result", "path": "diagnostics_md"},
        ],
    }


def register(api: Any) -> None:
    global _DATA_DIR
    _DATA_DIR = _runtime_data_dir(api)

    async def route(_request: Any) -> dict[str, Any]:
        if _DATA_DIR is None:
            return _unavailable("Runtime data directory is unavailable.", {})
        return await asyncio.to_thread(project_ledger, _DATA_DIR / _ledger_rel())

    api.register_route("data", route, methods=("GET",))
    api.register_ui_tab("cache_efficiency", "Cache Efficiency Snapshot", icon="activity", render=_render())
