# Token Observatory 1.1.0

Continuation of the existing `token-usage` extension, preserving its retained-history reader, Liquid Glass dashboard, filters, calendar, task scopes, numeric export and preferences. This private snapshot is a payload change for the parent to integrate; it is not a live Widgets acceptance result.

The owner scope is Ouroboros and agents launched by it only. Tokens are primary; recorded money is secondary. The dashboard makes no model calls and uses no external assets or application chat history.

## Exact host integration

The parent supplied the actual PluginAPI 2.0 contract. `plugin.py` exports `register(api)` and uses it directly, without route signature guessing or a `setup` alias:

```python
api.register_route('data', data, methods=['GET'])
api.register_route('export', export, methods=['GET'])
api.register_route('preferences', preferences, methods=['GET', 'PUT'])
api.register_ui_tab('observatory', 'Token Observatory', icon='◉', render={
    'kind': 'module', 'entry': 'widget.js', 'height': 560,
    'span': 2, 'start': 'manual',
})
api.on_unload(service.close)
```

Each relative path is registered once. The preferences handler dispatches on `Request.method`. The host adds `/api/extensions/token-usage`; every widget request uses this exact prefix. `get_runtime_info()` supplies fixed string `data_dir` and skill-owned `state_dir`. No client-controlled source paths are accepted.

Routes use real Starlette `Request` and `JSONResponse`. An absent Starlette import fails explicitly. Starlette is already a host dependency; this change adds no bundled runtime package. The reader thread starts on the first data/export query, not import or registration. Source refresh, numeric aggregation and successful data/export JSON encoding run outside the ASGI loop. Plugin unload sets the cancellation event and shuts down queued query work without joining a thread on the event loop.

`widget.js` is a classic module, with no CDN, framework or localStorage. It registers cleanup through `window.__ouroWidgetOnDispose(fn)`. Widget Stop/disposal aborts browser requests, polling and preference timers, removes listeners, and revokes download URLs. The host owns Start/Stop controls; plugin unload owns backend-worker shutdown.

## Read-only source contract

Only these fixed sources are used:

| Source | Retained projection |
| --- | --- |
| `data_dir/state/usage_attempts.jsonl` | Allowlisted numeric counters, record kind/state, recorded labels, timestamps and task links |
| Header-linked `data_dir/archive/usage_ledger/<basename>` | Verified earlier full-ledger generations |
| `data_dir/state/project_task_bindings.json` | Exact task/project identities |
| `data_dir/state/projects.json` | Project identity/name |

Only a leading `usage_baseline` header can link an archive. `archive_rel` is relative to **data_dir** and must be exactly `archive/usage_ledger/<basename>`. SHA-256, source byte size and source row count are checked before archived records enter the projection. Prior archives may themselves link older generations. Unlinked archives are never scanned or treated as authority. Traversal, cycles, symlinks, nonregular files, missing/tampered archives and incomplete archive tails produce explicit coverage issues.

Replay runs oldest to newest. The latest journal row for an exact `attempt_id` replaces previous lifecycle states even when `seq` resets between compactions. Baseline headers/groups are excluded; physical attempts are not counted again as archived or compacted rollups.

Source opens are read-only, with directory-descriptor walks and `O_NOFOLLOW`/regular-file checks. No monetary lock, repair, ledger write, tariff lookup or static price is used. No task results, chats, prompts, credentials, account IDs or session hashes are used for statistics. The metadata parser structurally skips unselected values rather than retaining source text. The sole runtime write is validated display preferences in `state_dir/token-observatory/display-preferences.json`, atomically replaced with restrictive permissions.

## Incremental replay and boundaries

Initial catch-up reads the complete current journal and its verified linked chain. Subsequent refreshes compare safe stat fingerprints; appends also check bounded beginning/end anchors, then read new lines. **Unchanged refreshes read zero journal and archive payload bytes**, verified by instrumentation around actual `read`/`readline` calls. Metadata is parsed only when its fingerprint changes. Linked-archive replacement or removal invalidates cached history rather than leaving stale totals.

Each source pass captures EOF. Lines are bounded to 1 MiB; oversized lines are skipped in chunks and disclosed, while an incomplete live tail waits for append. A cancellation event is checked during line/chunk/archive/metadata traversal; an interrupted refresh restores the previous snapshot rather than publishing partial catch-up as complete.

Coverage reports numeric-history completeness separately from attribution issues, earliest/latest retained dated records, and retained undated counts. Undated records are included only in All history and never acquire invented chart dates. Missing/corrupt source is an error; missing earlier history is partial, never an empty success.

Limits remain explicit: the numeric index is in memory, so process restart rereads retained history; archive chains stop at 128 generations with partial coverage; a rewrite preserving inode, size, mtime and checked anchors is not detectable by this incremental protocol. Append-only canonical generations between replacement/compaction events are assumed. Numeric aggregation and memory use scale with retained unique records, not a fixed recent tail.

## Meaning of the numbers

Physical metrics include only `kind='attempt'` in dispatched, settled or unresolved states. Subscription sessions are aggregate counters that can overlap physical calls. They get **separate summaries, time series and rankings** and are never added to physical totals. Reserved/released attempts and legacy totals remain inspectable as excluded record types.

Every integer metric returns `{value, exact, known, missing}`. No measured observation means `value=null`; a measured zero remains zero. Python integer arithmetic and decimal `exact` strings preserve values above JavaScript's safe-integer limit. Raw snapshot downloads preserve original JSON bytes. Chart geometry/compact labels are approximate; exact counters are available in metric titles, chart tables and record details. Counters beyond browser chart precision use the exact table instead of invalid SVG coordinates.

`reported_tokens` adds only recorded input and output, with `fully_reported_rows` and field missing counts qualifying the sum. Cache read/write are non-additive and are never added to input or each other. Retained legacy input semantics are not reconstructed from cache/input inequalities. Reasoning is **Not separately reported**.

For subscription sessions, the optional `input_token_usage` object is valid only if it has **exactly** `total_tokens`, `cache_read_tokens`, `cache_write_tokens`, with each value null or a non-boolean integer ≥ 0. Partial/extra keys, booleans, negative values, floats and strings make the whole object absent. The source persists this object independently of legacy fields, and the projection preserves that independence.

A valid normalized object supplies effective session input/cache metrics; a null field remains unknown and never falls back to its legacy counterpart. When the object is absent/invalid, legacy input/cache observations supply the effective metrics. Completion remains the recorded `completion_tokens`. Normalized and legacy observations are never summed. `subscription_summary` exposes:

- `metrics`: effective input/output/cache values used in the session chart.
- `normalized_input_usage`: valid-object known/missing counts.
- `normalized_metrics`: normalized-only total/cache observations and field known/missing counts.
- `legacy_metrics`: original legacy observations for inspection only.
- `legacy_input_sessions`: sessions whose normalized object is absent/invalid, whether legacy numeric fields are known or not.

Rows keep the original legacy numbers, valid normalized object or null, `token_exact`, `input_token_usage_exact`, and `session_token_exact`. The UI field-availability table and record details expose these distinctions.

Recorded harness/route uses only `subscription_route`. Provider and model are distinct recorded labels; neither is inferred as harness or observed executor. Missing facts remain Unknown/Unassigned. Requested versus observed executors are not separately available. Project attribution prefers direct exact binding, then exact root binding; conflicts and gaps remain visible.

Confirmed cost requires settled state, `cost_final=true` and recorded numeric cost. Estimated cost requires settled state and `cost_final=false`. Unknown finality stays unknown. Active held amounts use dispatched/unresolved reservation bounds; undispatched reservation holds are separate. No subscription price, savings, exchange rate or tariff is invented. Cost panels are explicitly labelled physical-call costs even in the session view.

## Queries, calendar and interaction

| Route | Behavior |
| --- | --- |
| GET `/data` | Filtered summary, physical `trend`/`rankings`, separate `session_trend`/`session_rankings`, facets, paginated records and coverage |
| GET `/export` | Full frozen filtered numeric selection, summary, query, snapshot ID and SHA-256; pagination does not truncate exported rows |
| GET/PUT `/preferences` | Read/save one validated display document, up to 8192 bytes |

Query fields are `period`, `start`, `end`, `model`, `route`, `project`, `task`, `scope`, `page`, `page_size`. Unsupported/duplicate fields are rejected. Filters apply before all totals, series, rankings and pagination. Facets use the whole retained projection so an empty selection remains recoverable.

Presets are 1h, 24h, 7d, 30d, All history and Calendar. Backend time windows are timezone-aware UTC `[start, end)`. The browser converts inclusive local From/Through calendar dates into local midnight at the start and midnight after the final date, then sends UTC ISO instants. DST dates can therefore span 23 or 25 hours.

A selected root's Own usage means exact `task_id`. Descendants means exact `root_task_id` excluding own rows. Own + descendants is their disjoint union; no prefix matching or inferred ancestry is used. Empty dimensions mean all, `__unknown__` means missing model/route, and `__unassigned__` means missing project/task.

Display preferences contain `period`, `model`, `route`, `project`, `task`, `scope`, `custom_start`, `custom_end`, and `view`. The view is `auto`, `physical`, or `sessions`; it is not a source selection filter. Auto shows sessions when they exist and physical calls are absent or their token sum is unknown. Explicit cohort choices persist. The record explorer is labelled **all retained record types**, including both accounting views and exclusions.

The 560 px host module scrolls its own document inside a fixed frame — nothing is measured in `vh`, so the page never sizes itself to the frame that sizes itself to the page — with one keyboard-focusable scroll region, a sticky control row, compact collapsed coverage notes, token cards and chart above cache/money, and responsive controls. The height is chosen to fit whole inside the owner's desktop window (**1016×681** PyWebView/WebKit), which a 740 px frame could not. Loading, catch-up, retry/error, empty and unknown states remain distinct. GET response fencing protects newer selections; serialized PUTs preserve the latest preferences. Unchanged refreshes preserve content nodes/focus; failed initial preference reads cannot overwrite intervening interaction.

## Files and verification

`plugin.py` owns host integration/preferences; `ingestion.py` owns numeric replay; `accounting.py` owns pure selection/arithmetic; `widget.js` owns presentation. `fixtures/numeric_snapshot.json` remains the existing public synthetic baseline. Tests cover lifecycle and multi-compaction dedup, archive integrity, zero-read unchanged refresh, cancellation, all filters/presets/calendar boundaries, tree partition totals, normalized absent/invalid/null/zero/huge cases, separate session series/rankings, exact exports, and strict host registration/preferences/lifecycle.

Run from the payload using the host's Python interpreter with Starlette available:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -B -c "import starlette"
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -B tests/oracle.py fixtures/numeric_snapshot.json
node --check widget.js
node tests/widget_contract.cjs
```

`tests/oracle.py` imports no implementation module and uses exact integers/nulls and Decimal monetary comparison (tolerance 0.000001 USD). It validates export checksum, selected-row membership, original/nested exact strings and normalized precedence. It has been maintained during this implementation; the parent's final **non-author Sol numeric verification** remains separate. A filtered export alone cannot prove completeness of canonical history.

This environment's Starlette import is unavailable and a test-only install failed DNS resolution. Tests that require real Starlette are explicitly skipped here; there is no fake successful runtime adapter. Real ASGI integration tests use Starlette directly without adding httpx. Node v24.16.0 is working here, and both JS checks execute successfully. See HANDOFF for final counts and remaining parent checks.

No new project, public publication, commit, self-approval or enablement was performed. Current host/core code, `docs/CREATING_SKILLS.md`, DESIGN, prior task artifacts and lifecycle state are outside this private payload; this continuation uses the exact parent-supplied host/source contracts. The parent owns reconciliation/application, installed review/grants/enablement and live Chromium/WebKit clicks, Start/Stop, keyboard/persistence checks, inspected screenshots and the final owner delivery.
