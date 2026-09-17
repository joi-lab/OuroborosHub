"""High-performance, multi-timeframe cache efficiency analytics engine for Ouroboros.

Requests come from the per-call ``llm_usage`` events (the live ``logs/events.jsonl``
plus its rotated ``archive/events_*.jsonl`` files inside a bounded window), which
survive ledger compaction; whole harness sessions still come from the retained
``state/usage_attempts.jsonl`` ledger. Both readers are incremental and thread-safe,
measurements keep explicit coverage, and one HTTP endpoint feeds the module widget.
"""
from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

TERMINAL_STATE = "settled"
MAX_MODELS_RETURNED = 25
MAX_BUCKETS_RETURNED = 120
MAX_CACHE_RETAIN_DAYS = 30
BASELINE_KINDS = frozenset({"usage_baseline", "usage_baseline_group"})
USAGE_EVENT_TYPE = "llm_usage"
USAGE_ARCHIVE_GLOB = "events_*.jsonl"
USAGE_ARCHIVE_WINDOW_DAYS = 7
_EMPTY_EVENT_STATS = {
    "total_lines_read": 0,
    "malformed_lines": 0,
    "non_usage_lines": 0,
    "usage_events": 0,
    "unparsed_events": 0,
    "duplicate_events_skipped": 0,
    "archives_read": 0,
}

_EMPTY_LEDGER_STATS = {
    "total_lines_read": 0,
    "malformed_lines": 0,
    "non_settled_lines": 0,
    "valid_records": 0,
    "compacted_records_skipped": 0,
    "request_rows_skipped": 0,
}

_DATA_DIR: Optional[Path] = None


class LedgerCache:
    """Thread-safe incremental cache for whole-session rows of the usage ledger.

    Physical request rows are NOT taken from here any more: compaction folds them
    into ``usage_baseline_group`` summaries within hours, so the per-call
    ``llm_usage`` events are the request source (``UsageEventsCache``).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.file_identity: Optional[Tuple[int, int]] = None  # (inode, dev)
        self.last_mtime_ns: int = 0
        self.last_size: int = 0
        self.records: List[Dict[str, Any]] = []
        self.raw_stats: Dict[str, int] = dict(_EMPTY_LEDGER_STATS)

    def clear(self) -> None:
        with self._lock:
            self.file_identity = None
            self.last_mtime_ns = 0
            self.last_size = 0
            self.records = []
            self.raw_stats = dict(_EMPTY_LEDGER_STATS)

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
                self.raw_stats = dict(_EMPTY_LEDGER_STATS)
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

                # Compaction rows (the settled ``usage_baseline`` header and its
                # folded ``usage_baseline_group`` rows) carry compaction time,
                # not per-request timestamps; the core reader skips them too.
                if str(row.get("kind") or "") in BASELINE_KINDS:
                    self.raw_stats["compacted_records_skipped"] += 1
                    continue
                # Physical requests are counted from the per-call usage events
                # (they outlive compaction there); counting the ledger row too
                # would double the request.
                if str(row.get("kind") or "attempt") == "attempt":
                    self.raw_stats["request_rows_skipped"] += 1
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


def _usage_event_identity(row: Mapping[str, Any]) -> str:
    """One physical call = one event; a byte-identical replay (rotation seam) is the same call."""
    facts = [row.get(key) for key in ("ts", "task_id", "model", "source", "prompt_tokens",
                                       "completion_tokens", "cached_tokens", "llm_call_id",
                                       "ledger_attempt_ids")]
    return json.dumps(facts, sort_keys=True, default=str, separators=(",", ":"))


def _normalize_usage_event(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """A per-call ``llm_usage`` event: inclusive input, cache reads, cache writes."""
    stamp = _parse_timestamp(row)
    if stamp is None:
        return None
    provider = str(row.get("provider") or row.get("api_key_type") or "unknown")
    model = str(row.get("model") or "unknown")
    prompt = _token_count(row.get("prompt_tokens"))
    cached = _token_count(row.get("cached_tokens"))
    write = _token_count(row.get("cache_write_tokens"))
    if prompt is not None and cached is not None and cached > prompt:
        cached = None  # Inconsistent measurements are unknown, never clipped or guessed.
    measured = prompt is not None and cached is not None
    return {
        "ts": stamp, "kind": "attempt", "provider": provider, "model": model,
        "display_name": _clean_model_display_name(model),
        "prompt_tokens": prompt, "cached_tokens": cached, "cache_write_tokens": write,
        "measured": measured, "eligible": measured and prompt > 0,
        "uncached_tokens": prompt - cached if measured else None,
        "identity": _usage_event_identity(row),
    }


class UsageEventsCache:
    """Thread-safe incremental cache over the per-call ``llm_usage`` events.

    The live ``logs/events.jsonl`` is read incrementally (identity, offset, torn
    trailing line) like the ledger. The core rotates it by renaming it to
    ``archive/events_<stamp>.jsonl``; those files are immutable once they exist
    and each is read once, inside a bounded mtime window. Records are keyed by
    call identity, so the rotation seam (a tail already read from the live file
    and then met again inside the new archive) never counts a request twice.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.file_identity: Optional[Tuple[int, int]] = None
        self.last_size: int = 0
        self.records: List[Dict[str, Any]] = []
        self.seen: set = set()
        self.archives: Dict[str, Tuple[int, int]] = {}
        self.raw_stats: Dict[str, int] = dict(_EMPTY_EVENT_STATS)

    def clear(self) -> None:
        with self._lock:
            self.file_identity = None
            self.last_size = 0
            self.records = []
            self.seen = set()
            self.archives = {}
            self.raw_stats = dict(_EMPTY_EVENT_STATS)

    def update(self, live_path: Path, archive_dir: Path, *,
               now: Optional[datetime] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        with self._lock:
            now = now or datetime.now(timezone.utc)
            horizon = now - timedelta(days=USAGE_ARCHIVE_WINDOW_DAYS)
            meta: Dict[str, Any] = {
                "exists": live_path.is_file(), "events_path": str(live_path),
                "archive_dir": str(archive_dir), "archive_window_days": USAGE_ARCHIVE_WINDOW_DAYS,
                "read_error": None,
            }
            # Archives first: after a rotation the tail of the previous live file
            # lives only in the newest archive, and the live file starts over.
            self._ingest_archives(archive_dir, horizon, meta)
            if meta["exists"]:
                self._ingest_live(live_path, meta)
            else:
                meta["read_error"] = meta["read_error"] or "file_not_found"
            self._prune(horizon)
            meta["raw_stats"] = dict(self.raw_stats)
            return list(self.records), meta

    def _ingest_archives(self, archive_dir: Path, horizon: datetime, meta: Dict[str, Any]) -> None:
        try:
            candidates = sorted(archive_dir.glob(USAGE_ARCHIVE_GLOB)) if archive_dir.is_dir() else []
        except OSError as exc:
            meta["read_error"] = f"archive_error:{type(exc).__name__}"
            return
        for path in candidates:
            try:
                stat = path.stat()
            except OSError:
                continue
            if datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc) < horizon:
                continue
            identity = (stat.st_size, stat.st_mtime_ns)
            if self.archives.get(str(path)) == identity:
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                meta["read_error"] = f"archive_read_error:{type(exc).__name__}"
                continue
            self._ingest_bytes(data)
            self.archives[str(path)] = identity
            self.raw_stats["archives_read"] += 1

    def _ingest_live(self, live_path: Path, meta: Dict[str, Any]) -> None:
        try:
            stat = live_path.stat()
        except OSError as exc:
            meta["read_error"] = f"stat_error:{type(exc).__name__}"
            return
        current_ident = (stat.st_ino, stat.st_dev)
        meta["file_size_bytes"] = stat.st_size
        # A replaced (rotated) or truncated live file starts over at offset 0;
        # records already read stay, the identity set absorbs any replay.
        if self.file_identity != current_ident or stat.st_size < self.last_size:
            self.file_identity = current_ident
            self.last_size = 0
        if stat.st_size == self.last_size:
            return
        try:
            with live_path.open("rb") as handle:
                if self.last_size:
                    handle.seek(self.last_size)
                new_bytes = handle.read()
        except OSError as exc:
            meta["read_error"] = f"read_error:{type(exc).__name__}"
            return
        cut = new_bytes.rfind(b"\n")
        if cut < 0:
            return  # Incomplete partial line: wait for its newline.
        complete = new_bytes[: cut + 1]
        self._ingest_bytes(complete)
        self.last_size += len(complete)

    def _ingest_bytes(self, data: bytes) -> None:
        for raw in data.splitlines():
            if not raw.strip():
                continue
            self.raw_stats["total_lines_read"] += 1
            if USAGE_EVENT_TYPE.encode() not in raw:
                self.raw_stats["non_usage_lines"] += 1
                continue
            try:
                row = json.loads(raw)
            except Exception:
                self.raw_stats["malformed_lines"] += 1
                continue
            if not isinstance(row, dict) or row.get("type") != USAGE_EVENT_TYPE:
                self.raw_stats["non_usage_lines"] += 1
                continue
            record = _normalize_usage_event(row)
            if record is None:
                self.raw_stats["unparsed_events"] += 1
                continue
            if record["identity"] in self.seen:
                self.raw_stats["duplicate_events_skipped"] += 1
                continue
            self.seen.add(record["identity"])
            self.records.append(record)
            self.raw_stats["usage_events"] += 1

    def _prune(self, horizon: datetime) -> None:
        # Archives are read in name order and the live tail last, so an expired
        # record can sit anywhere in the list; scan the whole list.
        if any(r["ts"] < horizon for r in self.records):
            self.records = [r for r in self.records if r["ts"] >= horizon]
            self.seen = {r["identity"] for r in self.records}
        for key in list(self.archives):
            try:
                stale = datetime.fromtimestamp(Path(key).stat().st_mtime, tz=timezone.utc) < horizon
            except OSError:
                stale = True
            if stale:
                self.archives.pop(key, None)


_EVENTS = UsageEventsCache()


def _runtime_data_dir(api: Any) -> Optional[Path]:
    try:
        info = api.get_runtime_info()
    except Exception:
        return None
    raw = info.get("data_dir") if isinstance(info, Mapping) else None
    return Path(raw).expanduser() if isinstance(raw, str) and raw.strip() else None


def _clean_model_display_name(model_name: str) -> str:
    """Short label only; exact provider/model/kind remain the grouping identity."""
    return str(model_name or "unknown").rsplit("/", 1)[-1].rsplit("::", 1)[-1]


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


def _token_count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or int(value) != value:
        return None
    return int(value)


def _normalize_record(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    stamp = _parse_timestamp(row)
    if stamp is None:
        return None
    kind = str(row.get("kind") or "attempt")  # the core ledger reader's default
    provider = str(row.get("provider") or "unknown")
    model = str(row.get("model") or row.get("resolved_model") or "unknown")

    # The additive object is authoritative, including its unknown fields.
    if "input_token_usage" in row:
        usage = row["input_token_usage"]
        usage = usage if isinstance(usage, Mapping) else {}
        prompt = _token_count(usage.get("total_tokens"))
        cached = _token_count(usage.get("cache_read_tokens"))
        write = _token_count(usage.get("cache_write_tokens"))
    elif kind == "attempt" or (
        kind == "subscription_session" and provider == "codex"
        and row.get("subscription_route") == "codex"
    ):
        # Physical attempts are already canonical. Legacy native Codex sessions
        # report inclusive input and cache reads; Claude/Cursor combined reads
        # and creation in the old cached field, so cannot be reconstructed here.
        usage = row.get("usage") if isinstance(row.get("usage"), Mapping) else {}
        prompt = _token_count(row.get("prompt_tokens", usage.get("prompt_tokens")))
        cached = _token_count(row.get("cached_tokens", usage.get("cached_tokens")))
        write = _token_count(row.get("cache_write_tokens", usage.get("cache_write_tokens")))
    else:
        prompt = cached = write = None

    if prompt is not None and cached is not None and cached > prompt:
        cached = None  # Inconsistent measurements are unknown, never clipped or guessed.
    measured = prompt is not None and cached is not None
    return {
        "ts": stamp, "kind": kind, "provider": provider, "model": model,
        "display_name": _clean_model_display_name(model),
        "prompt_tokens": prompt, "cached_tokens": cached, "cache_write_tokens": write,
        "measured": measured, "eligible": measured and prompt > 0,
        "uncached_tokens": prompt - cached if measured else None,
    }


def _aggregate(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One arithmetic owner for headlines, route rows and time buckets."""
    totals = [r["prompt_tokens"] for r in records if r["prompt_tokens"] is not None]
    reads = [r["cached_tokens"] for r in records if r["cached_tokens"] is not None]
    eligible = [r for r in records if r["eligible"]]
    prompt = sum(r["prompt_tokens"] for r in eligible)
    cached = sum(r["cached_tokens"] for r in eligible)
    return {
        "total_records": len(records),
        "request_count": sum(r["kind"] == "attempt" for r in records),
        "session_count": sum(r["kind"] == "subscription_session" for r in records),
        "other_count": sum(r["kind"] not in ("attempt", "subscription_session") for r in records),
        "prompt_tokens": sum(totals) if totals else None,
        "cached_tokens": sum(reads) if reads else None,
        "eligible_prompt_tokens": prompt,
        "eligible_cached_tokens": cached,
        "uncached_tokens": prompt - cached,
        "unknown_prompt_tokens": sum(totals) - prompt,
        "cache_read_rate": round(cached / prompt * 100, 2) if prompt else None,
        "measured_records": sum(r["measured"] for r in records),
        "rate_records": len(eligible),
        "unknown_total_records": len(records) - len(totals),
        "unknown_read_records": len(records) - len(reads),
        "zero_volume_records": sum(r["measured"] and r["prompt_tokens"] == 0 for r in records),
    }


def _get_timeframe_bounds(tf: str, now: datetime) -> Tuple[datetime, timedelta]:
    if tf == "1H":
        return now - timedelta(hours=1), timedelta(minutes=5)
    if tf == "6H":
        return now - timedelta(hours=6), timedelta(minutes=30)
    if tf == "7D":
        return now - timedelta(days=7), timedelta(hours=6)
    return now - timedelta(hours=24), timedelta(hours=1)


def calculate_analytics(
    records: List[Dict[str, Any]],
    timeframe: str = "24H",
    meta: Optional[Dict[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    tf = str(timeframe or "24H").upper().strip()
    if tf not in ("1H", "6H", "24H", "7D", "ALL"):
        tf = "24H"
    now = now or datetime.now(timezone.utc)
    start, step = _get_timeframe_bounds(tf, now)
    if tf == "ALL":
        start = min((r["ts"] for r in records if r["ts"] < now), default=now)
        span = (now - start).total_seconds()
        step = timedelta(hours=1 if span <= 86400 else 6 if span <= 7 * 86400 else 12 if span <= 30 * 86400 else 24)
    window = [r for r in records if start <= r["ts"] < now]
    summary = _aggregate(window)

    # Half-open intervals contain each observation once, including boundaries.
    # ALL also retains empty intervals, so the chart cannot bridge unobserved time.
    count = math.ceil((now - start).total_seconds() / step.total_seconds())
    omitted = max(0, count - MAX_BUCKETS_RETURNED)
    bucket_records: Dict[int, List[Dict[str, Any]]] = {i: [] for i in range(omitted, count)}
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for record in window:
        index = int((record["ts"] - start).total_seconds() // step.total_seconds())
        if index in bucket_records:
            bucket_records[index].append(record)
        key = (record["provider"], record["model"], record["kind"])
        groups.setdefault(key, []).append(record)

    buckets = []
    for index, members in bucket_records.items():
        stamp = start + index * step
        aggregate = _aggregate(members)
        buckets.append({
            **aggregate, "ts": stamp.isoformat(),
            "label": stamp.strftime("%Y-%m-%d %H:%M" if tf == "ALL" else "%m-%d %H:%M"),
            "rate": aggregate["cache_read_rate"],
            "prompt": aggregate["prompt_tokens"],
            "cached": aggregate["eligible_cached_tokens"],
            "uncached": aggregate["uncached_tokens"],
            "unknown": aggregate["unknown_prompt_tokens"],
        })

    models = []
    for (provider, model, kind), members in groups.items():
        aggregate = _aggregate(members)
        models.append({
            **aggregate, "provider": provider, "model": model, "kind": kind,
            "display_name": _clean_model_display_name(model), "rate": aggregate["cache_read_rate"],
        })
    models.sort(key=lambda row: row["prompt_tokens"] or 0, reverse=True)
    quality = dict(meta or {})
    quality.update({
        "settled_records_total": len(records), "window_records": len(window),
        "oldest_ts": min((r["ts"] for r in window), default=None),
        "newest_ts": max((r["ts"] for r in window), default=None),
        "models_omitted": max(0, len(models) - MAX_MODELS_RETURNED),
        "buckets_omitted": omitted,
        "coverage": (
            "Requests come from the per-call usage events (live log plus rotated archives within "
            f"the last {USAGE_ARCHIVE_WINDOW_DAYS} days); whole harness sessions come from the "
            "retained ledger. Older history is not reconstructed."
        ),
    })
    for key in ("oldest_ts", "newest_ts"):
        quality[key] = quality[key].isoformat() if quality[key] else None
    return {
        "status": "degraded" if quality.get("read_error") else "ok", "timeframe": tf,
        "summary": summary, "buckets": buckets, "models": models[:MAX_MODELS_RETURNED], "quality": quality,
    }


def register(api: Any) -> None:
    global _DATA_DIR
    _DATA_DIR = _runtime_data_dir(api)

    def route_handler(request: Any = None) -> Dict[str, Any]:
        global _DATA_DIR
        data_dir = _DATA_DIR or _runtime_data_dir(api) or Path(os.path.expanduser("~/Ouroboros/data"))
        events_path = data_dir / "logs" / "events.jsonl"
        archive_dir = data_dir / "archive"
        ledger_path = data_dir / "state" / "usage_attempts.jsonl"

        timeframe = "24H"
        if request is not None:
            if hasattr(request, "query_params"):
                timeframe = request.query_params.get("timeframe", "24H")
            elif isinstance(request, Mapping) and "query_params" in request:
                timeframe = request["query_params"].get("timeframe", "24H")

        requests, events_meta = _EVENTS.update(events_path, archive_dir)
        sessions, ledger_meta = _CACHE.update(ledger_path)
        # A missing ledger loses only the session rows; the request view stays healthy.
        meta = {
            **events_meta, "ledger_path": str(ledger_path),
            "ledger_read_error": ledger_meta.get("read_error"),
            "raw_stats": {**events_meta["raw_stats"],
                          **{f"ledger_{key}": value for key, value in ledger_meta["raw_stats"].items()}},
        }
        return calculate_analytics(requests + sessions, timeframe=timeframe, meta=meta)

    api.register_route("data", route_handler, methods=("GET",))
    api.register_ui_tab(
        "cache_efficiency",
        "Cache Efficiency Snapshot",
        icon="activity",
        render={"kind": "module", "entry": "widget.js", "span": 2},
    )
    if hasattr(api, "on_unload") and callable(api.on_unload):
        try:
            api.on_unload(lambda: (_EVENTS.clear(), _CACHE.clear()))
        except Exception:
            pass
