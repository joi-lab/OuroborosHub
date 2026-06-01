---
name: telegram-bridge
description: Bidirectional Telegram bot bridge for Ouroboros with configurable command modes, inline keyboard control panel, and optional silent (edit-in-place) mirror.
version: 2.2.0
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
| **strict** (default) | None — all slash commands blocked | Everything with `/` |
| **safe_commands** | `/status`, `/bg`, `/bg status` | Mutating commands |
| **full_access** | Raw owner commands including `/panic`, `/restart`, `/review`, `/evolve`, `/bg` | Unknown commands only |

**Important:** In `full_access`, this reviewed transport is a first-class owner
chat surface. Slash commands are forwarded as raw chat text through the Host
Service after the skill passes review, grants, enablement, token, rate-limit,
and chat/user binding checks.

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
