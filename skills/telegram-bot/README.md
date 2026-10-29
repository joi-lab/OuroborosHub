# Telegram Bot 0.8.0

A public Telegram bot connector for private-chat, group and channel places,
separate from the built-in owner-control Telegram bridge. It keeps incoming
events and outgoing delivery receipts in its durable inbox/outbox.

On Hosts that offer Presence continuation version 2, messages sent while
Ouroboros is working on an answer reach that answer as context, a busy
conversation waits without holding up other chats, and the reply answers the
last message the answer took into account. See
[Timely conversations](SKILL.md#timely-conversations-080).

In the place's connector settings, add **Words to notice**, one word or phrase
per line. Matching is literal, case-insensitive, and requires non-word
boundaries on both sides. The list is empty by default. Choose **Words you
chose appear (connector setting)** under **What calls it here** on the place's
sheet to let matching events wake it. Silent places can wake but still hold
replies and writes; bot messages still require the bot wake choice.

See [SKILL.md](SKILL.md#connector-settings-and-facts-063) for the fact rules,
setup, provider visibility and delivery behavior, and [CHANGELOG.md](CHANGELOG.md)
for release notes.

Offline tests from the Hub root:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q skills/telegram-bot/tests
```

The optional host intake tests run when the core checkout is on `PYTHONPATH`.
