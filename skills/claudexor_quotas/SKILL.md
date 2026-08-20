---
name: claudexor_quotas
description: Read-only widget showing quota windows and limits for every authorized Claudexor account, with per-facet read-state honesty.
version: 0.2.0
type: extension
runtime: python3
entry: plugin.py
permissions: [net, route, widget]
env_from_settings: []
when_to_use: The owner wants to see, at a glance, the quota windows, limits and reset times of every authorized Claudexor account, including which facets could not be read.
timeout_sec: 60
ui_tab:
  tab_id: quotas
  title: Claudexor Quotas
  icon: gauge
  span: 2
  render:
    kind: module
    entry: widget.js
---

# Claudexor Quotas (v0.2.0)

A read-only projection of the host's own account surface. It adds no gateway
route to the core repo, reads no daemon token, and mutates nothing.

## What it reads

One existing endpoint, through the host's own authenticated fetch:

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

`subject_id` is `null` for the native login and the profile id for a named
account; matching is EXACT on `(harness, subject_id)` so a named profile's
exhausted window is never reported as the default login's.

## Honesty rules (the point of this widget)

1. **Per-facet provenance, never a global verdict.** Each facet is labeled from
   its own `reads` value. A refused or unread facet is rendered as
   "not checked" / "unavailable" and the header names exactly which facets did
   not answer. It is never rendered as "no quota", `0`, or an empty list.
2. **No invented number.** A missing `used_ratio` is "no usage numbers
   reported", not `0%` and not "unlimited". A missing `resets_at` prints
   nothing rather than a fabricated time.
3. **Stale is disclosed, not silently dropped.** The runtime ignores a stale
   reading for routing decisions, so a stale window never colors the account's
   headline state — but the reading is still shown, labeled stale with its
   observation time, instead of being hidden (no silent omission).
4. **Per-model caps stay per-model.** A constraint with a non-empty
   `applies_to_models` never marks the whole account exhausted; it becomes a
   scoped note. A present `cooldown_until` in the future (or one that cannot be
   parsed) counts as spent.
5. **`local_store` verification is honest both ways.** It reads
   "Signed in — local session, not verified live"; only `verification_source:
   vendor` earns "Verified live". Neither is treated as an absent account.
6. **Degraded accounts keep their rows as "last known"** and lose any green
   verified claim; rotation wording counts only accounts actually signed in.

## Interactive Features (v0.2.0)

- **Dynamic Progress Bars**: Color-coded progress indicators (`<60%` emerald, `60–85%` amber, `>85%` rose/exhausted).
- **Live 1-Second Countdown Tickers**: In-place DOM updates for time until reset and cooldown expiry.
- **Client-side Tab Filters**: Instant filtering by `All`, `Active` (signed-in and enabled), and `Alerts` (exhausted quota, active cooldown, verification failure, disabled).
- **Model Scoped Indicators**: Visual badges distinguishing general account limits from model-specific caps.

## Owner-controlled steps

The skill declares no secrets (`env_from_settings: []`). Its permissions are
`net` + `route` + `widget`: `net` is declared because the route reads the host's
own status endpoint over loopback with `urllib` (no external host, no proxy
handler, 25s timeout), and no secret key grant is required. Enabling a reviewed
skill remains the owner's action in Skills.
