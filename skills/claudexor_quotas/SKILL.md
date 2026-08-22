---
name: claudexor_quotas
description: Read-only widget showing quota windows and limits for every authorized Claudexor account, with per-facet read-state honesty.
version: 0.3.0
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

# Claudexor Quotas (v0.3.0)

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
   "not checked" / "unavailable". The status button carries a red pip whenever
   one of them did not answer, the status strip behind it names which, and a
   banner above the list names it again in the open — so a failure is never
   only one click away from being invisible. It is never rendered as
   "no quota", `0`, or an empty list.
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

## Interactive Features (v0.3.0 Redesign)

- **One control row, no header**: the frame is short and the host already prints the widget's name, so there is no title row. A status button carries a pip — green when all three facets answered, red when one did not — and opens sideways into daemon state, engine version and per-facet read state (`catalog`, `accounts`, `quota`). A facet that did not answer also raises a banner above the list, so a failure is never hidden behind the button.
- **One account at a time, chosen in the row**: the frame opens at 320px and grows only when the module asks, so the screen shows one account in full — every window, every reset time — instead of a list whose remainder is scrolled out of sight without a scrollbar to say so. The family is picked from a segment carrying each vendor's own mark; the account selector beside it names the account on screen, shows how full its hottest window is and says how many of the family's other accounts need attention; opening it gives every account a state dot and a second line — its live quota windows, or the engine's own sentence about what is wrong with it, or its plan and how long ago it was checked. What is hidden still speaks: a pip on the family mark whenever any of its accounts needs attention, and the banners above the account speak for every family, not for the selection.
- **8px Gradient Progress Bars**: Height-expanded progress indicators with smooth transitions and theme gradients (`--grad-ok` Emerald, `--grad-warn` Amber, `--grad-bad` Ouroboros red, diagonal striped unmetered).
- **Reset Times**: the moment a window resets and a cooldown ends, printed as a date and hour in tabular numerals — no per-second ticking and no layout shift.
- **Model Scoped Indicators & Stale Accordions**: Clean chips for per-model caps and styled warning accordions for cached historical readings.

## Owner-controlled steps

The skill declares no secrets (`env_from_settings: []`). Its permissions are
`net` + `route` + `widget`: `net` is declared because the route reads the host's
own status endpoint over loopback with `urllib` (no external host, no proxy
handler, 25s timeout), and no secret key grant is required. Enabling a reviewed
skill remains the owner's action in Skills.
