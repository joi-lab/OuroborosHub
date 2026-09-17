# Telegram presentation helper provenance

`telegram_bot/formatting.py` includes the pure text/HTML helpers from
[Ouroboros at 82508f56e02918819cdf3db9f7c4d80038740690](https://github.com/razzant/ouroboros/blob/82508f56e02918819cdf3db9f7c4d80038740690/skills/telegram/lib/telegram_api.py).
They retain their original function bodies and constants. `prepare_text` and
`prepare_caption` are local wrappers for the durable Presence transport. The MIT
notice is preserved in `LICENSE.formatting`.

The native helper is internal to a separately installed skill, not an exported
PluginAPI. Keeping its reviewed source in this payload makes installation and
hash-bound review self-contained. No runtime import reads a sibling skill or an
installation-specific source checkout. When updating this included source,
compare the named upstream functions and rerun the formatting and delivery tests;
never silently pick up a different installed native version.

Behavior tests are adapted from the same revision's
`tests/test_telegram_markdown.py` and `tests/test_telegram_format_parity.py`, with
additional fake-provider checks for topic/reply routing and durable retries.
