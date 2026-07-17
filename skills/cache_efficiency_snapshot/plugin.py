"""Bounded, chart-free cache-efficiency snapshot widget.

This intentionally diverges from cache_hit_rate's chart presentation: only the
pure suffix-reader/projection ideas are retained so this payload has no canvas
or Chart.js dependency.
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
MAX_RECENT_ROWS = 5
MAX_MODEL_ROWS = 4
MAX_DIAGNOSTICS = 6
POLL_INTERVAL_MS = 10_000
POLL_MAX_TICKS = 3
TERMINAL_STATES = {"settled", "unresolved", "released"}

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
    except (TypeError, ValueError):
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


def _cache_hit(row: Mapping[str, Any]) -> bool | None:
    explicit = _lookup(row, "cache_hit", "cache.hit", "cached", "from_cache")
    if isinstance(explicit, bool):
        return explicit
    if isinstance(explicit, str):
        text = explicit.strip().lower()
        if text in {"true", "hit", "yes", "1"}:
            return True
        if text in {"false", "miss", "no", "0"}:
            return False
    cached = _whole_number(_lookup(row, "cached_tokens", "usage.cached_tokens", "cache_read_input_tokens"))
    return cached > 0 if cached is not None else None


def _read_suffix(path: Path) -> tuple[list[str], dict[str, Any]]:
    coverage: dict[str, Any] = {"exists": path.is_file(), "bytes_total": 0, "bytes_read": 0, "suffix_only": False, "partial_first_line_discarded": False}
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
                    blob = blob[newline + 1 :] if newline >= 0 else b""
            else:
                blob = handle.read(MAX_TAIL_BYTES)
    except OSError as exc:
        coverage["read_error"] = type(exc).__name__
        return [], coverage
    lines = blob.decode("utf-8", errors="replace").splitlines()
    coverage["line_cap_applied"] = len(lines) > MAX_PARSED_LINES
    return lines[-MAX_PARSED_LINES:], coverage


def _latest_attempts(lines: Iterable[str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
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
        "summary": {"hit_rate": None, "known_samples": 0, "hits": 0, "misses": 0},
        "recent_rows": [],
        "model_rows": [],
        "coverage_rows": [{"key": key, "value": value} for key, value in coverage.items()],
        "diagnostics_md": f"- {message}",
        "integrity": {"ledger_unavailable": True},
    }


def project_ledger(path: Path) -> dict[str, Any]:
    """Return one complete, fixed-cardinality read-only snapshot."""
    lines, coverage = _read_suffix(path)
    if not coverage.get("exists"):
        return _unavailable("Usage ledger is not available yet; no cache metric can be calculated.", coverage)
    if coverage.get("read_error"):
        return _unavailable(f"Usage ledger could not be read ({coverage['read_error']}).", coverage)

    rows, stats = _latest_attempts(lines)
    totals = {"hits": 0, "misses": 0, "unknown": 0, "nonterminal": 0, "invalid": 0}
    recent_rows: list[dict[str, Any]] = []
    models: dict[str, dict[str, Any]] = {}
    newest: str | None = None
    for row in rows:
        if str(_lookup(row, "state") or "").lower() not in TERMINAL_STATES:
            totals["nonterminal"] += 1
            continue
        stamp = _timestamp(row)
        if stamp is None:
            totals["invalid"] += 1
            continue
        prompt = _whole_number(_lookup(row, "prompt_tokens", "usage.prompt_tokens"))
        cached = _whole_number(_lookup(row, "cached_tokens", "usage.cached_tokens", "cache_read_input_tokens"))
        if prompt is not None and cached is not None and cached > prompt:
            totals["invalid"] += 1
            continue
        hit = _cache_hit(row)
        if hit is None:
            totals["unknown"] += 1
        else:
            totals["hits" if hit else "misses"] += 1
        newest = max(newest or stamp.isoformat(), stamp.isoformat())
        model = str(_lookup(row, "model") or "unknown")[:120]
        model_summary = models.setdefault(model, {"model": model, "hits": 0, "misses": 0})
        if hit is not None:
            model_summary["hits" if hit else "misses"] += 1
        recent_rows.append({"at": stamp.strftime("%Y-%m-%d %H:%M UTC"), "model": model, "cache": "hit" if hit else "miss" if hit is False else "unknown"})

    diagnostics: list[str] = []
    if coverage.get("suffix_only"):
        diagnostics.append(f"Охвачен только хвост ledger: {coverage['bytes_read']:,} из {coverage['bytes_total']:,} байт.")
    if coverage.get("partial_first_line_discarded"):
        diagnostics.append("Неполная первая строка хвоста отброшена и не считается повреждённой.")
    for field, label in (("malformed", "Повреждённые JSONL-строки"), ("duplicates_discarded", "Повторные попытки отброшены"), ("nonterminal", "Незавершённые попытки исключены"), ("unknown", "Попытки без cache-сигнала"), ("invalid", "Строки с некорректными данными исключены")):
        if stats.get(field) or totals.get(field):
            diagnostics.append(f"{label}: {stats.get(field, totals.get(field, 0)) or totals.get(field, 0)}.")
    diagnostics = diagnostics[:MAX_DIAGNOSTICS]
    known = totals["hits"] + totals["misses"]
    model_rows = []
    for model in sorted(models.values(), key=lambda item: (item["hits"] + item["misses"], item["model"]), reverse=True)[:MAX_MODEL_ROWS]:
        model_rows.append({"model": model["model"], "hit_rate": _percent(model["hits"], model["hits"] + model["misses"]), "samples": model["hits"] + model["misses"]})
    coverage_rows = [
        {"key": "Observed bytes", "value": coverage["bytes_read"]},
        {"key": "Suffix only", "value": bool(coverage["suffix_only"])},
        {"key": "Latest terminal row", "value": newest or "—"},
        {"key": "Rows inspected", "value": stats["rows_seen"]},
    ]
    return {
        "status": "done",
        "summary": {"hit_rate": _percent(totals["hits"], known), "known_samples": known, "hits": totals["hits"], "misses": totals["misses"]},
        "recent_rows": recent_rows[-MAX_RECENT_ROWS:],
        "model_rows": model_rows,
        "coverage_rows": coverage_rows,
        "diagnostics_md": "\n".join(f"- {line}" for line in diagnostics) or "- No data-quality warnings in the observed suffix.",
        "integrity": {**stats, **totals, "chart_or_canvas_used": False, "max_recent_rows": MAX_RECENT_ROWS, "max_model_rows": MAX_MODEL_ROWS},
    }


def _render() -> dict[str, Any]:
    return {
        "kind": "declarative",
        "schema_version": 1,
        "span": 2,
        "components": [
            {"id": "snapshot-poll", "type": "poll", "route": "data", "target": "result", "auto_start": True, "interval_ms": POLL_INTERVAL_MS, "max_ticks": POLL_MAX_TICKS, "stop_path": "status", "stop_value": "unavailable", "label": "Refresh snapshot"},
            {"id": "summary", "type": "group", "title": "Observed cache efficiency", "layout": "grid", "columns": 2, "target": "result", "components": [
                {"id": "hit-rate", "type": "metric", "label": "Hit rate", "path": "summary.hit_rate", "unit": "%", "precision": 2},
                {"id": "known-samples", "type": "metric", "label": "Known samples", "path": "summary.known_samples", "precision": 0},
                {"id": "hits", "type": "metric", "label": "Hits", "path": "summary.hits", "precision": 0, "tone": "success"},
                {"id": "misses", "type": "metric", "label": "Misses", "path": "summary.misses", "precision": 0, "tone": "warning"},
            ]},
            {"id": "recent", "type": "table", "target": "result", "path": "recent_rows", "columns": [{"label": "Observed at", "path": "at"}, {"label": "Model", "path": "model"}, {"label": "Cache", "path": "cache"}]},
            {"id": "models", "type": "table", "target": "result", "path": "model_rows", "columns": [{"label": "Model", "path": "model"}, {"label": "Hit rate", "path": "hit_rate"}, {"label": "Samples", "path": "samples"}]},
            {"id": "coverage", "type": "key_value", "target": "result", "path": "coverage_rows"},
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
