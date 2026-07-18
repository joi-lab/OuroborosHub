---
name: cache_efficiency_snapshot
version: 0.2.0
title: Cache Efficiency Snapshot
description: Honest bounded-suffix cache-read analysis with token-weighted charts.
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

A read-only external extension that turns a **bounded suffix** of Ouroboros's
usage ledger into an explicit, reproducible cache-read analysis. It has no
network access, custom JavaScript, payload state writes, or lifetime-history
claim.

## What the charts mean

The headline **cache-read rate** is token-weighted, never an average of
per-request percentages:

`sum(cached_tokens) / sum(prompt_tokens)`

It includes only deduplicated, terminal `settled` records with a UTC timestamp,
a positive prompt-token denominator, and a known cache-token measurement where
`0 <= cached_tokens <= prompt_tokens`.

Two bounded host-rendered charts show the same canonical bucket data:

1. **Cache-read rate by observed UTC time bucket** — the token-weighted percentage
   alone, using the displayed prompt-token denominator.
2. **Prompt-token composition by the same buckets** — cached, uncached, and
   explicitly **unknown cache measurement** tokens. This is the separate volume
   view; unknown data is never silently painted as a miss.

The card always shows the observed suffix window, valid sample count,
denominator, and separate quality counters. Summary token values use **M** for
readability; the observed-window table retains their exact integer values. It
does not claim a lifetime or system-wide cache rate.

## Bounds and integrity

- Reads at most **192 KiB** and **2,000 JSONL lines** from the ledger tail.
- Renders at most **8 chronological buckets**. Their UTC resolution adapts to the
  observed suffix: 15 minutes (≤2 h), hour (≤2 d), otherwise day; it also renders
  **5 recent rows**, **4 model rows**, and **7 diagnostics**.
- Repeated `attempt_id` rows are reduced to their latest occurrence in the
  observed suffix.
- Malformed, duplicate, non-terminal, missing-time, zero-denominator, unknown,
  invalid-token, and applied-line-cap observations stay visible as separate
  data-quality facts.
- Charts use the host declarative Chart.js contract with an accessible semantic
  data table fallback. The host owns canvas sizing and bounded lifecycle.
