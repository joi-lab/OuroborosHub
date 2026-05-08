---
name: telegram-bridge
description: Bidirectional Telegram bot bridge for Ouroboros. Polls Telegram for inbound text/photos and mirrors host chat output back to Telegram.
version: 1.0.0
type: extension
entry: plugin.py
permissions: [net, read_settings, widget, supervised_task, subscribe_event, inject_chat]
env_from_settings: [TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]
subscribe_events: [chat.outbound, chat.typing, chat.photo]
when_to_use: User wants to communicate with Ouroboros through Telegram.
timeout_sec: 60
---

# Telegram Bridge

This skill moves the Telegram bridge out of the core runtime. It uses a
host-supervised polling task for Telegram `getUpdates`, injects inbound
Telegram messages through the loopback Host Service API, and mirrors
outbound chat/typing/photo events back to the configured Telegram chat.

`TELEGRAM_BOT_TOKEN` is a protected setting and requires an explicit owner
grant before the skill can run.
