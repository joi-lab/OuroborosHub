"""Cache measurements preserve unknown values and compare the same input totals."""
import copy
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

PLUGIN_PATH = Path(__file__).parent / "plugin.py"
spec = importlib.util.spec_from_file_location("cache_efficiency_snapshot_plugin", PLUGIN_PATH)
assert spec is not None and spec.loader is not None
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)

LedgerCache = plugin.LedgerCache
_normalize_record = plugin._normalize_record
calculate_analytics = plugin.calculate_analytics
register = plugin.register
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


def row(**values: Any) -> Dict[str, Any]:
    result = {
        "state": "settled", "kind": "attempt", "provider": "openai",
        "model": "test-model", "ts": (NOW - timedelta(minutes=10)).isoformat(),
        "prompt_tokens": 100, "cached_tokens": 80, "cache_write_tokens": 0,
    }
    result.update(values)
    return result


def measurement(total: Any, read: Any, write: Any = None) -> Dict[str, Any]:
    return {"total_tokens": total, "cache_read_tokens": read, "cache_write_tokens": write}


def normalized(*rows: Dict[str, Any]) -> list:
    result = [_normalize_record(item) for item in rows]
    assert all(item is not None for item in result)
    return result


def test_physical_attempt_uses_canonical_inclusive_counts_without_mutation() -> None:
    source = row(prompt_tokens=270, cached_tokens=130, cache_write_tokens=None)
    original = copy.deepcopy(source)
    result = _normalize_record(source)
    assert result["prompt_tokens"] == 270
    assert result["cached_tokens"] == 130
    assert result["cache_write_tokens"] is None
    assert result["uncached_tokens"] == 140
    assert result["eligible"] is True
    assert result["measured"] is True
    assert source == original


def test_explicit_measurement_wins_over_conflicting_legacy_counts() -> None:
    result = _normalize_record(row(input_token_usage=measurement(120, 30, 10)))
    assert result["prompt_tokens"] == 120
    assert result["cached_tokens"] == 30
    assert result["cache_write_tokens"] == 10
    assert result["uncached_tokens"] == 90


@pytest.mark.parametrize("value", [None, [], "unavailable", {}, measurement(None, None)])
def test_present_unknown_measurement_never_falls_back_to_legacy(value: Any) -> None:
    result = _normalize_record(row(input_token_usage=value))
    for category in ("prompt_tokens", "cached_tokens", "cache_write_tokens", "uncached_tokens"):
        assert result[category] is None
    assert result["eligible"] is False
    assert result["measured"] is False


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), True, "80"])
def test_invalid_category_stays_unknown_without_discarding_known_total(bad: Any) -> None:
    result = _normalize_record(row(input_token_usage=measurement(100, bad)))
    assert result["prompt_tokens"] == 100
    assert result["cached_tokens"] is None
    assert result["eligible"] is False
    assert result["uncached_tokens"] is None


def test_unknown_write_does_not_hide_valid_read_measurement() -> None:
    result = _normalize_record(row(input_token_usage=measurement(100, 80)))
    assert result["cache_write_tokens"] is None
    assert result["cached_tokens"] == 80
    assert result["eligible"] is True


@pytest.mark.parametrize("explicit", [False, True])
def test_read_exceeding_total_is_unknown_not_clipped_or_guessed(explicit: bool) -> None:
    source = row(prompt_tokens=20, cached_tokens=80, cache_write_tokens=10)
    if explicit:
        source["input_token_usage"] = measurement(20, 80, 10)
    result = _normalize_record(source)
    assert result["prompt_tokens"] == 20
    assert result["cached_tokens"] is None
    assert result["uncached_tokens"] is None
    assert result["eligible"] is False


@pytest.mark.parametrize("provider", ["claude", "cursor", "other"])
def test_legacy_non_codex_sessions_do_not_reconstruct_combined_cache(provider: str) -> None:
    result = _normalize_record(row(kind="subscription_session", provider=provider,
                                  prompt_tokens=20, cached_tokens=80, cache_write_tokens=10))
    for category in ("prompt_tokens", "cached_tokens", "cache_write_tokens"):
        assert result[category] is None
    assert result["eligible"] is False


def test_legacy_codex_session_preserves_proven_total_and_read_only() -> None:
    result = _normalize_record(row(kind="subscription_session", provider="codex",
                                  subscription_route="codex",
                                  prompt_tokens=50, cached_tokens=20, cache_write_tokens=None))
    assert result["prompt_tokens"] == 50
    assert result["cached_tokens"] == 20
    assert result["cache_write_tokens"] is None
    assert result["eligible"] is True


@pytest.mark.parametrize("route,expected", [("codex", True), ("cursor", False), ("claude", False),
                                            (None, False)])
def test_legacy_codex_session_requires_recorded_codex_route(route: Any, expected: bool) -> None:
    source = row(kind="subscription_session", provider="codex", subscription_route=route)
    if route is None:
        del source["subscription_route"]  # the writer always records it; absence proves nothing
    result = _normalize_record(source)
    assert result["eligible"] is expected
    assert result["prompt_tokens"] == (100 if expected else None)
    assert result["cached_tokens"] == (80 if expected else None)


def test_missing_kind_is_a_physical_attempt_like_the_core_ledger_reader() -> None:
    source = row()
    del source["kind"]
    result = _normalize_record(source)
    assert result["kind"] == "attempt"
    assert result["prompt_tokens"] == 100
    assert result["cached_tokens"] == 80
    summary = calculate_analytics([result], now=NOW)["summary"]
    assert summary["request_count"] == 1
    assert summary["other_count"] == 0


def test_nested_canonical_attempt_usage_and_missing_read() -> None:
    source = row()
    for key in ("prompt_tokens", "cached_tokens", "cache_write_tokens"):
        del source[key]
    source["usage"] = {"prompt_tokens": 100, "cached_tokens": 0}
    result = _normalize_record(source)
    assert result["prompt_tokens"] == 100
    assert result["cached_tokens"] == 0
    assert result["eligible"] is True
    del source["usage"]["cached_tokens"]
    result = _normalize_record(source)
    assert result["cached_tokens"] is None
    assert result["eligible"] is False


def test_mixed_coverage_uses_same_eligible_rows_for_numerator_and_denominator() -> None:
    records = normalized(
        row(model="physical", prompt_tokens=100, cached_tokens=80),
        row(kind="subscription_session", provider="claude", model="claude-session",
            input_token_usage=measurement(120, 30, 10)),
        row(kind="subscription_session", provider="codex", model="codex-session",
            subscription_route="codex", prompt_tokens=50, cached_tokens=20, cache_write_tokens=None),
        row(model="unknown-read", input_token_usage=measurement(90, None)),
        row(model="unknown-total", input_token_usage=measurement(None, 7)),
        row(model="zero", input_token_usage=measurement(0, 0)),
        row(kind="subscription_session", provider="claude", model="old-session"),
    )
    original = copy.deepcopy(records)
    response = calculate_analytics(records, timeframe="1H", now=NOW)
    summary = response["summary"]
    expected = {
        "prompt_tokens": 360, "cached_tokens": 137,
        "eligible_prompt_tokens": 270, "eligible_cached_tokens": 130,
        "total_records": 7, "request_count": 4, "session_count": 3, "other_count": 0,
        "measured_records": 4, "rate_records": 3, "unknown_total_records": 2,
        "unknown_read_records": 2, "zero_volume_records": 1, "unknown_prompt_tokens": 90,
    }
    for key, value in expected.items():
        assert summary[key] == value, key
    assert summary["cache_read_rate"] == pytest.approx(48.15)
    populated = [bucket for bucket in response["buckets"] if bucket["total_records"]]
    assert len(populated) == 1
    for key, value in {"prompt": 360, "cached": 130, "uncached": 140, "unknown": 90}.items():
        assert populated[0][key] == value, key
    assert populated[0]["rate"] == summary["cache_read_rate"]
    assert records == original


def test_zero_read_zero_volume_unknown_and_idle_are_different() -> None:
    cases = [
        (measurement(100, 0), 0.0, 1, 1, 0),
        (measurement(0, 0), None, 1, 0, 1),
        (measurement(100, None), None, 0, 0, 0),
        (measurement(None, None), None, 0, 0, 0),
    ]
    for values, rate, measured, eligible, zero in cases:
        response = calculate_analytics(normalized(row(input_token_usage=values)), now=NOW)
        summary = response["summary"]
        assert summary["cache_read_rate"] == rate
        assert summary["measured_records"] == measured
        assert summary["rate_records"] == eligible
        assert summary["zero_volume_records"] == zero
        assert all(bucket["rate"] is None for bucket in response["buckets"]
                   if bucket["total_records"] == 0)
    empty = calculate_analytics([], now=NOW)["summary"]
    assert empty["prompt_tokens"] is None
    assert empty["cached_tokens"] is None
    assert empty["cache_read_rate"] is None
    assert empty["total_records"] == 0


@pytest.mark.parametrize("timeframe,hours,count", [("1H", 1, 12), ("6H", 6, 12),
                                                 ("24H", 24, 24), ("7D", 168, 28)])
def test_timeframes_have_half_open_bounds_without_an_extra_endpoint(
    timeframe: str, hours: int, count: int,
) -> None:
    start = NOW - timedelta(hours=hours)
    step = timedelta(hours=hours / count)
    stamps = [start - timedelta(microseconds=1), start, start + step,
              NOW - timedelta(microseconds=1), NOW, NOW + timedelta(minutes=1)]
    response = calculate_analytics(normalized(*(row(ts=stamp.isoformat()) for stamp in stamps)),
                                   timeframe=timeframe.lower(), now=NOW)
    assert response["timeframe"] == timeframe
    assert response["summary"]["total_records"] == 3
    buckets = response["buckets"]
    assert len(buckets) == count
    assert datetime.fromisoformat(buckets[0]["ts"]) == start
    assert datetime.fromisoformat(buckets[-1]["ts"]) == NOW - step
    assert [buckets[index]["total_records"] for index in (0, 1, count - 1)] == [1, 1, 1]
    assert sum(bucket["total_records"] for bucket in buckets) == 3


def test_all_time_has_regular_buckets_and_retains_gaps_between_observations() -> None:
    stamps = [NOW - timedelta(hours=20), NOW - timedelta(hours=1), NOW]
    response = calculate_analytics(normalized(*(row(ts=stamp.isoformat()) for stamp in stamps)),
                                   timeframe="ALL", now=NOW)
    assert response["summary"]["total_records"] == 2
    buckets = response["buckets"]
    assert len(buckets) > 2
    dates = [datetime.fromisoformat(bucket["ts"]) for bucket in buckets]
    intervals = {after - before for before, after in zip(dates, dates[1:])}
    assert len(intervals) == 1
    assert next(iter(intervals)) > timedelta(0)
    assert all(stamp < NOW for stamp in dates)
    assert any(bucket["total_records"] == 0 and bucket["rate"] is None for bucket in buckets)
    assert sum(bucket["total_records"] for bucket in buckets) == 2
    assert all(bucket["label"].startswith("2026-") for bucket in buckets)
    day = calculate_analytics(normalized(row()), timeframe="24H", now=NOW)["buckets"]
    assert all(not bucket["label"].startswith("2026") for bucket in day)


def test_all_time_labels_stay_distinct_across_a_year_boundary() -> None:
    stamps = [NOW - timedelta(days=400), NOW - timedelta(days=35)]
    response = calculate_analytics(normalized(*(row(ts=stamp.isoformat()) for stamp in stamps)),
                                   timeframe="ALL", now=NOW)
    labels = [bucket["label"] for bucket in response["buckets"]]
    assert len(labels) == len(set(labels))


def test_model_groups_use_exact_provider_model_and_kind_not_display_names() -> None:
    records = normalized(
        row(provider="openai", model="same-model", prompt_tokens=100, cached_tokens=90),
        row(provider="codex", model="same-model", prompt_tokens=100, cached_tokens=10),
        row(provider="codex", model="same-model", kind="subscription_session",
            input_token_usage=measurement(200, 100)),
        row(provider="openai", model="different-model", prompt_tokens=100, cached_tokens=0),
    )
    for record in records:
        record["display_name"] = "Identical label"
    response = calculate_analytics(records, now=NOW)
    groups = {(item["provider"], item["model"], item["kind"]): item for item in response["models"]}
    assert len(groups) == 4
    assert groups[("openai", "same-model", "attempt")]["rate"] == 90.0
    assert groups[("codex", "same-model", "attempt")]["rate"] == 10.0
    session = groups[("codex", "same-model", "subscription_session")]
    assert session["rate"] == 50.0
    assert session["session_count"] == 1
    assert session["request_count"] == 0


def test_analytics_has_no_invented_money_or_ambiguous_call_counts() -> None:
    response = calculate_analytics(normalized(row()), now=NOW)
    serialized = json.dumps(response, allow_nan=False)
    for obsolete in ("savings_usd", "gross_cost_usd", "net_cost_usd", "pricing_estimated",
                     "total_calls", "call_hit_rate"):
        assert obsolete not in serialized
    assert response["status"] == "ok"
    assert calculate_analytics([], meta={"read_error": "file_not_found"}, now=NOW)["status"] == "degraded"


def write_rows(path: Path, *rows: Dict[str, Any]) -> None:
    path.write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")


def test_ledger_keeps_unknown_and_zero_but_excludes_unsettled_and_baseline(tmp_path: Path) -> None:
    path = tmp_path / "usage_attempts.jsonl"
    # The compaction header is a settled row stamped with compaction time, not a request
    # (ouroboros/usage_compaction.py builds it beside the folded group rows).
    header = {"kind": "usage_baseline", "attempt_id": "baseline-1", "state": "settled", "seq": 1,
              "ts": (NOW - timedelta(minutes=5)).isoformat(), "compaction_epoch": 1,
              "folded_attempt_count": 3, "group_count": 1, "retained_row_count": 3}
    write_rows(path, header, row(kind="usage_baseline_group", state="settled", folded_attempt_count=3),
               row(), row(input_token_usage=measurement(0, 0)),
               row(kind="subscription_session", provider="claude"),
               row(state="dispatched"))
    cache = LedgerCache()
    records, meta = cache.update(path)
    assert len(records) == 3
    assert {item["kind"] for item in records} == {"attempt", "subscription_session"}
    assert meta["exists"] is True
    assert meta["raw_stats"]["non_settled_lines"] == 1
    assert meta["raw_stats"]["compacted_records_skipped"] == 2
    assert meta["raw_stats"]["valid_records"] == 3
    assert meta["raw_stats"]["total_lines_read"] == 6
    summary = calculate_analytics(records, timeframe="1H", now=NOW)["summary"]
    assert summary["other_count"] == 0
    assert summary["total_records"] == 3
    repeated, same_meta = cache.update(path)
    assert repeated == records
    assert same_meta["raw_stats"] == meta["raw_stats"]
    cache.clear()
    assert cache.records == []


def test_ledger_torn_line_waits_for_newline_and_is_counted_exactly_once(tmp_path: Path) -> None:
    path = tmp_path / "usage_attempts.jsonl"
    write_rows(path, row(model="first"))
    second = json.dumps(row(model="second", input_token_usage=measurement(None, None))).encode()
    split = len(second) // 2
    with path.open("ab") as stream:
        stream.write(second[:split])
    cache = LedgerCache()
    records, meta = cache.update(path)
    assert [item["model"] for item in records] == ["first"]
    assert meta["raw_stats"]["malformed_lines"] == 0
    with path.open("ab") as stream:
        stream.write(second[split:])
    records, meta = cache.update(path)
    assert len(records) == 1
    with path.open("ab") as stream:
        stream.write(b"\nnot-json\n[]\n")
    records, meta = cache.update(path)
    assert [item["model"] for item in records] == ["first", "second"]
    assert meta["raw_stats"]["malformed_lines"] == 2
    assert meta["raw_stats"]["total_lines_read"] == 4
    records, repeated_meta = cache.update(path)
    assert len(records) == 2
    assert repeated_meta["raw_stats"] == meta["raw_stats"]


def test_ledger_rotation_and_truncation_replace_cached_records(tmp_path: Path) -> None:
    path = tmp_path / "usage_attempts.jsonl"
    write_rows(path, row(model="first"), row(model="second"))
    cache = LedgerCache()
    assert len(cache.update(path)[0]) == 2
    replacement = tmp_path / "new.jsonl"
    write_rows(replacement, row(model="replacement"))
    replacement.replace(path)
    records, meta = cache.update(path)
    assert [item["model"] for item in records] == ["replacement"]
    assert meta["raw_stats"]["total_lines_read"] == 1
    write_rows(path, row(model="x"))
    records, meta = cache.update(path)
    assert [item["model"] for item in records] == ["x"]
    assert meta["raw_stats"]["total_lines_read"] == 1


class MockAPI:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self.routes: Dict[str, Any] = {}
        self.ui_tabs: Dict[str, Any] = {}

    def get_runtime_info(self) -> Dict[str, Any]:
        return {"data_dir": str(self._data_dir)}

    def register_route(self, route_name: str, handler: Any, methods: Any = None) -> None:
        self.routes[route_name] = handler

    def register_ui_tab(self, tab_id: str, title: str, icon: str = "extension", render: Any = None) -> None:
        self.ui_tabs[tab_id] = {"title": title, "icon": icon, "render": render}

    def on_unload(self, fn: Any) -> None:
        self.unload_fn = fn


def test_plugin_route_uses_runtime_ledger_and_unload_clears_cache(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    path = state_dir / "usage_attempts.jsonl"
    stamp = datetime.now(timezone.utc) - timedelta(minutes=10)
    write_rows(path, row(ts=stamp.isoformat(), input_token_usage=measurement(100, 90)))
    api = MockAPI(tmp_path)
    register(api)
    assert "data" in api.routes
    assert api.ui_tabs["cache_efficiency"]["render"]["kind"] == "module"

    class MockRequest:
        query_params = {"timeframe": "1H"}

    try:
        response = api.routes["data"](MockRequest())
        assert response["status"] == "ok"
        assert response["summary"]["cache_read_rate"] == 90.0
        assert response["summary"]["request_count"] == 1
        assert response["summary"]["session_count"] == 0
        assert len(response["buckets"]) == 12
        assert response["quality"]["ledger_path"] == str(path)
        assert api.routes["data"]({"query_params": {"timeframe": "6H"}})["timeframe"] == "6H"
    finally:
        api.unload_fn()
    assert plugin._CACHE.records == []
