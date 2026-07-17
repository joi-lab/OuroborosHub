---
name: cache_efficiency_snapshot
version: 0.1.0
title: Cache Efficiency Snapshot
description: Bounded, chart-free cache-efficiency snapshot from the Ouroboros usage ledger.
type: extension
runtime: python3
entry: plugin.py
timeout_sec: 15
permissions:
  - route
  - widget
env_from_settings: []
---

# Cache Efficiency Snapshot

A read-only external extension that exposes a compact declarative widget without
Chart.js, canvas, custom JavaScript, network access, or payload state writes.
It intentionally reuses the bounded suffix-reading and terminal-attempt
projection principles of `cache_hit_rate`, but renders a fixed-cardinality
snapshot using metrics, tables, and diagnostics instead of a chart.

## What it measures

The route reads only a bounded suffix of the usage ledger. Repeated attempt rows
are reduced to the latest visible row per `attempt_id`; only terminal attempts
contribute. The hit rate is `hits / (hits + misses)` when a reliable cache signal
exists. Unknown cache evidence is excluded from the denominator rather than
counted as a miss.

## Boundaries and honesty

- The view never claims lifetime totals; it reports suffix coverage and malformed,
  duplicate, non-terminal, or unknown evidence.
- Each refresh returns one complete bounded snapshot. The host replaces its
  `result` state; this payload never emits append-only updates.
- The top-level host poll is capped at 3 ticks and has no chart or canvas surface.
- This is a chart-free replacement widget, not a repair of the separately tracked
  host declarative-chart resize-feedback defect.
