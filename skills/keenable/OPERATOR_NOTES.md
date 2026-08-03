# keenable — operator notes

Keenable web search + page fetch as an Ouroboros extension skill, over the
vendor's **keyless** streamable-HTTP MCP endpoint.

## Turning it on

1. Skills → **keenable** → run review if it is not already reviewed.
2. Toggle **enabled**.

That is all. **No key, no account, and no grant are required** — the keyless path
is primary and complete.

## The optional key

Add `KEENABLE_API_KEY` in **Settings → Secrets** only if you want the higher
quota. It then rides the same MCP requests as `X-API-Key`.

Two consequences, stated precisely:

- An **unconfigured** key requires no grant. `requested_core_setting_keys`
  (`ouroboros/skill_loader.py:586`) asks for a grant only for forbidden built-in
  keys, `TELEGRAM_BOT_TOKEN`, or a custom secret **already present in settings**.
  So while you have not added it, readiness is unaffected.
- Once the key exists in settings it becomes a grant-bound custom secret, so the
  skill will request a key grant (auto-grant is on by default). Adding a key is a
  separate owner-granted upgrade, not the baseline.

A **wrong** key is worse than no key: the vendor answers `401 requires_auth` and
issues no session, which breaks the keyless path too. The skill reports
`keenable_auth_invalid_key` and tells you to remove or correct the value. It does
not retry without the header, because silently ignoring a configured credential
would also silently change your quota tier.

## Vendor limits you inherit

- 1000 requests/hour **per IP** without a key; a key removes the hourly cap.
- 10 requests/second either way.
- **No `Retry-After`, no `X-RateLimit-*`.** Quota exhaustion is visible only as an
  error, so the skill never auto-retries a 429 — with up to 10 workers plus a
  subagent swarm behind one IP, a retry loop drains an hourly quota in minutes.
- Keenable's Terms define User Content broadly and take a perpetual, irrevocable,
  sublicensable licence over what you send, including for AI-model development,
  with retention stated only as "as long as reasonably necessary". Send public,
  minimal queries; never conversation history, repository excerpts, or personal
  data.

## Why a skill and not two settings lines

`ouroboros/mcp_client.py` already speaks `streamable_http`, so
`MCP_SERVERS=[{url: "https://api.keenable.ai/mcp", transport: "streamable_http"}]`
with `MCP_ENABLED=true` would expose both vendor tools with no new code. That was
considered and deliberately not chosen: the owner wanted a per-skill enable rather
than a global MCP toggle, a payload that can be published to OuroborosHub, and
normalized/parsed results with a typed error taxonomy. This note exists so a later
maintainer does not "simplify" the skill away without knowing the trade.

The payload also does **not** import `ouroboros.mcp_client`: it is unfrozen core,
needs the `mcp` SDK, and importing core internals from a data-plane skill is
coupling a reviewer should reject. The protocol needed here is one POST plus one
header.

## Result shape

Success carries `results` (`title`, `url`, `snippet`, `published`, `acquired`),
`auth` (`keyless` / `api_key`), `count`, `results_omitted`, `parse_status`, and
`untrusted_external_data`. The vendor returns one text blob rather than structured
JSON, so the skill parses it; the untouched `raw` text accompanies anything short
of a clean `records` parse instead of silence, and `include_raw: true` adds it to
that case too. Every bound is disclosed (`raw_truncated`, `raw_chars_total`,
`content_truncated_by_skill`, `content_chars_total`).

**v0.2.0 renamed the skill-owned fields, and the reason is the point of the
release.** `content_truncated` / `content_char_limit` / `filters` described what
*this skill* did — our clip and our outgoing arguments — and were read downstream
as vendor guarantees of page completeness and filter enforcement. A researcher
consequently recorded "the source does not contain X" for pages that did contain
X. The names now carry their owner (`content_truncated_by_skill`,
`skill_content_char_limit`, `filters_requested`), `vendor_content_complete` is
permanently `null` because it is unknowable here, `measured` carries raw counts
instead of conclusions, and `content_incompleteness_indicators` is a list whose
empty value means *no indicator found* rather than *complete*. There is
deliberately **no boolean** on the completeness axis: a `false` there would be a
machine-emitted completeness attestation, i.e. the original defect wearing a new
name. `parsed` was dropped rather than kept for compatibility, because `true`
merely meant "at least one record parsed" and silently hid discarded blocks —
`parse_status` (`records` / `partial` / `no_records` / `unparsed`) is now the
single parse authority, and `error_class` is the single authority on what a
failure implies (`not_read` = no page content was produced, so it is not evidence
of absence).

Record fields are bounded as well as counted: `title`/`url`/`published`/`acquired`
at 500 chars, `snippet` at 1200, at most 8 `extra` fields, and an aggregate
10000-char budget across all records — every cut disclosed via `<field>_truncated`,
`<field>_chars_total`, `extra_truncated`, `extra_omitted`, `results_omitted`. Without
these, `snippet_max_length: 10000` × 12 results would hand the host ~120KB to clip
generically instead of the skill disclosing it. The fetch `prompt` ceiling of 2000
chars is enforced locally (`keenable_bad_request`), not merely documented.

`RESULTS_TEXT_BUDGET` bounds accumulated **retained field text**, not serialized
JSON bytes — the wire payload additionally carries keys, syntax, and the
disclosure metadata itself. It is checked before **every field write**, so it is a
ceiling rather than an estimate.

**v0.3.0 adds the bound that actually matters: `ENVELOPE_CHAR_BUDGET = 13500` over
the SERIALIZED envelope.** A budget over retained field text is not a bound on what
the host receives, and the host caps a generic tool result at 15000 chars
(`ouroboros/tool_capabilities.py::DEFAULT_TOOL_RESULT_LIMIT`; extension tools are not
exempt). Measured: the old 20000-char retained budget serialized to **20066**, and a
full-page fetch to **15099** — in both cases the host would have truncated an
envelope whose entire purpose is that *its* bounds are the disclosed ones, while
`content_truncated_by_skill` still read false. Three consequences, all measured
rather than guessed: `RESULTS_TEXT_BUDGET` is 10000 (12237 serialized worst case),
`CONTENT_CHAR_LIMIT` is 9500 (12597, and it is also what the vendor is asked for, so
we do not pay for discarded text), and `raw` no longer takes a fixed 9000-char slice
on top of the records — it takes whatever the measured envelope still has room for,
reporting the limit that *actually applied* in `raw_char_limit` rather than the
constant.

Two further holes in that bound were found by review and are closed. **Echoed
values are now bounded**: `url`, `query`, each string in `filters_requested`, and the
vendor's own `served_url` / `served_title` were unbounded, so the envelope's size
bound was only a bound on the parts we had thought of — a 5000-char url or a
6000-char vendor header pushed a legitimate response over the cap. Each is clipped
to `FIELD_CHAR_LIMIT` with `<field>_truncated` / `<field>_chars_total`, and the
redirect comparison runs on the **full** values first, so a size bound can never
change the verdict it reports. And **`fit_envelope` is the final gate on both
paths**: every individual bound can hold while the total still overflows, because
the total is assembled by different code paths at different times. So the size is
measured once at the end on the real serialized payload; if it does not fit, the
designated body (`content` for fetch, `raw` for search) gives up the difference,
disclosed as `envelope_overflow_trimmed`, and only then are records dropped from the
tail into `results_omitted`.

Two smaller instances of the same class, both found by review after the above and
both worth naming because they show how easily this defect hides. A shortened
**filter** value was echoed with no disclosure at all (`clip(v, ...)[0]` threw the
metadata away, inside the very code whose job is to disclose), so a truncated filter
read as the complete requested one; the original length is now reported in a sibling
`filters_truncated` map, kept out of `filters_requested` so a vendor filter name can
never collide with our metadata. And a **failure** envelope's `message` was unbounded
while carrying vendor-controlled text — `keenable_tool_error` is built from the
vendor's own tool output — so a hostile or broken error could have approached
`MAX_RESPONSE_BYTES` and been truncated by the host, taking `error`/`error_class` with
it. That is the worst place to lose disclosure, since `error_class: not_read` is
exactly the field that stops an unread page being read as an absent fact. Bounded at
`ERROR_MESSAGE_LIMIT` with `message_truncated` / `message_chars_total`; a 60000-char
vendor error now serializes to 1561 chars with its typing intact.

**`verify_envelope_bounds.py` ships inside the payload** and asserts all of this —
run `python3 verify_envelope_bounds.py` from the skill directory; it needs no network
and exits nonzero with the offending sizes. Ten shapes are covered: records at full
budget, a partial parse carrying raw, `include_raw` on a clean parse, a full page
with a maximum-length prompt, an oversized caller url, oversized vendor headers, an
oversized query and filter, an over-ceiling `max_chars`, a 60000-char vendor error
message, and an oversized filter value whose original length must be reported. It is in the payload
rather than a scratch directory on purpose: it is hashed and reviewed with everything
else, and a documented size with no shipped check is a promise of a guard that does
not exist — the same defect one level up from the one this envelope was rewritten to
remove. An earlier draft of this very note claimed a `verify_v030.py` that lived only
in the author's task drive; review caught it, which is the argument for the file
existing.

Checking `RESULTS_TEXT_BUDGET` once per record, as an earlier
revision did, let a record that began just under the limit add four more fields
plus eight `extra` pairs on top of it: an overshoot of roughly 10KB that no field
flag described. A record is now refused outright once the remaining budget falls
below `MIN_VIABLE_RECORD_CHARS` and counted in `results_omitted`, because an
omitted record is honest while a record whose `url` was clipped to a prefix is a
broken link the operator cannot distinguish from a working one.

That refusal was necessary but not sufficient, and v0.2.0 closes the remaining
hole with **two independent guards**. `_bound_records` bounded `title` before
`url`, so an admitted record could still spend its remaining budget on a long
title and retain only a `https://…` prefix; `url` is therefore now bounded
**first**, ahead of the decorative field. Independently, `plugin._results_markdown`
refuses to linkify any record carrying `url_truncated` and renders
`_truncated url withheld_` instead — `_http_url` only validates the *scheme*, so a
clipped prefix passes it and would otherwise become a clickable link that looks
exactly like a valid one. Either guard alone leaves the class open on some path,
which is why both exist.

`extra` **keys are never truncated**. A key longer than 500 chars is dropped and
counted in `extra_keys_dropped`; one that no longer fits the shared budget is
counted in `extra_budget_dropped`. Clipping keys had a defect worse than the
missing flag it was reported for: two long keys sharing a 500-char prefix
collided and silently overwrote each other in the same dict. Dropping removes
that class instead of disclosing each instance.

**v0.3.0 adds three observations, all additive.** `served.served_url` is the url
the vendor says it *actually served*, kept beside the requested `url`; when they
differ, `served_url_differs_from_request` fires. This is the field that closes the
worst remaining case: a site answering a bot challenge got that challenge page
extracted and returned as a clean success, and the envelope said nothing —
`openreview.net/forum?id=…` arrived as `openreview.net/challenge?redirect=…` with
`ok: true` and an empty indicator list. `body_under_500_chars` covers the same class
where the url happens to match (t.me returned 146 chars, Google Scholar 267, both
`ok: true`); the bound is in the indicator's *name* because it is a measurement and
a genuinely short page trips it legitimately. `index_freshness` reports the newest
`acquired`/`published` dates the records actually carry — a lower bound on index
freshness, which is what separates "not indexed yet" from "does not exist", plus
`requested_after_beyond_observed` for a filter asking beyond it. The redirect
indicator is also evaluated for `prompt_extraction`, where v0.2.0 returned an
unconditionally empty list — the worst case, since a short prompt answer looks
normal. `filter_observations` now covers the `acquired` pair too; it always could,
and `acquired` is the field the vendor actually fills.

One class is deliberately **not** detected, and pretending otherwise would be the
same defect in reverse: on arXiv `/abs/` pages the vendor drops the author list
while keeping title, abstract, subjects, DOI and submission history. The text is
well-formed, the right size, matches the requested url and carries no markup
residue, so no measurement distinguishes it from a complete extraction. It is
documented in `SKILL.md` with the `/html/` workaround instead of guessed at.

Failures carry a typed `error`: `keenable_auth_invalid_key`,
`keenable_auth_required`, `keenable_rate_limited`, `keenable_server_error`,
`keenable_timeout`, `keenable_transport_error`, `keenable_tool_error`,
`keenable_protocol_error`, `keenable_bad_request`.

## Widget presentation, and why the route payload differs

The widget renders through the host's **declarative schema v1** only — no
`kind: module`, no skill JavaScript, so no sandbox or extra review surface. The
schema is a module-level literal (`_UI_RENDER` in `plugin.py`) precisely so
`skill_preflight` can parse it statically; keep it that way and do not mirror it
into `SKILL.md:ui_tab`, which would be a second source of truth that drifts.

Presentation is **route-only**. `_present_search`/`_present_fetch` run in the
route adapters; the tool adapters return the client envelope unchanged, so the
agent pays no tokens for UI-only fields. The narrow invariant, stated honestly:
non-truncating inputs are byte-identical to earlier revisions, while over-budget
inputs change *only* through the newly disclosed bounds above.

Three host contracts constrain this and are easy to break by accident:

- **A truthy top-level `error` is fatal to the whole view.** `callWidgetRoute` in
  `web/modules/widgets.js` does `if (!resp.ok || data.error) throw new
  Error(data.error || ...)`, and the form handler then replaces the entire widget
  state with `{error: err.message}`. Since that message *is* `data.error`, a typed
  code became the only thing on screen — which is exactly what the raw-JSON
  complaint was about. The route therefore reports failure as `ui_has_error` +
  `ui_error_text` and omits `error`, always at HTTP 200. A second callout stays
  bound to `error` so a genuine host-level transport failure still shows its own
  message rather than nothing.
- **Containers do not hand down `target`.** `passiveTarget = inheritedTarget ?
  target : ''`, so a top-level `group`'s children inherit nothing. Every
  data-bound node carries its own explicit `target`.
- **`condition_key` is plain truthiness via `getPath`.** `ui_has_error`,
  `ui_empty` and `content_truncated_by_skill` must stay real booleans and
  `ui_clipped` a real `int` — a string `"0"` would invert the intended show/hide.
  v0.3.0 moved the bounds disclosure off that int: it was gated on `ui_clipped`,
  so when nothing happened to be clipped the sentence vanished and took the
  "N results omitted" disclosure with it — results silently missing from a list
  that looked complete, i.e. this skill's own failure class hiding inside its
  disclosure code. It now gates on `ui_bounds_text`, its own string, like every
  other note.
  The v0.2.0 disclosure callouts (`ui_incomplete_text`, `ui_cache_text`,
  `ui_clamp_text`, `ui_filter_note`, `ui_parse_note`) deliberately gate on their
  **own** string field, so a presenter that has nothing to say returns `""` and
  the callout renders nothing at all rather than an empty box. That is why each of
  those helpers returns the empty string instead of a generic placeholder: a
  warning that always fires is a warning nobody reads, which is the failure mode
  this release exists to fix.

Untrusted vendor text is escaped for markdown *structure*, not merely for HTML:
titles are collapsed to one line and escaped for mid-line actives, snippets are
emitted as a blockquote with every line prefixed (empty lines included, since an
unprefixed blank line terminates the quote) and line-leading markers plus
backticks defused, so a snippet cannot inject a heading, a table, a fake link, or
an unterminated code fence that swallows the rest of the list. A title or snippet
never begins a rendered line, which is why line-only markers such as `-` are left
alone inline and dates read as `2026-01-01` instead of `2026\-01\-01`. The domain
is sanitised by *removal*, not escaping, because it is displayed inside a code
span where backslash escapes are not processed and would be shown literally. A
url is linked only when it is `http(s)`; anything else is rendered as text with
`non-http url withheld`.

The widget form is deliberately the common case (query, site, published
before/after). The full filter surface — `acquired_after`/`acquired_before`,
`snippet_max_length`, `mode`, `include_raw` — belongs to the agent tool.

The collapsed debug sections are labelled **Raw search payload** / **Raw fetch
payload** rather than "raw JSON" on purpose: each is that route's payload, carrying
the `ui_*` keys and omitting `error`, and neither is byte-identical to what the
agent tools return. They were both called "Raw route payload" until v0.3.0, which
made them distinguishable only by their position on screen.

Two v0.3.0 presentation choices worth knowing before changing them. Snippets are
shortened to **340 characters for display only**, with the number shortened stated
above the list: at the client's full 1200-char bound, ten results rendered as one
unbroken column of prose in which individual items were not visually separable. The
agent envelope and the raw payload both still carry the full text, so nothing is
lost — but this *is* a presentation-layer reduction and it is disclosed rather than
silent. Result blocks are also separated by a horizontal rule, because with only a
blank line a five-line snippet ran straight into the next result's number.

## Durability warning

This payload lives under the runtime data root, **outside the git repo and outside
the commit gate**. It is not covered by git history, and it has not been published
to OuroborosHub (`submit_skill_to_hub` needs a `GITHUB_TOKEN`, which is not
configured). Until it is published, keep a copy: a data-directory reset destroys
the only one.
