"""Comprehensive unit test suite for cache_efficiency_snapshot plugin and calculation engine."""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

# Dynamically load plugin.py from the same directory
PLUGIN_PATH = Path(__file__).parent / "plugin.py"
spec = importlib.util.spec_from_file_location("cache_efficiency_snapshot_plugin", PLUGIN_PATH)
assert spec is not None and spec.loader is not None
plugin = importlib.util.module_from_spec(spec)
sys.modules["cache_efficiency_snapshot_plugin"] = plugin
spec.loader.exec_module(plugin)

LedgerCache = plugin.LedgerCache
_clean_model_display_name = plugin._clean_model_display_name
_normalize_record = plugin._normalize_record
_resolve_pricing = plugin._resolve_pricing
calculate_analytics = plugin.calculate_analytics
register = plugin.register


@pytest.fixture
def sample_ledger_file(tmp_path: Path) -> Path:
    now = datetime.now(timezone.utc)
    p = tmp_path / "usage_attempts.jsonl"
    rows = [
        # Normal settled row with cache read
        {
            "state": "settled",
            "ts": (now - timedelta(minutes=10)).isoformat(),
            "model": "openrouter/anthropic/claude-opus-5",
            "usage": {
                "prompt_tokens": 100_000,
                "cached_tokens": 80_000,
                "cache_write_tokens": 5_000,
            },
        },
        # Fresh-only reporter (cached > prompt)
        {
            "state": "settled",
            "ts": (now - timedelta(minutes=30)).isoformat(),
            "model": "anthropic/claude-sonnet-5",
            "prompt_tokens": 20_000,
            "cached_tokens": 80_000,
            "cache_write_tokens": 0,
        },
        # Non-settled (should be ignored)
        {
            "state": "dispatched",
            "ts": (now - timedelta(minutes=5)).isoformat(),
            "model": "openai/gpt-5.6-terra",
            "prompt_tokens": 50_000,
        },
        # Zero prompt tokens (should be ignored)
        {
            "state": "settled",
            "ts": (now - timedelta(minutes=15)).isoformat(),
            "model": "gemini-3.7-flash",
            "prompt_tokens": 0,
        },
        # 2 days ago record
        {
            "state": "settled",
            "ts": (now - timedelta(days=2)).isoformat(),
            "model": "openrouter/google/gemini-3.7-flash",
            "prompt_tokens": 500_000,
            "cached_tokens": 400_000,
            "cache_write_tokens": 0,
        },
    ]
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def test_pricing_resolver() -> None:
    opus_rates, is_est = _resolve_pricing("openrouter/anthropic/claude-opus-5")
    assert opus_rates == (15.00, 1.50, 18.75)
    assert is_est is False

    gemini_rates, is_est = _resolve_pricing("gemini-3.7-flash")
    assert gemini_rates == (0.15, 0.0375, 0.15)
    assert is_est is False

    fallback_rates, is_est = _resolve_pricing("custom-unknown-model-v1")
    assert fallback_rates == (2.00, 0.50, 2.00)
    assert is_est is True


def test_model_display_name_cleaner() -> None:
    assert _clean_model_display_name("openrouter/anthropic/claude-opus-5") == "Claude Opus 5"
    assert _clean_model_display_name("google/gemini-3.7-flash") == "Gemini 3.7 Flash"
    assert _clean_model_display_name("openai::gpt-5.6-terra") == "GPT-5.6 Terra"


def test_normalization_canonical_tokens() -> None:
    # Standard inclusive row
    row1 = {
        "ts": "2026-08-23T12:00:00Z",
        "model": "claude-opus-5",
        "prompt_tokens": 100_000,
        "cached_tokens": 80_000,
        "cache_write_tokens": 0,
    }
    norm1 = _normalize_record(row1)
    assert norm1 is not None
    assert norm1["prompt_tokens"] == 100_000
    assert norm1["cached_tokens"] == 80_000
    assert norm1["uncached_tokens"] == 20_000
    assert norm1["has_cache"] is True
    assert round(norm1["gross_cost_usd"], 2) == 1.50
    assert round(norm1["net_savings_usd"], 2) == 1.08

    # Fresh-only row where cached > prompt
    row2 = {
        "ts": "2026-08-23T12:00:00Z",
        "model": "claude-sonnet-5",
        "prompt_tokens": 20_000,
        "cached_tokens": 80_000,
        "cache_write_tokens": 10_000,
    }
    norm2 = _normalize_record(row2)
    assert norm2 is not None
    assert norm2["prompt_tokens"] == 110_000
    assert norm2["cached_tokens"] == 80_000
    assert norm2["uncached_tokens"] == 30_000


def test_ledger_cache_incremental(sample_ledger_file: Path) -> None:
    cache = LedgerCache()
    records, meta = cache.update(sample_ledger_file)
    assert len(records) == 3
    assert meta["exists"] is True

    # Immediate second call hits cache
    records_cached, meta_cached = cache.update(sample_ledger_file)
    assert len(records_cached) == 3

    # Append a new row
    now = datetime.now(timezone.utc)
    with sample_ledger_file.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps({
                "state": "settled",
                "ts": now.isoformat(),
                "model": "gpt-5.6-luna",
                "prompt_tokens": 50_000,
                "cached_tokens": 25_000,
            }) + "\n"
        )

    records_updated, meta_updated = cache.update(sample_ledger_file)
    assert len(records_updated) == 4
    assert records_updated[-1]["model"] == "gpt-5.6-luna"

    # Test clear
    cache.clear()
    assert len(cache.records) == 0


def test_timeframe_analytics(sample_ledger_file: Path) -> None:
    cache = LedgerCache()
    records, meta = cache.update(sample_ledger_file)

    # 1H should include the two recent records (10m and 30m ago)
    res_1h = calculate_analytics(records, timeframe="1h", meta=meta)
    assert res_1h["status"] == "ok"
    assert res_1h["timeframe"] == "1H"
    assert res_1h["summary"]["total_calls"] == 2
    assert res_1h["summary"]["prompt_tokens"] == 200_000
    assert res_1h["summary"]["cached_tokens"] == 160_000
    assert res_1h["summary"]["cache_read_rate"] == 80.0
    assert res_1h["summary"]["call_hit_rate"] == 100.0

    # 7D or ALL should include all 3 settled records
    res_7d = calculate_analytics(records, timeframe="7D", meta=meta)
    assert res_7d["summary"]["total_calls"] == 3
    assert len(res_7d["models"]) == 3
    assert "models_omitted" in res_7d["quality"]


class MockAPI:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self.routes: Dict[str, Any] = {}
        self.ui_tabs: Dict[str, Any] = {}
        self.unloaded = False

    def get_runtime_info(self) -> Dict[str, Any]:
        return {"data_dir": str(self._data_dir)}

    def register_route(self, route_name: str, handler: Any, methods: Any = None) -> None:
        self.routes[route_name] = handler

    def register_ui_tab(self, tab_id: str, title: str, icon: str = "extension", render: Any = None) -> None:
        self.ui_tabs[tab_id] = {"title": title, "icon": icon, "render": render}

    def on_unload(self, fn: Any) -> None:
        self.unload_fn = fn


def test_plugin_registration_and_route(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = state_dir / "usage_attempts.jsonl"
    now = datetime.now(timezone.utc)
    with ledger_path.open("w", encoding="utf-8") as f:
        f.write(
            json.dumps({
                "state": "settled",
                "ts": now.isoformat(),
                "model": "claude-opus-5",
                "prompt_tokens": 100_000,
                "cached_tokens": 90_000,
            }) + "\n"
        )

    api = MockAPI(tmp_path)
    register(api)

    assert "data" in api.routes
    assert "cache_efficiency" in api.ui_tabs
    assert api.ui_tabs["cache_efficiency"]["render"]["kind"] == "module"

    class MockRequest:
        query_params = {"timeframe": "24H"}

    resp = api.routes["data"](MockRequest())
    assert resp["status"] == "ok"
    assert resp["summary"]["cache_read_rate"] == 90.0
    assert resp["summary"]["total_calls"] == 1
    assert len(resp["buckets"]) > 0
