# Changelog

## 0.8.0 — 2026-10-29

- Request Presence continuation version 2 when `/identity` lists it in
  `presence_continuation_versions`. Store the early `continuing` answer's real
  reference with the submitted event, move the conversation's input queue on and
  poll the author with the existing work worker. Other Hosts keep completed-only
  requests.
- Startup bursts and corrections sent while the author reasons reach it as
  context and are acknowledged without speech.
- Retry a busy `presence_capacity_full` admission (`conversation` or `capacity`)
  as the same first event after the Host's `retry_after`, never shortened (at least one second),
  without a growing delay or a counted attempt, while other
  chats proceed.
- Reply to the covered message a version-2 `reply_anchor` names when this chat
  captured it; promoted children, retained references and older Hosts keep
  answering the original event.
- Send each `output_ref` once per event across the author, its children, the
  original turn and retained references.
- Show the negotiated `presence_continuation_version` in transport status.

## 0.7.0 — 2026-10-26

- Capture private chats that people open with the bot on Places hosts and report
  their messages with the declared `direct_message` fact. It joins the suggested
  wakes; existing place wake lists are not changed by the upgrade.
- Check replies to a private chat against its place like any other place instead
  of holding them as unsupported.
- Explain in the transport tab what the bot can reach and that the Bot API cannot
  read earlier history; a dedicated account is the separate
  `telegram-user-session` connector.

## 0.6.3 — 2026-10-26

- Add the connector's `words` setting, empty by default, with one literal word
  or phrase per line and saved-value loading in the settings form.
- Report `signal.words` on matching message text and captions, including edits,
  using case-insensitive whole-word matching. Retain the fact with the incoming
  event; negotiate signal support before sending it to the host.
- Declare the signal's label in the manifest. The owner selects it per place;
  wake suggestions, bot gating and Silent reply/write holds stay as before.
- Share settings reads between the form and poller; preserve binding and group
  settings when only words change, and preserve words when those settings change.

## 0.6.2

- Declare the bot's sending identity from Telegram `getMe` on status and events
  when the host advertises identity support.
