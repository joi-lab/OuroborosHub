---
name: cache_efficiency_snapshot
version: 0.3.0
title: Cache Efficiency Snapshot
description: Interactive dark-glassmorphic cache observability dashboard with multi-timeframe analytics, token-weighted hit rates, cost savings, and Canvas 2D charts.
type: extension
runtime: python3
entry: plugin.py
timeout_sec: 30
permissions:
  - route
  - widget
env_from_settings: []
---

# Cache Efficiency Snapshot

A high-performance observability extension for Ouroboros that tracks prompt caching performance, token-weighted efficiency, and monetary cost savings across all models and execution lanes.

## Key Features

1. **Multi-Timeframe Analytics**:
   - `1H`: Trailing 1 hour (5-minute resolution buckets) for live inspection.
   - `6H`: Trailing 6 hours (30-minute resolution buckets).
   - `24H` (Default): Trailing 24 hours (1-hour resolution buckets).
   - `7D`: Trailing 7 days (6-hour resolution buckets).
   - `ALL`: Entire ledger history with adaptive daily/half-day resolution.

2. **Accurate Mathematical Model**:
   - **Token-Weighted Cache Read Rate**: Calculated as sum(cached_tokens) / sum(canonical_prompt_tokens) * 100%, avoiding per-call sample skew.
   - **Call-Level Hit Rate**: Percentage of requests with cached_tokens > 0.
   - **Provider-Aware Normalization**: Handles both cache-inclusive (OpenAI, OpenRouter) and fresh-only input reporters (Anthropic raw) without double-counting.
   - **Net Monetary Cost Savings ($)**: Tiered price matrix per model measuring gross cache read savings minus cache creation/write surcharges.

3. **High-Performance In-Memory Caching**:
   - Incremental stat/inode-based parser for `state/usage_attempts.jsonl` that only reads newly appended bytes, enabling sub-millisecond route response times on multi-megabyte ledgers.

4. **Zero-CDN Dark Glassmorphic Dashboard (`widget.js`)**:
   - Standalone Module Widget rendered in a sandboxed iframe.
   - Hardware-accelerated Canvas 2D spline curves with interactive hover crosshairs and tooltips.
   - Top KPI cards with dynamic trend badges and circular rate indicators.
   - Per-model efficiency breakdown table with visual gradient progress bars.
   - Collapsible data quality & diagnostics drawer.
