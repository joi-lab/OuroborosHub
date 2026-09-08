"""High-performance, multi-timeframe cache efficiency analytics engine for Ouroboros.

Provides incremental ledger ingestion with thread-safe caching, token-weighted
cache rate calculations, monetary savings modeling, and a sync/async HTTP endpoint for the module widget.
"""
from __future__ import annotations

import json
import math
import os
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

LEDGER_FALLBACK = Path("state") / "usage_attempts.jsonl"
TERMINAL_STATE = "settled"
MAX_MODELS_RETURNED = 25
MAX_BUCKETS_RETURNED = 120
MAX_CACHE_RETAIN_DAYS = 30

# Default Model Pricing Map per 1M tokens (Input, Cache Read, Cache Write)
# Derived from canonical provider tariffs
MODEL_PRICING: Dict[str, Tuple[float, float, float]] = {
    # Anthropic / Claude
    "claude-opus-5": (15.00, 1.50, 18.75),
    "claude-opus-4": (15.00, 1.50, 18.75),
    "claude-sonnet-5": (3.00, 0.30, 3.75),
    "claude-sonnet-4": (3.00, 0.30, 3.75),
    "claude-3-7-sonnet": (3.00, 0.30, 3.75),
    "claude-3-5-sonnet": (3.00, 0.30, 3.75),
    "claude-3-5-haiku": (0.80, 0.08, 1.00),
    "claude-haiku-4": (0.80, 0.08, 1.00),
    # Google Gemini
    "gemini-3.7-flash": (0.15, 0.0375, 0.15),
    "gemini-3.6-flash": (0.15, 0.0375, 0.15),
    "gemini-2.5-flash": (0.15, 0.0375, 0.15),
    "gemini-2.5-pro": (1.25, 0.3125, 1.25),
    # OpenAI GPT
    "gpt-5.6-terra": (3.00, 0.75, 3.75),
    "gpt-5.6-sol": (3.00, 0.75, 3.75),
    "gpt-5.6-luna": (0.25, 0.0625, 0.30),
    "gpt-5.5": (2.50, 0.625, 3.125),
    "gpt-5": (2.50, 0.625, 3.125),
    "gpt-4.5": (10.00, 2.50, 12.50),
    "gpt-4o": (2.50, 1.25, 2.50),
    "gpt-4o-mini": (0.15, 0.075, 0.15),
    # xAI Grok
    "grok-4.6": (2.00, 0.50, 2.00),
    "grok-4.5": (2.00, 0.50, 2.00),
    "grok-4": (2.00, 0.50, 2.00),
    # DeepSeek
    "deepseek-v4": (0.27, 0.07, 0.27),
    "deepseek-v3": (0.27, 0.07, 0.27),
    "deepseek-r1": (0.55, 0.14, 0.55),
}

# Default fallback pricing: 75% savings on cache read
DEFAULT_FALLBACK_PRICING: Tuple[float, float, float] = (2.00, 0.50, 2.00)

_DATA_DIR: Optional[Path] = None


class LedgerCache:
    """Thread-safe incremental cache for the usage attempts JSONL ledger."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.file_identity: Optional[Tuple[int, int]] = None  # (inode, dev)
        self.last_mtime_ns: int = 0
        self.last_size: int = 0
        self.records: List[Dict[str, Any]] = []
        self.raw_stats: Dict[str, int] = {
            "total_lines_read": 0,
            "malformed_lines": 0,
            "non_settled_lines": 0,
            "valid_records": 0,
        }

    def clear(self) -> None:
        with self._lock:
            self.file_identity = None
            self.last_mtime_ns = 0
            self.last_size = 0
            self.records = []
            self.raw_stats = {
                "total_lines_read": 0,
                "malformed_lines": 0,
                "non_settled_lines": 0,
                "valid_records": 0,
            }

    def update(self, ledger_path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        with self._lock:
            meta: Dict[str, Any] = {
                "exists": ledger_path.is_file(),
                "ledger_path": str(ledger_path),
                "read_error": None,
                "raw_stats": dict(self.raw_stats),
            }
            if not meta["exists"]:
                meta["read_error"] = "file_not_found"
                return list(self.records), meta

            try:
                stat = ledger_path.stat()
            except OSError as exc:
                meta["read_error"] = f"stat_error:{type(exc).__name__}"
                return list(self.records), meta

            current_ident = (stat.st_ino, stat.st_dev)
            current_mtime = stat.st_mtime_ns
            current_size = stat.st_size
            meta["file_size_bytes"] = current_size

            # If file replaced or truncated, do a full reload
            if (
                self.file_identity != current_ident
                or current_size < self.last_size
                or self.file_identity is None
            ):
                self.records = []
                self.last_size = 0
                self.raw_stats = {
                    "total_lines_read": 0,
                    "malformed_lines": 0,
                    "non_settled_lines": 0,
                    "valid_records": 0,
                }
                self.file_identity = current_ident

            # If no change in size/mtime and we already have records, return cached copy
            if current_size == self.last_size and current_mtime == self.last_mtime_ns and self.records:
                meta["raw_stats"] = dict(self.raw_stats)
                return list(self.records), meta

            # Read only new bytes
            start_offset = self.last_size
            try:
                with ledger_path.open("rb") as f:
                    if start_offset > 0:
                        f.seek(start_offset)
                    new_bytes = f.read()
            except OSError as exc:
                meta["read_error"] = f"read_error:{type(exc).__name__}"
                meta["raw_stats"] = dict(self.raw_stats)
                return list(self.records), meta

            if not new_bytes:
                meta["raw_stats"] = dict(self.raw_stats)
                return list(self.records), meta

            # Find the last newline so we never parse a torn trailing line
            cut = new_bytes.rfind(b"\n")
            if cut < 0:
                # Incomplete partial line: do not advance offset yet
                meta["raw_stats"] = dict(self.raw_stats)
                return list(self.records), meta

            complete_bytes = new_bytes[: cut + 1]
            self.last_size = start_offset + len(complete_bytes)
            self.last_mtime_ns = current_mtime

            lines = complete_bytes.decode("utf-8", errors="replace").splitlines()
            for line in lines:
                if not line.strip():
                    continue
                self.raw_stats["total_lines_read"] += 1
                try:
                    row = json.loads(line)
                except Exception:
                    self.raw_stats["malformed_lines"] += 1
                    continue

                if not isinstance(row, dict):
                    self.raw_stats["malformed_lines"] += 1
                    continue

                state = str(row.get("state") or "").lower()
                if state != TERMINAL_STATE:
                    self.raw_stats["non_settled_lines"] += 1
                    continue

                parsed = _normalize_record(row)
                if parsed is not None:
                    self.records.append(parsed)
                    self.raw_stats["valid_records"] += 1

            # Bounded memory retention: prune records older than MAX_CACHE_RETAIN_DAYS
            if len(self.records) > 20000:
                cutoff_dt = datetime.now(timezone.utc) - timedelta(days=MAX_CACHE_RETAIN_DAYS)
                self.records = [r for r in self.records if r["ts"] >= cutoff_dt]

            meta["raw_stats"] = dict(self.raw_stats)
            return list(self.records), meta


_CACHE = LedgerCache()


def _runtime_data_dir(api: Any) -> Optional[Path]:
    try:
        info = api.get_runtime_info()
    except Exception:
        return None
    raw = info.get("data_dir") if isinstance(info, Mapping) else None
    return Path(raw).expanduser() if isinstance(raw, str) and raw.strip() else None


def _resolve_pricing(model_name: str) -> Tuple[Tuple[float, float, float], bool]:
    """Returns ((p_input, p_read, p_write), is_estimated_fallback)."""
    clean = model_name.lower().strip()
    for key, rates in MODEL_PRICING.items():
        if key in clean:
            return rates, False
    return DEFAULT_FALLBACK_PRICING, True


def _clean_model_display_name(model_name: str) -> str:
    raw = str(model_name or "unknown").strip()
    if "/" in raw:
        parts = raw.split("/")
        raw = parts[-1]
    if "::" in raw:
        parts = raw.split("::")
        raw = parts[-1]

    name_map = {
        "claude-opus-5": "Claude Opus 5",
        "claude-sonnet-5": "Claude Sonnet 5",
        "claude-3-7-sonnet": "Claude 3.7 Sonnet",
        "claude-3-5-sonnet": "Claude 3.5 Sonnet",
        "claude-3-5-haiku": "Claude 3.5 Haiku",
        "gemini-3.7-flash": "Gemini 3.7 Flash",
        "gemini-3.6-flash": "Gemini 3.6 Flash",
        "gemini-2.5-flash": "Gemini 2.5 Flash",
        "gemini-2.5-pro": "Gemini 2.5 Pro",
        "gpt-5.6-terra": "GPT-5.6 Terra",
        "gpt-5.6-sol": "GPT-5.6 Sol",
        "gpt-5.6-luna": "GPT-5.6 Luna",
        "gpt-5": "GPT-5",
        "gpt-4o": "GPT-4o",
        "gpt-4o-mini": "GPT-4o Mini",
        "grok-4.6": "Grok 4.6",
        "grok-4.5": "Grok 4.5",
        "deepseek-v3": "DeepSeek V3",
        "deepseek-r1": "DeepSeek R1",
    }
    for k, v in name_map.items():
        if k in raw.lower():
            return v
    return raw[:48]


def _parse_timestamp(row: Mapping[str, Any]) -> Optional[datetime]:
    for key in ("ts", "settled_at", "finished_at", "updated_at", "timestamp"):
        val = row.get(key)
        if isinstance(val, (int, float)):
            try:
                return datetime.fromtimestamp(val, tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                continue
        if isinstance(val, str) and val.strip():
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
            except ValueError:
                continue
    return None


def _normalize_record(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    stamp = _parse_timestamp(row)
    if stamp is None:
        return None

    usage = row.get("usage") if isinstance(row.get("usage"), Mapping) else {}

    def _get_num(*keys: str) -> int:
        for k in keys:
            v = row.get(k)
            if v is None and usage:
                v = usage.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0:
                return int(v)
        return 0

    raw_prompt = _get_num("prompt_tokens", "input_tokens", "tokens_prompt")
    raw_cached = _get_num("cached_tokens", "cache_read_input_tokens", "cache_read_tokens")
    raw_write = _get_num("cache_write_tokens", "cache_creation_input_tokens")

    if raw_prompt <= 0 and raw_cached <= 0:
        return None

    if raw_cached > raw_prompt:
        canonical_prompt = raw_prompt + raw_cached + raw_write
    else:
        canonical_prompt = raw_prompt

    if canonical_prompt <= 0:
        return None

    cached_tokens = min(raw_cached, canonical_prompt)
    uncached_tokens = max(0, canonical_prompt - cached_tokens)

    model_name = str(row.get("model") or row.get("resolved_model") or "unknown")
    (p_input, p_read, p_write), is_estimated = _resolve_pricing(model_name)

    gross_cost = (canonical_prompt * p_input) / 1_000_000.0
    gross_savings = (cached_tokens * (p_input - p_read)) / 1_000_000.0
    write_surcharge = (raw_write * max(0.0, p_write - p_input)) / 1_000_000.0
    net_savings = max(0.0, gross_savings - write_surcharge)
    net_cost = max(0.0, gross_cost - net_savings)

    # If row carries exact settled cost, keep reference
    recorded_cost = row.get("cost_usd")
    if isinstance(recorded_cost, (int, float)) and math.isfinite(recorded_cost) and recorded_cost >= 0:
        net_cost = float(recorded_cost)

    return {
        "ts": stamp,
        "model": model_name,
        "display_name": _clean_model_display_name(model_name),
        "prompt_tokens": canonical_prompt,
        "cached_tokens": cached_tokens,
        "uncached_tokens": uncached_tokens,
        "cache_write_tokens": raw_write,
        "has_cache": cached_tokens > 0,
        "gross_cost_usd": gross_cost,
        "net_savings_usd": net_savings,
        "net_cost_usd": net_cost,
        "pricing_estimated": is_estimated,
    }


def _get_timeframe_bounds(tf_normalized: str, now: datetime) -> Tuple[datetime, timedelta, int]:
    if tf_normalized == "1H":
        return now - timedelta(hours=1), timedelta(minutes=5), 12
    elif tf_normalized == "6H":
        return now - timedelta(hours=6), timedelta(minutes=30), 12
    elif tf_normalized == "7D":
        return now - timedelta(days=7), timedelta(hours=6), 28
    elif tf_normalized == "ALL":
        return datetime.fromtimestamp(0, tz=timezone.utc), timedelta(days=1), 30
    else:  # Default: 24H
        return now - timedelta(hours=24), timedelta(hours=1), 24


def calculate_analytics(
    records: List[Dict[str, Any]],
    timeframe: str = "24H",
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    tf_normalized = str(timeframe or "24H").upper().strip()
    now = datetime.now(timezone.utc)
    start_dt, step_delta, _ = _get_timeframe_bounds(tf_normalized, now)

    # Filter records in window
    window_records = (
        [r for r in records if r["ts"] >= start_dt]
        if tf_normalized != "ALL"
        else list(records)
    )

    # If ALL, adapt bucket step to span
    if tf_normalized == "ALL" and window_records:
        oldest_ts = min(r["ts"] for r in window_records)
        span = (now - oldest_ts).total_seconds()
        if span <= 86400:
            step_delta = timedelta(hours=1)
        elif span <= 7 * 86400:
            step_delta = timedelta(hours=6)
        elif span <= 30 * 86400:
            step_delta = timedelta(hours=12)
        else:
            step_delta = timedelta(days=1)
        start_dt = oldest_ts

    total_calls = len(window_records)
    total_prompt = sum(r["prompt_tokens"] for r in window_records)
    total_cached = sum(r["cached_tokens"] for r in window_records)
    total_uncached = sum(r["uncached_tokens"] for r in window_records)
    total_gross_cost = sum(r["gross_cost_usd"] for r in window_records)
    total_net_savings = sum(r["net_savings_usd"] for r in window_records)
    total_net_cost = sum(r["net_cost_usd"] for r in window_records)
    cache_hits = sum(1 for r in window_records if r["has_cache"])

    cache_read_rate = round((total_cached / total_prompt * 100.0), 2) if total_prompt > 0 else 0.0
    call_hit_rate = round((cache_hits / total_calls * 100.0), 2) if total_calls > 0 else 0.0

    buckets_dict: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    if tf_normalized != "ALL":
        curr = start_dt
        while curr <= now:
            b_label = (
                curr.strftime("%m-%d %H:%M")
                if tf_normalized in ("1H", "6H", "24H")
                else curr.strftime("%Y-%m-%d %H:%M")
            )
            buckets_dict[b_label] = {
                "label": b_label,
                "ts": curr.isoformat(),
                "prompt": 0,
                "cached": 0,
                "uncached": 0,
                "unknown": 0,
                "rate": 0.0,
                "savings_usd": 0.0,
                "calls": 0,
            }
            curr += step_delta

    for r in window_records:
        r_ts = r["ts"]
        if tf_normalized != "ALL":
            delta_sec = max(0.0, (r_ts - start_dt).total_seconds())
            bucket_idx = int(delta_sec // step_delta.total_seconds())
            bucket_time = start_dt + (step_delta * bucket_idx)
            b_label = (
                bucket_time.strftime("%m-%d %H:%M")
                if tf_normalized in ("1H", "6H", "24H")
                else bucket_time.strftime("%Y-%m-%d %H:%M")
            )
        else:
            b_label = (
                r_ts.strftime("%Y-%m-%d")
                if step_delta >= timedelta(days=1)
                else r_ts.strftime("%Y-%m-%d %H:00")
            )

        if b_label not in buckets_dict:
            buckets_dict[b_label] = {
                "label": b_label,
                "ts": r_ts.isoformat(),
                "prompt": 0,
                "cached": 0,
                "uncached": 0,
                "unknown": 0,
                "rate": 0.0,
                "savings_usd": 0.0,
                "calls": 0,
            }

        b = buckets_dict[b_label]
        b["prompt"] += r["prompt_tokens"]
        b["cached"] += r["cached_tokens"]
        b["uncached"] += r["uncached_tokens"]
        b["savings_usd"] += r["net_savings_usd"]
        b["calls"] += 1

    buckets_list = []
    for b in buckets_dict.values():
        p = b["prompt"]
        c = b["cached"]
        b["rate"] = round((c / p * 100.0), 2) if p > 0 else 0.0
        b["savings_usd"] = round(b["savings_usd"], 4)
        buckets_list.append(b)

    # Ensure chronological sort across all timeframes before bounding
    buckets_list.sort(key=lambda x: x["ts"])
    buckets_omitted = max(0, len(buckets_list) - MAX_BUCKETS_RETURNED)
    if len(buckets_list) > MAX_BUCKETS_RETURNED:
        buckets_list = buckets_list[-MAX_BUCKETS_RETURNED:]

    model_groups: Dict[str, Dict[str, Any]] = {}
    for r in window_records:
        m_key = r["display_name"]
        if m_key not in model_groups:
            model_groups[m_key] = {
                "model": r["model"],
                "display_name": m_key,
                "calls": 0,
                "prompt_tokens": 0,
                "cached_tokens": 0,
                "savings_usd": 0.0,
                "pricing_estimated": r.get("pricing_estimated", False),
            }
        mg = model_groups[m_key]
        mg["calls"] += 1
        mg["prompt_tokens"] += r["prompt_tokens"]
        mg["cached_tokens"] += r["cached_tokens"]
        mg["savings_usd"] += r["net_savings_usd"]

    models_list = []
    for mg in model_groups.values():
        p = mg["prompt_tokens"]
        c = mg["cached_tokens"]
        mg["rate"] = round((c / p * 100.0), 2) if p > 0 else 0.0
        mg["savings_usd"] = round(mg["savings_usd"], 2)
        models_list.append(mg)

    models_list.sort(key=lambda x: x["prompt_tokens"], reverse=True)
    has_estimated_pricing = any(m.get("pricing_estimated") for m in models_list)
    models_omitted = max(0, len(models_list) - MAX_MODELS_RETURNED)
    models_list = models_list[:MAX_MODELS_RETURNED]

    oldest_str = min((r["ts"] for r in window_records), default=now).isoformat()
    newest_str = max((r["ts"] for r in window_records), default=now).isoformat()

    has_error = bool(meta and meta.get("read_error"))
    resp_status = "degraded" if has_error else "ok"

    quality_info = {
        "settled_records_total": len(records),
        "window_records": total_calls,
        "oldest_ts": oldest_str,
        "newest_ts": newest_str,
        "cache_hits": cache_hits,
        "models_omitted": models_omitted,
        "buckets_omitted": buckets_omitted,
    }
    if meta:
        quality_info.update(meta)

    return {
        "status": resp_status,
        "timeframe": tf_normalized,
        "summary": {
            "cache_read_rate": cache_read_rate,
            "call_hit_rate": call_hit_rate,
            "total_calls": total_calls,
            "prompt_tokens": total_prompt,
            "cached_tokens": total_cached,
            "uncached_tokens": total_uncached,
            "net_savings_usd": round(total_net_savings, 2),
            "gross_cost_usd": round(total_gross_cost, 2),
            "net_cost_usd": round(total_net_cost, 2),
            "has_estimated_pricing": has_estimated_pricing,
        },
        "buckets": buckets_list,
        "models": models_list,
        "quality": quality_info,
    }


def register(api: Any) -> None:
    global _DATA_DIR
    _DATA_DIR = _runtime_data_dir(api)

    def route_handler(request: Any = None) -> Dict[str, Any]:
        global _DATA_DIR
        data_dir = _DATA_DIR or _runtime_data_dir(api) or Path(os.path.expanduser("~/Ouroboros/data"))
        ledger_path = data_dir / "state" / "usage_attempts.jsonl"

        timeframe = "24H"
        if request is not None:
            if hasattr(request, "query_params"):
                timeframe = request.query_params.get("timeframe", "24H")
            elif isinstance(request, Mapping) and "query_params" in request:
                timeframe = request["query_params"].get("timeframe", "24H")

        records, meta = _CACHE.update(ledger_path)
        return calculate_analytics(records, timeframe=timeframe, meta=meta)

    api.register_route("data", route_handler, methods=("GET",))
    api.register_ui_tab(
        "cache_efficiency",
        "Cache Efficiency Snapshot",
        icon="activity",
        render={"kind": "module", "entry": "widget.js", "span": 2},
    )
    if hasattr(api, "on_unload") and callable(api.on_unload):
        try:
            api.on_unload(lambda: _CACHE.clear())
        except Exception:
            pass
