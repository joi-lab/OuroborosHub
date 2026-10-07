---
name: claudexor_quotas
description: "Claudexor quota widget and read-only quota_summary tool: one bar per account and limit (current, dated last-known, or unknown — never zero), reserve in account-windows with coverage, restrictions and reported resets, and a conditional recent-pace estimate for the accounts whose pace qualifies, from a bounded local history of passive readings."
version: 0.7.0
type: extension
runtime: python3
entry: plugin.py
plugin_api: "2.0"
permissions: [net, route, widget, tool, supervised_task]
env_from_settings: []
when_to_use: "The owner wants quota windows, resets and remaining reserve of the authorized Claudexor accounts; or, before choosing executors, the model wants measured reserve by family and limit. Advisory only: never a dispatch guarantee or a pin change."
model_experience:
  what_model_sees: "One tool, quota_summary, with a fixed schema. Only when called does compact JSON (account-windows per family and limit, current and dated last-known apart, coverage, restrictions, resets, pace, and a one-line headline per limit) arrive as a tool result; nothing is auto-inserted."
  token_effect: "Fixed schema and description while enabled; each voluntary call adds about 1-8 KB of JSON, less per family. No automatic call or cache benefit is promised."
timeout_sec: 180
ui_tab:
  tab_id: quotas
  title: Claudexor Quotas
  icon: "◔"
  span: 2
  render:
    kind: module
    entry: widget.js
    appearance: host
    start: auto
---

# Claudexor Quotas (v0.7.0)

A projection of the host's own account surface, plus a small planner on top of
it: how much measured quota is left per agent family and limit, when it is
reported to reset, and what happens if the recent pace continues. Cached
projection reads remain read-only. The owner's explicit Refresh button invokes
the host's dedicated foreground quota action; the skill reads no daemon token
and owns no quota freshness, routing, retry, pacing, or vendor policy.

The skill keeps these local files inside the state directory the host hands
it (`api.get_state_dir()`):

- `prefs.json` — the reader's display choices (how much of a row to unfold,
  which windows a row shows per agent family, which kinds of account fold
  away). The widget cannot keep them itself: module widgets run in an
  opaque-origin sandbox where every browser store throws, so a preference kept
  there is silently forgotten. The route stores those three values and
  nothing else. It reads and writes the file off the event loop, one save at
  a time per load of the skill, each through a temporary file of its own
  moved over `prefs.json`. Of two saves that cross, the reader's later choice
  is kept: the widget numbers its saves per load of the frame (`frame`, `seq`,
  read and never stored), the route does not write a save that arrives after
  a newer one from the same frame, and the widget draws only its newest
  save's answer. A save without that number keeps the later arrival.
- `quota_history.sqlite3` — a bounded history of passive quota readings,
  written only by the skill's one supervised collector (see below).
- `quota_collector.lock` — an empty lease file, never removed while the skill
  runs. Its OS lock serializes collector cycles across unload/reload; it holds
  no account or quota data. SQLite may also keep its standard WAL/SHM files.

## What changed in 0.7.0

- **Nothing vanishes, nothing becomes 0.** An account whose reading is not
  current keeps its last value, dated, as a hatched bar (from a stale reading
  in the answer, from the last answer that read the quota facet, or from the
  local history) — while its reported reset is ahead, or, with none reported,
  for at most its window's length. Anything else is an outlined "?" that is
  not counted. `measured`, the tool's `remaining_windows` and the widget's
  row figure stay current readings only — with none, the figure is "—",
  never 0; the last-known part stands under it as its own labelled, dated
  line ("Last known 0.40 · 21 min"), never added in.
- **A limit an answer does not name keeps its record.** For each account of
  the roster (read now or kept), every recorded limit of exactly that
  pseudonymous subject that no reading of the answer names — the quota facet
  unanswered, or answered without it — is shown as its dated `history`
  last-known value; a scoped one stays scoped (`model_scope:
  names_unknown`), and its chart reads that limit's own record.
- **A facet that did not answer is answered from the last read that did**,
  per facet and dated, never as current: a failed status read keeps the last
  roster and readings on screen with one banner and a Retry instead of an
  empty card. A roster read now always wins, so a deleted account is never
  brought back, and a confirmed empty roster is not replaced.
- **Rows as approved in the v4 design**: the family's marks with their active
  account counts, one row per limit with one proportional bar per account,
  fullest first, a bar selects its account; a compact line per limit for the
  selected account; the chart on request.
- **The estimate covers the accounts whose pace qualifies**, named by count,
  not only when every account has one; it stops at the first reported reset
  among them, and a refill at reported resets is a separate scenario drawn
  only on request.
- **Every request ends**: the widget bounds its reads (60 s) and Refresh
  (210 s) above the skill's own route bounds, never re-sends a Refresh on its
  own, and keeps the screen it had when an answer cannot be drawn. A read that
  fails keeps the newest screen drawn — a degraded one with its dated
  last-known values included, never an older whole one that would look
  fresher — re-read at that moment: nothing on it is current any more (see
  "Every request ends"). An answer with nothing to draw and no reason is not
  a screen. A Refresh whose answer the skill could not read is reported as
  `outcome_unknown`, never as failed, and never sent again on its own.

## Host appearance

This module opts into the host-resolved Light/Dark theme through the optional
`OuroborosWidget.onTheme` bridge. Older hosts keep the widget’s dark palette.
Theme changes update presentation in place and disposal unsubscribes the
listener. Both themes use the same neutral surfaces, 12/14/16px type scale,
32px basic controls and visible keyboard focus. Family buttons have visible
names, and Refresh is explicitly labelled. There are no glass, canvas pools,
plan gradients or tiny-text exceptions.

Host disposal stops polling and document listeners immediately, while an
already queued preference save can finish within the existing bounded flush.
A terminal disposal cannot restart through `pageshow`; ordinary pagehide /
back-forward-cache restoration can. Late replies from an old generation cannot
release a newer request’s in-flight lock, and deferred chart requests cannot
start after stop. Pool names and family ids are treated as literal keys even
when they match JavaScript prototype names (for example `constructor`). These
are regression-tested repairs; they do not establish the cause of a previously
reported native desktop `Script error` whose stack was unavailable.

The card declares `start: auto`: it is a cheap dashboard that starts
when the Widgets page is shown and stops when the owner leaves it (the owner's
per-card mode still wins). Its icon is one glyph, `◔`.

## Reserve overview (quota_summary.py)

One module computes every reserve number. The widget's overview and chart and
the model's `quota_summary` tool call the same functions on the same passive
status projection and the same history view; the widget only formats.

**Unit — account-windows.** One account's remaining share of one limit counts
1, whatever its plan: accounts with 40% and 70% left are 1.10 account-windows,
shown with the average (55%) and the number of measured accounts. It is an
arithmetic index of measured quota, not tokens, hours or work. When the
measured accounts carry different plan labels the group is marked "mixed
plans" and both the overview and detailed tool output show per-plan account
counts and account-windows; no weights are invented.

**What is one group.** A group is one agent family (harness) and one limit:
the limit's meaning (its constraint id, with only the engine's own
`<harness>:` namespace prefix dropped, so the app-server's `codex:primary` and
the rollout log's `primary` are one limit, while `base_model_inference:primary`
stays its own pool), its duration from `window_seconds` (never from the words
primary/secondary), and its model scope (`applies_to_models`, told apart by
the whole list with every name in full, serialized so that a newline inside a
name is never read as a second name — every scope without one keeps the key
its history is stored under; a list longer than 24 names is shown in part,
saying how many names it leaves out). A 5-hour limit,
a weekly limit and a model-scoped limit are separate groups and are never
added together: they bound the same work at the same time. A constraint with
neither a ratio nor a window (a cooldown, a reset-credit counter) is not a
quota window; a cooldown becomes a restriction instead.

**Who is counted, once.** Accounts are matched by the engine's exact subject
`(harness, subject_id)`. On a unified engine (`unified_accounts: true`) the
reserved `<harness>-default` profile may inherit legacy null-subject readings,
the host's own rule, and only while it has no fresh reading of its own;
otherwise a null-subject reading is left out and counted as unattributed, as
is a reading for a subject the account list does not contain (and, with the
account list unread, a null-subject reading on a unified engine). The account
view takes each account's readings from the same rule (`attribute`), so the
default row shows the legacy reading exactly when the overview counts it for
that row. A Refresh answer carries no account list: its per-account update
matches readings by exact subject until the next status read. When one account
has fresh readings from several sources, they are one limit only if their
reported resets agree (within 120 s); the newest observation is used. Sources
that observed the same moment (within one second) must also agree on the
value, within one whole percent (the larger use is then counted); no source
outranks another. If sources disagree about the reset or about a same-moment
value, the account is shown as "sources disagree" and left out of the total
rather than guessed. Accounts are never merged because they share an e-mail
address; the number of measured accounts sharing a sign-in is disclosed
instead, because two profiles of one vendor account may draw on one pool.

**Only fresh numbers are summed.** A reading contributes only when the host
marks it fresh, it has an observation time, and its `used_ratio` is a JSON
number in [0, 1]. A stale-only, missing, non-numeric, NaN, infinite or
out-of-range ratio is never a zero and never clamped: the account is shown as
stale or unreadable in the coverage line. A fresh reading whose reported reset
has already passed describes an ended cycle and is shown as "reset since
reading", not counted — the same rule for every reading, whatever its value.
Accounts of the family that report no reading of a limit are named as "N other
accounts: no reading, limit may not apply": not counted, not zero, and not
assumed to have that limit.

**Last known: dated, labelled, never current.** An account of a limit with no
current reading keeps its newest usable value as a dated last-known fact
(`LastKnown`): from a stale reading in this answer, from the quota facet of the
last answer that read it (when this one did not), or — for an account of the
current roster whose limit this answer carries no reading of at all — from the
newest run the local history keeps for exactly that pseudonymous subject and
series (never by e-mail). The source's own observation time is kept, never
when it was fetched; stale sources that disagree give none — said as "its
sources disagree", and kept through every fallback: the history stands in
for this answer's stale evidence only with a newer reading, never an older
run in place of evidence refused — and the newest
kept run of every source of a history series goes through the same policy
(two sources that disagree there give none either, never the newest by
chance). It is *carried* (drawn hatched and dated, and said under the
widget's row figure as its own line "Last known … · age" — never in the
figure, `measured`, `shares` or the tool's `remaining_windows`) while
its reported reset is still ahead or, with no reset reported, for at most its
window's length (24 hours without a window). A carried value is dated
evidence, not a bound on what is left now and not available reserve: use may
have grown since, and a restore or an unreported reset may have lowered it. Past that, or once its reset has passed, it stays on record for the
account ("last read …") and the account is unknown. Each group adds `bars`
(one per account the limit is known to apply to — `current`, `last_known` with
`origin` and `age_seconds`, or `unknown` with `why` — fullest first by share
alone, so an account flapping between a current and a last-known reading of
one value keeps its place; unknown ones last), `slots` (their count: the scale
of the row and of the chart), `last_known` (accounts, windows, oldest and newest
observation, origins), `unknown`, `applicability_unknown` (roster accounts with
no reading of the limit here or in the history) and `with_last_known` (current
plus carried windows — for the tool's `remaining_windows_with_last_known`; the
widget does not show this sum, and it is never available now). `measured`,
`coverage` and `shares` keep their meaning. Account keys travel on the bars
only in the widget route's answer, beside the account list it already sends;
the tool and every other answer carry no account identity.

**A facet that did not answer.** The process keeps, in memory only, each facet
(catalog, accounts, quota) of the last status read that answered it. When a
later read fails a facet — or fails altogether — that facet is answered from
the kept one, and the answer says which and from when (`cached`, per facet),
while `facets`/`reads` keep saying what was actually read. Kept quota readings
are all stale, so they can only be last known. A kept account list keeps the
roster, never the accounts' state: every account is then flagged
`account_state_unknown` (and the widget says once that the account list was not
read now, rather than painting every bar amber). A facet read now — an empty
roster included — is never replaced: a deleted account is not brought back,
and fresh disable/sign-out/auth state still holds over kept quota numbers.
With no earlier read in this process nothing is invented: the roster is
`unknown` and only what the answer carries is shown. A limit the history
records for a roster subject (read now or kept) that no reading of the answer
names — a cold start with the quota facet not answered (`not_read` or
`failed`, and none kept), or a quota answer that omits it (an answered facet
with no reading of a limit is not proof its last reading was false) — comes
from the local history of exactly those roster subjects: each recorded limit
of theirs with a usable last-known value is shown as that dated `history`
value (sources resolved as above), never as current, so `measured` stays 0
for it and the headline says "no current reading". An account not in the
roster now is never asked about, and an empty or unknown roster restores
nothing. Such a limit's coverage counts the whole family as `other` and takes
the restored accounts off once (`applicability_unknown`). History alone does
not record a scoped limit's model names, only its scope: such a limit is
labelled by its recorded meaning and stays scoped — `model_scope:
"names_unknown"` (else `named` or `none`), the tool's `model_scope` note and
headline say "model-scoped, model names unavailable from history", and the
widget names it "· model-scoped" — never the family's shared limit. When the
chart opens on such a limit (asked for, or the default), the route reads once
more, bounded the same way, with that limit as the charted one, so its past
is its own record. A status read that fails
also stops a chart or family switch (`reuse=1`) from answering from an older
successful read, so the screen never jumps back to "fine" over a newer failure.

**The account view reads the same way.** An account's current windows are the
readings the reserve counts, read and resolved by the same code
(`reading_of`, `resolve_member`): one window per limit — from the source the
same-moment policy chooses, not one per source — and only from a fresh,
numeric reading observed at a known, not future, time of a cycle whose
reported reset is still ahead. A fresh reading the reserve refuses (a ratio
out of range or not a number, no or a future observation time, a reported
reset that has passed, sources that disagree) is never a current bar or a
"Limit reached" verdict: it stays in view as "Not current — <reason>", beside
the stale readings, as the known fact it is. A window reported with no ratio
at all stays a window without a bar. Percentages are carried unrounded
(`used_pct`) with their words (`used_text`): whole percents as reported, a
finer reading to one decimal, and never "100" for a window that is not at its
limit nor "0" for one that has been used ("<100", ">0"). Whether a window is
at its limit (`at_limit`) is decided on the unrounded share, exactly as the
overview's `at_limit` is — 99.6% used is not the limit — and every label, bar
colour, dot, chip, tile, list row and verdict takes it from there.

**A cooldown is not the limit.** Cooldowns are read apart from the numbers,
by the one rule the overview's "cooling" restriction uses (`cooldowns_of`):
a `cooldown_until` that is ahead or unreadable, from any source of the account
and from fresh or stale readings alike, and a fresh reading's availability
state `cooldown` while its `resets_at` is ahead, unreadable or not reported;
one whose time has passed — either kind — is history. So a cooldown
reported by a source whose number is not the one drawn, or by a stale
reading, is not lost. The account view carries each as a fact of its own
(`cooldowns`: the whole account or the models it holds, until when — or that
the end is unreadable, not reported, or already past — and whether a stale
reading reported it). An account held by one reads "Cooling down" until the
last one ends (`cooling_until`, empty when an end is unknown) — never "Limit
reached", and its end is never shown as a reset: nothing refills then. A
window at its limit still reads "Limit reached" with that window's own reset,
and a cooldown beside it stays a separate line. A model's cooldown marks the
model, not the account. The engine's own `availability` word is passed on as
reported.

**A model's limit reported out is not a cooldown.** The engine's
`availability.model_scoped_exhaustions` (`{constraint_id, applies_to_models,
resets_at}`) is read apart from cooldowns (`exhaustions_of`), from fresh and
stale readings alike, and carried in the account view as facts of their own
(`model_exhaustions`: the limit, the whole model scope by `scope_key`, the
first 24 names and how many more, the reported reset, which reading reported
it, and `live`). One holds its models only while its reported reset parses and
is still ahead, as the engine reported it. One with no reset, an
unreadable one, or one whose reset has passed is disclosed as reported
(`reset_note`: `not_reported`, `unreadable`, `passed`) and never made a hold;
no reset is invented for it. It never holds the whole account or changes the
account's state, it is never a share, and nothing is forecast from it: the
limit's own number, where one was read, stays that limit's reading.

**Reserve is not availability.** Measured accounts that are disabled, signed
out, failing verification, cooling down (a live or unreadable
`cooldown_until`, fresh or stale evidence, since the engine may still honour
it), or blocked by another spent shared limit are counted in the total and
listed as restrictions with their account-windows; beside them the rest is
shown as unrestricted. A shared limit blocks as soon as its current measured reading is
spent, whether or not its reset time was reported (unknown timing is not the
absence of a restriction); a reading whose reported reset has passed, a stale
or an unreadable one blocks nothing. A live reported model exhaustion
restricts (`model_exhausted`) only the account's share of the limit with
exactly that model scope, and only while the share counted there is not
already at the limit (which says it itself, and is not counted twice); a
disclosed one, or one naming no model, restricts nothing. None of this is a
dispatch guarantee: Claudexor decides routing.

**Lowest left (`tightest`).** In each family, the limit with the smallest share
left on average among those with a measured account (more accounts at the
limit, then the summary's order, break a tie) is marked `tightest` — in the
overview (worded "lowest left"), in the tool (`tightest_in_family`), and as the
chart's default. It is a ranking of averages, not a verdict on what can run: a
nearly full 5-hour limit does not stand for a family whose weekly or
model-scoped limit is running out, nothing is added across limits to find it,
and a model-scoped limit (such as "Weekly · Fable") binds only the models it
names, never the family's other models. Each group also lists `shares`: the
measured accounts' share left, fullest first, with whether each is at the
limit (on the unrounded share) and whether a restriction touches it — no
identity — which the widget draws one bar per account; they add up to the
figure. Each row is sorted on its own, so a column of bars does
not follow one account from row to row.

**Reported resets and even pace.** The next reported reset is shown with the
number of accounts resetting then. "Even use to each account's reported reset"
is the sum over accounts with a reported future reset of remaining share /
hours to that reset. Every reported reset is taken as reported, whatever the
usage: nothing is inferred about whether a window "has started", since the
status API does not say so. An account whose reading carries no reset is
counted in the reserve and named as "with no reported reset"; no refill is
drawn for it.

## History and the collector (quota_history.py)

A single server-owned supervised task (`api.register_supervised_task`,
permission `supervised_task`) reads the same passive `GET
/api/claudexor/status` every 120 seconds, with the 25-second read bound, on a
worker in the event loop's shared, bounded executor. State-directory and port
lookup, the network read, normalization, SQLite writes, pruning, vacuum and
connection closure all run there, outside the host's event-bus loop. It
never calls the refresh endpoint and never asks a provider for a new reading.
Two minutes is deliberate: each status read makes the daemon probe the agent
CLIs for seconds, and the engine refreshes quota on its own slower schedule. A
status read a widget route or the tool made within the last minute is recorded
instead of issuing another; the tool keeps its own reads exactly as the route
does (the facets they answered are what a later failed read is answered from). Any Refresh request drops that remembered read, whatever its
outcome, and a read still in the air when it returns is not remembered: the
next chart or family switch, tool call or sweep reads the status anew rather
than reuse a read from before the Refresh. No Refresh is ever issued for it.

- The host starts the task only when the server publishes the registration;
  worker processes merely record it. Disable, unload, delete or shutdown
  cancels it, and `on_unload` sets a stop flag as a second guard. Widget
  frames never start a collector, however many are open.
- A run that ends on an error stops only itself, so the host's `on_failure`
  policy (at most three restarts, 30 s apart) starts a new run. Unload and
  cancellation stop the registration itself: a run started after either
  ends before any I/O.
- Each coroutine submits at most one blocking cycle at a time. An OS file
  lock on `quota_collector.lock`, tried without waiting on that worker,
  covers the whole cycle: read, write and connection closure. It is `flock`
  on macOS/Linux and a lock of the file's first byte through
  `msvcrt.locking` (`LK_NBLCK`, then `LK_UNLCK`) on Windows. Both belong to
  the open file, not the process, so an old and a new registration in one
  process exclude each other, as do two processes sharing the state
  directory, and the OS drops the lock if its holder dies. A refused lock
  (`EWOULDBLOCK`; on Windows `EACCES`/`EDEADLOCK`) means another worker holds
  it: the new registration skips that cycle while the previous one settles.
  Any other lock error skips the sweep with a warning. Unload/re-enable
  creates no private executor, thread or unbounded writer pool. Where
  neither lock exists the collector fails closed: every cycle is skipped and
  logged, and nothing is read or written. The Windows branch is verified
  only against a model of the documented `msvcrt.locking` behaviour, not on
  Windows itself. A network file system that emulates `flock` with
  process-owned locks would not separate two generations in one process;
  the state directory is expected on a local disk.
- **Stop request versus settled:** `on_unload` sets a stop flag through a
  memory-only lock and returns immediately, without waiting for disk. Host
  task cancellation requests the same stop. A queued cycle is cancelled; a
  running read may return, but any result returned after stop is discarded.
  The port is looked up (runtime info, a file read) before the status
  request, which is then admitted right before it starts: a stop requested
  before admission prevents the request. Pending transactions check stop
  while processing and before commit admission, and roll back if stopped.
  Admission uses the same short lock; no lock is held during network or disk
  I/O. A status request, commit (or initialization/maintenance operation)
  admitted before the stop request may start or finish afterwards; such a
  request's answer is discarded. An already committed atomic write cannot be
  cancelled.
- The coroutine drains an active worker asynchronously, including rollback or
  admitted commit, connection closure and lease release, then sets its
  internal `control.settled` event. Repeated cancellation does not detach a
  writer. **The public host unload API does not await or report this internal
  acknowledgement:** callback return means stop requested, not persistence
  settled. A stalled filesystem can delay settlement, but never makes this
  callback wait on disk. Actual installed Stop/Panic behavior remains a host
  integration check.
- Each sweep writes, in one SQLite transaction, the fresh numeric readings it
  saw (every source), plus one sweep row (time, ok, short reason). A reading
  the host keeps reporting with the same observation time **and unchanged
  content** is not a new point: while the watch of that source is unbroken it
  only extends `last_seen`. A changed value, reset or plan evidence starts a
  new run even if its timestamp is unchanged; that correction is marked and
  cuts pace, rather than treating the correction as new spend. A break in the
  watch of a source also starts a new run — sightings more than 7 minutes apart
  (three missed sweeps plus one status timeout), or any sweep since its last
  sighting: a failed one, or one that did not see this source fresh and
  numeric (stale, missing, unreadable) while the rest of that sweep was
  healthy — even when the host reports the very same cached reading
  afterwards. The run after a break is marked, so neither an outage nor one
  source's short hole is bridged by a returning cached timestamp, and healthy
  neighbouring sources keep their runs.
- Readers apply the same rule from the sweep rows themselves: a completed
  sweep between two sightings of a source (or after its last sighting) that
  did not see it is a hole in that source's line, in the chart, in pace and in
  the reading just made by a route before the next sweep. History written by
  0.6.0 kept those sweep rows, so its recorded holes are read as holes too;
  where 0.6.0 merged a hole into one run (the same cached reading returning),
  nothing is left to read and nothing is reconstructed.
- Stored per run: a salted pseudonymous subject id (random salt kept in the
  store; not the profile id, name or e-mail), the limit key, source name, plan
  evidence (the reading's own plan label and the account list's plan, both as
  reported), raw ratio, reported reset, first/last source observation times,
  first/last collector sightings, break and correction marks. No raw response,
  detail text, identity or credential is written.
- Bounds, exactly: every 30 sweeps, runs last sighted more than 14 days ago
  and older sweeps are deleted, and a run still sighted is trimmed to the last
  14 days (its first sighting, and its first observation when a later one
  repeats the value, move up to the cutoff; a single observation keeps its own
  time); then at most 200 000 runs and 20 000 sweeps are kept. After every
  sweep a file (with its write-ahead log) over 64 MiB drops its oldest quarter
  of runs and returns the freed pages: a soft ceiling, checked after the
  sweep has written, and a file still above it shrinks again next sweep. A
  cap that removed data is recorded (`capped_before`) and the widget says the
  history is shorter than 14 days. Reads are capped at 50 000 rows per call
  (and at the 20 000 sweep rows a continuity check can need). The widget says
  where the record begins: the first sweep still kept.
- A history file the skill cannot read (not a SQLite database, an unknown
  shape or version) fails closed: it is not moved, deleted or rebuilt,
  nothing more is written, the collector logs it once, and the overview and
  tool report the history as unavailable (the reserve itself is unaffected).
  Before each read and write only the schema and salt are checked; no full
  integrity scan is run. A damaged page is therefore found only when a query
  reaches it: that read reports the history unavailable and that sweep is
  rolled back (the file is still not moved, deleted or rebuilt), while a sweep
  that never reaches it may still be written. A file that is not a SQLite database is never even opened,
  because SQLite would delete the write-ahead log beside it. Removing
  `quota_history.sqlite3` from the skill's state directory starts a new
  history. Schema version 2 is intentionally distinct from uninstalled R1/R2;
  there is no migration. Every read and write validates tables, columns,
  indexes and salt, including empty requests and failed/empty sweeps. A locked
  or unwritable file only skips that sweep. Readers use SQLite `mode=ro` and
  `query_only`, never initialize or repair the database, and see committed WAL
  content. A checkpointed file receives an immutable **schema-only** preflight
  to reject unsupported files without creating sidecars; actual history reads
  never use immutable mode. SQLite may maintain SHM/read-lock bookkeeping on
  ordinary read-only WAL connections. Database and existing WAL preservation
  are tested; a blanket no-filesystem-write claim is not made.
- While Ouroboros is closed, offline, or the daemon or quota facet does not
  answer, nothing is recorded: that is a gap, never idle time and never a
  zero. Pace warms up until at least 15 minutes of comparable watched
  observations exist within the trailing hour; an hour of history is not
  required. No history is generated.

## Recent pace and the chart

**Recent pace** is, per account, the endpoint slope over the trailing
comparable stretch of one source inside the last hour: (last ratio − first
ratio) / elapsed time between the two source observations, with that span
shown. Comparable means the same source, the same reported reset (two readings
that both report none are told apart only by a drop), a non-decreasing ratio,
the same plan evidence and no break in the watch; a reset, a ratio drop
(including a manual limit restore), a plan change or a break cuts the stretch,
and a cut is never read as negative or idle use. Plan evidence is the quota
reading's own plan label and the account list's plan together, as reported:
the account list may report a change the reading's label does not (absent, or
still the old one), so a change in either — one appearing, disappearing or
starting to disagree, or the account list going unread — cuts, and neither
overrides the other. The plan shown remains the account list's, else the
reading's label. Only watched time counts: an observation
made before the collector's current unbroken run of good sweeps began (or
before the individual source first appeared in its current uninterrupted
watch segment, including its first-ever appearance and its return after a
sweep that did not see it) is not a starting point, unless the same value was
observed again after that moment, which the estimator takes as its value
there (its assumption: the same reading on both sides, nothing observed
between). At least 15 minutes
between the two observations is required. Zero growth is reported as "no growth observed in that
span", never as an unlimited reserve, and an unchanged reading is not proof
of zero use. `resolution_windows_per_hour` is kept for compatibility: the pace
one percentage point over the span would give — a reference scale, not an
error bound and not evidence of how a vendor rounds; nothing shows it as "±".
The group pace is the sum over accounts whose pace is known; with
some unknown it is "at least" that and marked partial.

**The chart** is folded until the owner opens it (the widget then asks the
route for it; while folded it sends `chart=0` and the chart and its longer
history read are not computed). It shows one group (chosen in the chart; by
default the family's "lowest left" limit, which is then kept as a pick is: an
answer during an outage that names another limit "lowest left" does not move
it — only a click, or the limit's absence from a whole answer, chooses again)
over 24 hours or 7 days back and ahead,
one y-axis in account-windows, on local clock times. The time axis is the
chosen range in whole hours (it moves once an hour, not with every reading),
never where the record happens to begin; the chart says where its history
begins, and the future side is tinted as scenario:

- *observed* — every account of that limit with a record in the range
  (`past_accounts` of the `y_max` slots; `past_basis` `current`, `recorded` or
  `last_known` by how many of them have a current reading now), the same
  accounts whatever is fresh now, so a reading going stale never changes the
  past — where one of them has no value vouched for, the line has a gap, never
  a smaller total, and it ends where their record does unless every one of
  them has a current reading. An account with no record in the range at all is
  left out and counted apart, rather than blanking the range. The estimate's
  cohort is separate (below). A reading counts from its first sighting until
  the next one, never before it was seen, never across a break in that
  source's watch (including a sweep that did not see it) and never past its
  own reported reset. The line is **lossless**: every moment at which any
  account's value can change — each edge of what a source vouches for and each
  run's last sighting — is a vertex, and only a vertex equal to the one
  before it is dropped. Every such moment but a reported reset is a collector
  sighting, so a week holds at most about one vertex per sweep; above 12 000
  vertices the oldest part is left out and the chart says from when it is
  drawn (`past_clipped_before`). Wherever any account is not vouched for, the
  line stops; a gap is never a zero. Historical and current values use the
  same reset and same-moment value conflict policy. A compressed unchanged run
  retains endpoint observation times; if omitted intermediate times could
  change the choice between disagreeing sources, that historical interval is
  unknown. A reading the collector saw at **one sweep only** — a source seen
  once, or a change seen at the last sighting before a gap — is evidence of
  that moment and of no stretch of time: it is never held across a gap, and
  never erased either. At every such sighting the group's total is resolved
  by the same source policy and, where the line does not already show that
  total on one side of the moment, kept as a separate `points` entry
  `[t, v]` — or `[t, null]` where the sources seen then disagree, also where
  the line has no value there (the last sighting before a gap, or one inside
  it), provided every other account is spoken for at that sweep. The chart
  draws each as a dot of its own (hollow where the sources disagree), joined
  to nothing; a disagreeing one where no line runs sits on a separate rail
  marked ≠ above the value scale, never at a value;
- *no new use* — each account holds its current reading and refills to one
  full window only at its own next reported reset; kept in the data and the
  table, no longer drawn (0.7.0);
- *estimate* (`recent_pace`) — the accounts whose own pace qualified (the
  cohort, `recent_pace_scope.accounts` of `.of`), each continuing its observed
  net change and staying at zero once it runs out, **summed over the cohort
  alone**: an account left out is not held at its value inside the line. Where
  the cohort is not exactly the accounts the observed line sums (some measured
  accounts did not qualify, or the record holds accounts with no current
  reading now), the same accounts' observed total (`cohort_past`) is the line
  the estimate continues, over the pale area of the record. The cursor and its
  spoken read-out give the observed total "of N accounts" of the record
  (`past_accounts`), never of the current count — except where there is no
  record in the range at all: the line is then the current figure alone, at
  now, and that point is "of" the accounts read now (`accounts`), never "of
  0". The line stops at the first reported reset among the
  cohort (`stops_at_reset`, `until`): what a window does after its reset is not
  observed, and a reported reset time is not evidence of a full refill;
- *refill scenario* (`recent_pace_refill_scenario`) — the same pace with each
  account refilled to a full window at its next reported reset, drawn only
  when the reader switches it on in the legend;
- **no additional unreported resets** are assumed: only each account's next
  reported reset is applied, and no later reset is inferred from a window's
  length. The horizon is how far a line is drawn, not how far it is reliable;
  the lines are conditions, not a forecast, and no accuracy is claimed for
  them. Whole-percent readings do not establish how a vendor rounds: a small
  observed change is said as the change it is
  (`exhaust_before_reset_small_change` counts those resting on one percentage
  point or less), never turned into a confidence band.

The chart's scale is `slots`, every account the limit applies to, so it does
not move when a reading goes stale. With no current account (the quota facet
or the whole status not read now), the past line is the record of the accounts
shown as last known (`past_basis: last_known`), ending where the record ends,
never at a value now.

Reported resets are marked; gaps in the record are pale bands, never zeros;
the two sides of "now" are labelled observed and scenario. A cursor carries
the numbers: a hairline, a dot on each line and one tooltip naming the
limit's series at that moment, driven by the pointer or by arrow keys on the
focused plot, with a spoken read-out. Within six pixels of a single-sweep
point the cursor sits on it and says "observed at this sweep only" (or
"sources disagree at this sweep", and on the rail "no value here"), and an arrow step stops at every point it
would pass, so each can be reached and heard by keyboard. Under the chart stand at most three
lines — the estimate in one conditional sentence ("At the pace observed over
the last N min, if it continued and nothing refilled first, X would reach the
limit about T"; an account with no reported reset that would reach it is said
apart, "With no reset reported, X would reach it about T", never as "before
its reset"), who is not in it and why, and the conditions; the other notes
and a data table fold under "Notes and data table". The table
is a sample of the observed line (a stride of it and up to eight gaps'
beginnings; it says how many of how many recorded changes and gaps it lists)
and the newest eight single-sweep points, marked as such ("seen at this sweep
only", or "not settled" where the sources disagree), plus the scenario at
reported resets and a quarter, half and all of the horizon. The chart, its limit and
horizon, the table, an unfolded row, the keyboard focus and the cursor all
survive the automatic 30-second redraw. Refresh is disabled while a read is in the air, and a disabled
button cannot keep focus; a keyboard reader on it gets it back when the read ends, unless they have moved
elsewhere or left the frame. Times are shown in the frame's local time zone, named on screen, and
the reading observation time is shown apart from the moment the status was
read. The overview is not recomputed by a live Refresh; the widget says so
until the next reading, and a chart or family switch after a Refresh brings
that reading (see the collector above), never a read from before it.

## Model tool: quota_summary

`quota_summary(harness?: string, detail?: boolean)` returns compact JSON: per
group the family, limit, duration, remaining account-windows and measured
accounts, average, unrestricted windows, coverage, restrictions (by kind:
`cooling`, `model_exhausted`, `other_limit_spent`, `disabled`, `signed_out`,
`auth_failed`, `account_state_unknown`), observation
times, next reported reset, even pace to reset and recent pace, and
`tightest_in_family` on the limit the overview marks "lowest left"; plus the
history state. Its `unbroken_watch` is `{since, exact, lookback_seconds}`:
where the collector's current unbroken watch began (`exact: true`: a break,
or the first sweep kept, lies within the look-back) or only a lower bound
(`exact: false`: it began at or before `since`, further back than the
74-minute look-back pace needs). The widget says "watched without a break
since HH:MM" or "at least since HH:MM" — never "for the last 74 minutes":
the earliest sweep the look-back finds can lie up to a sweep inside it. It is found with a bounded query and one
index seek, never a scan of the whole history; 0.6.0's `session_start`, which
called that bounded boundary the start of the whole collection, is gone. `harness` limits the answer to one family; `detail` adds plan
breakdowns and why pace is unknown. Since 0.7.0 each group also says
`accounts_known_to_apply` (the slots), `last_known` (windows, accounts,
oldest observation) with `remaining_windows_with_last_known` when any is
carried, `unknown_accounts`, `applicability_unknown_accounts`, and a one-line
`headline` built from the same numbers; the answer carries `cached_facets` and
`roster` when a facet was answered from an earlier read. `remaining_windows`
stays current readings only; with no measured account the headline leads with
"no current reading of any of N accounts", never "0 account-windows left".
`exhaust_before_reset` counts the accounts whose recent pace, continued, would
reach the limit before their reported reset; those with no reported reset that
would reach it are counted apart (`reach_limit_no_reported_reset`,
`earliest_no_reset_reach_at`) — the same predicate as the widget's account
sentence (`reaches_limit`, with `reset_reported` on each bar). It performs one passive status read
(20-second bound) unless a read from the last 45 seconds, made after the last
Refresh, is at hand, reads the history, and computes the same numbers the
widget shows. It never refreshes a
provider, never changes model or account pins or routing, and never injects
chat or writes memory.

The tool's schema and description are static; numbers appear only in the tool
result, at the end of the conversation, when the model chooses to call it.
Nothing calls it automatically and nothing guarantees a prompt-cache hit.

## What it reads

The automatic 30-second visibility poll uses one existing passive endpoint,
through the host's own authenticated fetch:

    GET /api/claudexor/status

Fields consumed (exact wire names, verified against a live response of engine
3.3.15):

- `reads` — `ClaudexorStatusReads`: `catalog` / `accounts` / `quota`, each
  `ok` | `not_read` | `failed`. This is the provenance authority.
- `daemon` — `state`, `engine_version`, `self_started`, `runtime.last_error`.
- `harnesses[]` — `id`, `display_name`, `status`, `enabled`, `provider_family`.
  One agent family per card.
- `profiles.harnessAccounts[]` — the per-harness native login:
  `harness_id`, `native_credentials_enabled`, `native_login_detected`,
  `identity.{email,plan}`, `next_up.{kind,route,profile_id}`.
- `profiles.profiles[]` — named credential profiles as wrapper objects:
  `profile.{profile_id,harness_id,display_name,credential_kind,enabled}`,
  `status.{availability,verification,verification_source,last_verified_at,detail}`,
  `identity.{email,plan}`.
- `quota[]` — snapshots: `subject.{harness,subject_id,plan_label,credential_route}`,
  `constraints[].{id,label,used_ratio,window_seconds,resets_at,cooldown_until,applies_to_models}`,
  `availability.{state,blocking_constraints,model_scoped_exhaustions}`,
  `observed_at`, `freshness`.
- `quota_absences[]` — typed missing-snapshot evidence. Visible copy is always
  generic; only supported owner actions (`Sign-in required`, `Retry after Xm`,
  `No live quota source`) are projected, never raw reason/detail text.

One explicit owner action is separate from cached reads:

    POST /api/claudexor/quota/refresh

The extension calls that host action only from the Refresh button. The host
retains the Claudexor bearer token and returns the exact foreground quota
envelope. The widget merges only the quota facet by exact
`(harness, subject_id)`; it does not perform a second status GET.

`subject_id` is `null` for the native login and the profile id for a named
account; matching is EXACT on `(harness, subject_id)` so a named profile's
exhausted window is never reported as the default login's.

## Honesty rules (the point of this widget)

1. **Per-facet provenance, never a global verdict.** Each facet is labeled from
   its own `reads` value. A refused or unread facet is rendered as
   "not checked" / "unavailable". The status button carries a red pip whenever
   one of them did not answer, the status strip behind it names which, and a
   banner above the list names it again in the open — so a failure is never
   only one click away from being invisible. It is never rendered as
   "no quota", `0`, or an empty list.
2. **No invented number.** A missing `used_ratio` is "no usage numbers
   reported", not `0%` and not "unlimited". A ratio out of range or not a
   number is an "unreadable ratio", never clamped to 100% or 0%. Rounding
   never turns a share that is not at its limit into "100%", nor a used one
   into "0%". A missing `resets_at` prints nothing rather than a fabricated
   time.
3. **Stale is disclosed, not silently dropped.** A carried last-known value is a hatched bar in its
   row with its age on hover and its own labelled, dated line under the row figure ("Last known
   0.40 · 21 min"), never in the current number (the row figure, `measured`, `remaining_windows`);
   with no current reading the row figure is "—", never 0, while a measured 0 stays 0. A last-known
   value at the limit stays hatched and dated ("at the limit when last read (20m ago)"): never the
   red base, "at the limit until …" or any other current-exhaustion wording. In the account brief
   and Details it stays a last-known reading: an amber "Last known" panel (only when the reading has windows — an empty one is not drawn) with a muted
   (translucent neutral) share, observation age and an explicit
   statement that stale percentages are not used to grant routing. They never
   look like fresh red exhaustion, nor like an amber share held back now. A still-live cooldown carried by stale
   evidence may still deny or rank a route, so the widget does not claim the
   engine ignores that evidence.
4. **Per-model caps stay per-model.** A constraint with a non-empty
   `applies_to_models` never marks the whole account exhausted; it becomes a
   scoped note. A present `cooldown_until` in the future (or one that cannot be
   parsed) is a cooldown — the window or model is out for now, "cooling down"
   in amber, never "Limit reached", never the red of a spent share and never a
   reset. Red is only a measured share at its limit; a cooldown, a reported
   model exhaustion and any other hold are amber — on tiles, chips, pool
   chips, reset lines and their times alike. A hold reported apart from the
   windows (a cooldown on some models, a model limit reported out) is tied to
   a window only by the skill's `scope_key` of the whole scope, never by the
   printed names; a model's pool in the account row is grouped by the same
   key, so two scopes that print one name ("Fable", "M00 +24") stay two pools,
   each coloured only by its own hold (the chip's hover lists its scope). On
   the account row's lines under the bars, a model window its scope's hold
   covers reads "cooling down" or "limit reported" in amber, with that hold's
   end or reset, never "available"; the account's shared windows stay neutral.
   A live exhaustion that names no model holds nothing, as in the reserve: it
   is disclosed in Details only ("Reported model limit reached · models not
   named · reported until … · holds no window", muted) and colours no dot,
   chip, tile or pool.
5. **`local_store` verification is honest both ways.** It reads
   "Signed in — local session, not verified live"; only `verification_source:
   vendor` earns "Verified live". Neither is treated as an absent account.
6. **Degraded accounts keep their rows as "last known"** and lose any green
   verified claim; rotation wording counts only accounts actually signed in,
   and with the account list not read now no account is "next up"
   (`routing_read: false`): a kept list's routing verdict is last known.

## Interactive Features

- **Reserve overview first, as approved in v4**: above the account, one card for the chosen family: a status line beside the title ("All 19 read · observed 1 min ago", "15 current · 4 last known (21 min)", or "Claudexor not read now · all as read 3 min ago"; exact times, the status read and the time zone on hover), the unit ("Account-windows left · a full account counts 1 · limits are never added"), then one row per limit — its name ("5-hour", "Weekly · Fable"; a pool named after the family itself is not repeated), one bar per account the limit applies to, as tall as its share left on the same 0–100% scale in every row, fullest first, one fixed strip width per slot count (wide enough to point at; never gathered into "?×N" or an average): solid for a current reading, hatched for a dated last-known value, amber when a restriction holds it back, a red base at the limit, an outlined "?" (never an empty bar or a zero) after the rest for an account with no usable value. Each bar names its account, share, reset and age on hover and aloud, and selects that account. On the right the figure — account-windows left now of the accounts the limit applies to, current readings only ("10.35 of 19"; "— of 19", never 0, when none is current; a measured 0 stays "0.00") — and under it the average, or, when last-known values stand in the row, their own dated line ("Last known 2.12 · 21 min"), never added to the figure. A tail names one account at the limit or held back by name, else counts them, then unknown accounts and the next reset; the limit of lowest average share is marked "lowest left". Clicking a limit's name shows it in the chart; the chevron unfolds its details (scope, restrictions with the unrestricted windows, what is not counted, plans, shared sign-ins, resets, even use and recent pace). Below the rows one line names the family's accounts that stand in no row (switched off in Claudexor, or with no reading of these limits here or in the history) and whether the account list is kept from an earlier read. "How to read" and "Show chart" stay; both start folded on every mount (the chart is then neither computed nor read) and keep their state through the 30-second redraw. Rows keep the skill's fixed order; bars reorder only when a value really changes.
- **The account below, folded to a brief**: its labelled selector and Details button sit below the family overview. The selector retains the worst shared-window percentage and other accounts needing attention. The brief keeps observation age, failed verification, disabled/signed-out state, typed quota absence, cooldown scope/end/provenance, model restrictions, reported model exhaustions that may hold now ("Model limit reached · Fable · until …", or, with no or an unreadable reset, "Reported model limit reached · … · no reset time reported" in the muted voice) and excluded-reading notices visible; one whose reset has passed, one naming no model, and one whose model window the brief already names at its limit, stay in Details. A cooling account has one cooldown sentence rather than a repeated verdict. A spent quota window keeps its independent verdict and reset even when the account is also cooling. Ordinary window tiles, identity details and normal verification unfold through Details. With no overview available, account details remain fully visible. The account list expands in the document flow, with its own bounded scroll area, so it cannot be clipped outside a short frame.
- **Labelled family controls and explicit actions**: visible family names select the overview; Settings and Refresh use the same quiet controls as disclosures. Settings contains row detail, model filters, account folding and system state. The state indicator and visible banners retain daemon/facet failures. Settings tabs support arrow keys and Home/End; Escape returns focus to Settings.
- **One account at a time**: the selector names the account on screen, says how much of its hottest shared window is used and counts other accounts with problems. Its list retains every account’s state, live windows, typed generic state, or plan and observation age. Holds reported apart from a row's windows are named, not merged: "account cooldown"; one model hold by its model and kind ("model cooldown: Opus", "model limit reached: Fable"); several as a count ("2 model holds") whose title and spoken label name each scope, its end and its provenance. A hold a window of the row already shows is not repeated. Raw status detail and local paths stay out of visible text, ARIA and titles. Family state indicators and banners continue to speak for families beyond the current selection.
- **Accounts that do not work fold away**: an account with no login, one switched off in Claudexor, or one whose check failed cannot run anything, and in a family of several it buries the account that can. Those gather by reason at the bottom of the account list — under one row carrying a dot per reason and the number of them that need attention, so nothing hidden goes quiet; a family where nothing works has no such row and shows the reasons themselves. The Accounts tab in settings switches each reason on or off, all three start folded, and nothing folds at all while the accounts facet is unread.
- **One bar language**: every bar — the reserve's per-account bars, its "avg" bar, a window tile in Details, and the short bars on the account selector and in the account list — is as long (or tall) as the share LEFT on one 0–100% scale, in one neutral ink, over a hairline base that marks the whole scale; there is no coloured track and no usage threshold. Amber is a share held back now (a cooldown that covers the window — on the whole account, or on exactly the window's models, from whichever reading reported it, as the reserve's "cooling" restriction reads it; a model's cooldown does not hold the account's other windows —, a live reported exhaustion of exactly the window's models, a spent shared limit elsewhere on the account, an account that is signed out, switched off or failed its check); a share at its limit has no fill but a red base; a last-known reading is muted and claims neither; a window with no usable ratio has no bar at all, and its words say why. The number beside a bar — on a tile, the account selector and an account-list row — is the skill's own share used and always says so ("N% used", never a bare "N%"); it turns red only at the limit, and the exact share left is on the bar's hover.
- **Reset Times**: the moment a window resets and a cooldown ends, printed as a date and hour in tabular numerals — no per-second ticking and no layout shift.
- **Model Scoped Indicators & Last-known Bars**: Clean chips for per-model caps — red only when that model's measured share is at its limit, amber when a cooldown or a reported model limit holds it — and muted bars for cached historical readings. A model name shortened to "Fable +3" counts every name the chip leaves out, including names past the 24 sent.
- **The selected account, per limit**: under the account selector one compact line per limit of its family — its share left, its reported reset, "last known, read 21 min ago" or why it is unknown, a hold, and, only when its own observed pace continued would reach the limit before that reset (or, with no reset reported, at all; no reset is then named), "at its recent pace would reach the limit ~Fri 08:53". The full card stays behind Details.
- **Honest live Refresh**: the explicit button performs one host POST, uses the
  existing in-flight/disabled action lifecycle, and merges only returned quota
  evidence. Automatic polling remains passive GET. An older host reports that a
  newer Ouroboros is required instead of silently substituting a cached reload.
  Cached reads use a 25-second network bound; the foreground refresh may wait up
  to 180 seconds for the host's bounded handshake and sequential vendor work.
- **Every request ends**: the widget asks the bridge to bound each read at 60 s
  and the Refresh at 210 s (`init.timeoutMs`, above the route's own bounds) and
  stops waiting itself 5 s later on a host that does not. Headers or a body that
  never come, an abort, a bridge error, a body that is not a JSON object, an
  HTTP error and an answer that cannot be drawn all end the request and free
  Refresh; a late answer, or one after the frame was stopped or disposed,
  changes nothing. A Refresh with no answer in time is said to have an unknown
  outcome and is never sent again on its own — whether the widget's own bound
  ran out, or the skill's route read no answer from the host (its 180 s bound,
  a connection closed after the request went out, an answer that broke off or
  could not be read, a success answer that is not a refresh envelope such as
  `{}`: the route answers `outcome_unknown: true`, never "failed"; so does the
  widget for a success status whose body breaks off or is not the route's
  answer, and for a transport rejection before any status is available (which
  does not prove the POST was never delivered); only an answer that says it failed, or a request that never reached the
  host, is a failure). Passive retries are the ordinary poll, and a Retry button
  reads now. A read that fails keeps the newest screen re-read at that moment:
  every current bar becomes the dated last-known value it now is, or — past
  its reported reset, or with none past its window — a "?" with its last
  reading kept; the current figure, the next reset, pace, the estimate and
  the no-new-use line are withdrawn, no account is shown held back now, and
  each account's windows become last-known readings. The rest of the screen is
  read the same way, as the skill words a status it could not read: no facet
  is shown as read now (the settings pip says so), the daemon's state is "when
  last read", each account's check is "— last known" and none is "next up", a
  cooldown alone whose end has passed no longer reads "Cooling down", a
  switched-off account is "switched off when last read", plan splits and the
  answer's own stale/unreadable counts are withdrawn, and the chart's moment
  is labelled by the clock time it was read ("read 14:05"), not "now", on its
  own time axis. The banner says when the
  last answer was received, and that each value is dated by its own
  observation. When an answer cannot be drawn the screen before it stays,
  re-read the same way (the clock has moved since it was read),
  with the error and a Retry (which never wraps in a narrow frame); the error
  is logged, not swallowed. Raw transport text stays off the screen.

## Owner-controlled steps

The skill declares no secrets (`env_from_settings: []`). Its permissions are
`net` + `route` + `widget` + `tool` + `supervised_task`:

- `net` — the routes, the tool and the collector read the host's own endpoints
  over loopback with `urllib` (no external host and no proxy handler): passive
  status GET with a 25-second bound (20 seconds for the tool), and the explicit
  foreground quota POST with 180 seconds, only from the Refresh button;
- `route` + `widget` — the widget, its three routes and its tab;
- `tool` — the read-only `quota_summary` tool;
- `supervised_task` — the one history collector described above.

No secret key grant is required. Everything the skill writes lives in its own
state directory: `prefs.json` with three display choices, and the bounded
history, which holds no account name, address, credential or raw response.
Enabling a reviewed skill remains the owner's action in Skills.
