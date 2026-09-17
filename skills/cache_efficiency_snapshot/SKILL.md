---
name: cache_efficiency_snapshot
version: 0.5.0
title: Cache Efficiency Snapshot
description: Interactive cache observability dashboard over per-call usage events with token-weighted read rates, separate request and session counts, measurement coverage, and Canvas charts.
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
timeout_sec: 30
permissions:
  - route
  - widget
env_from_settings: []
---

# Cache Efficiency Snapshot

A read-only dashboard over Ouroboros's own usage records. Requests come from
the per-call `llm_usage` events in `logs/events.jsonl` and its rotated
`archive/events_*.jsonl` files; whole harness sessions come from the retained
`state/usage_attempts.jsonl` ledger. It reports input tokens and cache reads
with explicit measurement coverage. It makes no model calls and changes no
runtime state or task-card accounting.

Requests are read from events rather than from the ledger because ledger
compaction folds settled request rows into `usage_baseline_group` summaries
within hours, after which the main model lane of the day is invisible to any
ledger-only reader; the per-call events keep one row per physical request.

## Measurements

The cache-read rate is the sum of cache-read tokens divided by the sum of input
tokens from the **same measured records**. Both counts must be known, input
must be positive, and reads cannot exceed input. Missing cache-write data does
not invalidate an independently measured input total and cache-read count.

A usage event carries inclusive `prompt_tokens`, `cached_tokens` and
`cache_write_tokens` for one physical request; a missing count stays unknown,
and cache reads above the input total make the read unknown rather than
clipped. Session records use `input_token_usage` with nullable `total_tokens`,
`cache_read_tokens`, and `cache_write_tokens`. Null fields are never filled
from older fields. Legacy Codex session rows retain their proven inclusive
input and read-only counters when the recorded route is Codex. Other legacy
sessions remain unknown because their old counters do not establish these
semantics. There is no guess based on model names or on which count is larger.

Known-token totals exclude unknown records, whose counts remain visible. The
coverage card counts records with both valid measurements, including measured
zero-volume records. Zero input has no rate; positive input with zero cache
reads is **0%**. Idle and unavailable periods are gaps in the trend chart.
No monetary savings or estimated prices are calculated.

Requests and whole sessions remain different units. Rows group by exact
provider, model and record kind. A session aggregate may span attempts and
models; its displayed model is only the final reported model, not per-model
attribution of every token in that session.

## Timeframes and retained history

- `1H`: trailing hour in five-minute bins.
- `6H`: trailing six hours in thirty-minute bins.
- `24H` (default): trailing day in hourly bins.
- `7D`: trailing week in six-hour bins.
- `ALL`: all retained records, with adaptive hourly, six-hour, half-day or daily bins.

Intervals are half-open and have no artificial empty endpoint. The live
event log is read incrementally: incomplete trailing lines wait for their
newline, a rotated or truncated live file restarts at offset zero, and each
rotated archive is read once. Records are keyed by call identity, so a request
met in the live tail and again inside the archive it rotated into counts once.
Requests are kept for the last seven days (the archive window); session rows
follow the ledger's own retention. `ALL` therefore means all retained records,
not a replay of older history, and compacted ledger summaries are never turned
into requests. Diagnostics disclose archives read, duplicates skipped, skipped
summaries and display limits (25 model/route rows and 120 latest time bins);
headline totals still cover the selected retained window.

## Widget lifecycle

The standalone module uses the owning extension fetch bridge, with no external
CDN. Refresh is explicit and also runs every thirty seconds while mounted.
Trend and token-volume views support pointer, touch and arrow-key inspection;
coverage and unit explanations remain visible outside tooltips. Read failures
show a warning while retaining the last successfully loaded measurements.
The host disposal registrar stops polling and removes the resize listener.
The existing host/owner launch policy is unchanged.
