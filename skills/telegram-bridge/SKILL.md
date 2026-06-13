---
name: telegram-bridge
description: Bidirectional Telegram bot bridge for Ouroboros with configurable command modes, inline keyboard control panel, and optional silent (edit-in-place) mirror.
version: 2.5.0
type: extension
entry: plugin.py
runtime: python3
permissions: [net, read_settings, widget, route, supervised_task, subscribe_event, inject_chat]
env_from_settings: [TELEGRAM_BOT_TOKEN, OPENAI_API_KEY]
subscribe_events: [chat.outbound, chat.typing, chat.photo, chat.video]
when_to_use: User wants to communicate with Ouroboros through Telegram.
timeout_sec: 60
---

# Telegram Bridge

Bidirectional Telegram bridge for Ouroboros. Polls Telegram for inbound
text/photos and mirrors host chat output back to Telegram.

## Command Modes

Three configurable security modes control which slash commands are accepted
from Telegram (configure in Settings → Telegram Bridge):

| Mode | Allowed commands | Blocked |
|------|-----------------|---------|
| **strict** | None — all slash commands blocked | Everything with `/` |
| **safe_commands** | `/status`, `/bg`, `/bg status` | Mutating commands |
| **full_access** (default) | Raw owner commands including `/panic`, `/restart`, `/review`, `/evolve`, `/bg` | Unknown commands only |

**Important:** In `full_access`, this reviewed transport is a first-class owner
chat surface. Slash commands are forwarded as raw chat text through the Host
Service after the skill passes review, grants, enablement, token, rate-limit,
and chat/user binding checks.

## Subagent Activity

Ouroboros emits a progress event for every subagent state change (a parallel
swarm can fan out dozens). Instead of flooding Telegram with one bubble per
event, the bridge gives **each subagent a single message that is edited in
place** across its lifecycle (`🔵 queued → 🟡 running → ✅ done · $cost`). Each
card shows a status header **plus the latest work note** — the live commentary
you see in the Ouroboros web UI (e.g. "I will read lines 710–765 of llm.py…") —
so the bubble updates as the subagent works rather than posting a new line per
note. (An identical-text edit that Telegram rejects with "message is not
modified" is treated as a no-op, never as a reason to post a duplicate.) The
"Ouroboros is typing…" indicator covers the overall working signal, and your
real replies stay clean, separate messages.

Two settings (Settings → Telegram Bridge):

- **Subagent cards** (default *on*) — one updating message per subagent. Turn
  *off* to hide subagent activity entirely.
- **Mirror progress telemetry** (default *off*) — when *on*, also mirrors the raw
  non-subagent progress firehose (thinking notes, cost lines) as in older
  versions. Leave *off* for a clean replies-only chat.

## Push Notifications

Beyond mirroring chat replies, the bridge can send **proactive** notifications to
the pinned chat — useful for background/scheduled tasks that finish without you
watching. A separate `notifier` supervised task reads durable state files every
30s (read-only) and pushes structured one-liners. Both are **off by default**
(opt-in via Settings → Telegram Bridge):

- **Notify on task completion** — when a task writes its summary, push
  `✅ Task <id> done · <rounds>r · $<cost>` (cost from `task_results/<id>.json`
  when available; `⚠️` instead of `✅` for non-completed outcomes). Enabling the
  toggle primes against the existing backlog, so you only get notified about
  tasks that finish *after* you turn it on — no flood of old summaries.
- **Notify on budget thresholds** — push `⚠️ Budget: N%` once when cumulative
  spend crosses 80% / 90% / 100% of `TOTAL_BUDGET`. Each threshold fires once;
  raising the budget (or a spend reset) re-arms the lower thresholds.

Notifications require a pinned `TELEGRAM_CHAT_ID`; with no pinned chat the
notifier stays idle. Dedupe/threshold state lives in the skill's own state dir
(`notif_state.json`), never in core settings.

## Inline Keyboard

Send `/menu` in Telegram to get an inline button panel with available
commands (adapts to the current command mode). Button presses use non-slash
callback identifiers internally, then map to the same allowed command text as
ordinary Telegram messages.

## Silent Mode

When enabled (Settings → Telegram Bridge → Silent mode, or the inline
`🔕 Silent Mode` toggle inside `/menu → ⚙️ Settings`), successive outbound
messages within a single conversation turn are edited in place via
`editMessageText` instead of posting new bubbles. Each new inbound user
message (or sent photo/video) resets the silent chain so the next reply
starts a fresh bubble. Default: off.

## Setup

1. Set `TELEGRAM_BOT_TOKEN` in Settings → Secrets
2. Grant the token to this skill
3. Configure command mode, mirror mode, and chat ID in Settings → Telegram Bridge
4. Toggle the skill on

`TELEGRAM_BOT_TOKEN` is a protected secret and requires an explicit owner
grant before the skill can run. Chat routing settings such as `TELEGRAM_CHAT_ID`
are owned by this skill's settings panel rather than by core settings. Use
`full_access` only for a bot/chat you trust as an owner channel.
