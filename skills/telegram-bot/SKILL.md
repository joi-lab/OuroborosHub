---
name: telegram-bot
description: Durable Telegram transport for generic Ouroboros presences, with exact actor and conversation
  provenance, media staging, and provider receipts.
version: 0.4.0
type: extension
plugin_api: '2.0'
runtime: python3
entry: plugin.py
permissions:
- net
- fs
- read_settings
- supervised_task
- route
- widget
- tool
- presence
env_from_settings:
- TELEGRAM_PUBLIC_BOT_TOKEN
timeout_sec: 60
when_to_use: The owner wants a reviewed Telegram bot transport for one or more external presence
  bindings, separate from the native owner-control Telegram bridge.
ui_tab:
  tab_id: transport
  title: Telegram Transport
  icon: message
  render:
    kind: declarative
    schema_version: 1
    components:
    - type: form
      route: settings/save
      method: POST
      submit_label: Save transport settings
      fields:
      - name: binding_id
        label: Presence Binding ID
        type: text
        placeholder: 32-character binding id
        help: Owner-created binding for this Telegram transport (conversation_id=exact id or conversation_id=*).
      - name: management_group_id
        label: Management group ID
        type: text
        placeholder: Optional numeric Telegram group ID
        help: Marks this room in event facts. Work instructions stay in the behavior profile; no
          owner permissions are granted.
    - type: poll
      route: status
      method: GET
      target: status
      interval_ms: 5000
      auto_start: true
      max_ticks: 100
      label: Refresh transport status
    - type: callout
      path: runtime_state
      tone: info
      target: status
    - type: group
      title: Provider custody
      layout: grid
      columns: 4
      components:
      - type: metric
        label: Inbox waiting
        path: inbox_waiting
        target: status
      - type: metric
        label: Inbox leased
        path: inbox_leased
        target: status
      - type: metric
        label: Submitted
        path: inbox_submitted
        target: status
      - type: metric
        label: Inbox failed
        path: inbox_failed
        target: status
      - type: metric
        label: Outbox waiting
        path: outbox_waiting
        target: status
      - type: metric
        label: Delivered
        path: outbox_delivered
        target: status
      - type: metric
        label: Outbox failed
        path: outbox_failed
        target: status
      - type: metric
        label: Telegram offset
        path: telegram_offset
        target: status
      target: status
    - type: kv
      fields:
      - label: Bot
        path: bot_label
      - label: Last provider event
        path: last_event_at
      - label: Last delivery
        path: last_delivery_at
      - label: Last error
        path: last_error
      target: status
tools:
- name: telegram_send
  description: Queue a proactive Telegram text, photo, or document for durable delivery.
- name: telegram_moderate
  description: Queue exact-message deletion or member restriction, ban, or unban with durable receipts.
- name: telegram_operation
  description: Queue an own-message edit or reaction through the Telegram Bot API with durable receipts.
- name: telegram_receipt
  description: Inspect delivery state and provider receipts for a queued Telegram operation.
---

# Telegram Bot Presence Transport

This extension owns Telegram provider custody. It is deliberately separate from
the bundled owner-control Telegram bridge.

It provides:

- exact Telegram actor, chat, topic, message, reply, and attachment provenance;
- a SQLite inbox/outbox with stable event ids, leases, deduplication, bounded
  delivery retries, terminal failure state, and an offset committed in the same
  transaction as each accepted update;
- inbound photo/document staging through Telegram `getFile`;
- outbound text, photo, and document provider helpers with durable receipts;
- a namespaced `telegram_send` tool for proactive text/photo/document delivery
  to exact numeric chat, topic, and message ids;
- a bounded poller and fixed worker set; and
- an operational Widget showing provider and custody state.

The payload does not call the owner `/chat/inject` path, allocate synthetic
internal chats, interpret owner commands, or create prompt envelopes. It uses
only the reviewed `presence` Host permission. Configure the opaque,
owner-created binding (an exact conversation or `conversation_id="*"`) as
`binding_id` in this skill's local `settings.json`; inbound Telegram text is
never treated as configuration. The adapter submits the actual provider
conversation facts to the loopback Host and durably polls any deferred work
before delivering its late text once.

Supported v1 Telegram content is text/caption, photos, documents, edited
messages, and message-reaction provider facts. Voice/audio bytes are still
outside the interpreted content contract: this transport makes no
speech-to-text claim. Reaction updates require the bot to be an administrator
in the chat and an explicit Bot API `allowed_updates` subscription; Telegram
does not deliver bot-authored reactions as user reactions.

## Delivery history

Hosts advertising `presence_delivery_version=1` receive delivery observations
through `/presence/delivery`. Each confirmed text chunk or media send freezes its
actual transmitted text/format, exact destination, provider receipt and producer
origin in the existing outbox before reporting. Captionless files retain their
filename/provider media descriptors. A partial failure preserves confirmed parts
and reports the remainder as failed or uncertain rather than delivered speech.

History acknowledgements have their own backoff beside the provider receipt.
The existing outbound worker owns a concurrent report attempt, so a slow or
failed history callback does not hold later sends or resend successful parts.
Restart reuses the same immutable report. Queueing alone creates no spoken row.
Automatic turns keep the Host's actual echoed reporting mode through deferred
work; legacy rows use mode 0 and are not retroactively imported. Old Hosts keep
normal sending with an explicit unsupported/unavailable reporting status in
transport status and delivery receipts. Origin metadata never grants authority.

Telegram acceptance without a stored receipt can still lead to a duplicate on
the existing provider retry path; reporting does not claim provider exactly-once.

## Incoming context

Each event preserves the current sender's provider identity, the chat title and
topic ID, and the message's text or caption. Text and caption entities retain
their original UTF-16 offsets, hidden link URLs, and text-mention user facts.
When Telegram supplies a replied-to message, the event includes its text or
caption, original author/chat/message IDs, date, entities, and photo/document
descriptors. Selected quotes and forwarding origins remain separate source
facts; a forwarded author does not replace the current sender.

Edits retain their original message id with `message.event_kind` set to
`edited_message`. Reaction updates retain the target message id and the old/new
reaction arrays, with `event_kind` set to `message_reaction` and no synthetic
message text. These are observations for model judgment, not automatic replies.

Reply context comes only from that incoming update. It does not fetch history,
follow nested reply chains, or download the replied-to message's media. Direct
photo/document attachments continue through the existing staging path.

## Configured room and selected actions

The optional `management_group_id` in the transport settings is a numeric group
ID, not a secret. Messages in that group carry
`conversation.configured_room="management_group"`; the reviewed behavior profile
interprets this room fact. It grants no owner commands or system-settings access,
and the adapter does not copy a member roster or classify message intent. For
all human messages to reach the bot, disable bot privacy mode in BotFather or
provide the bot with the appropriate group visibility.

The selected `telegram_moderate` tool queues deletion of an exact message,
restriction/ban of an exact member, or unban. The model supplies the decision and
provider IDs; Telegram enforces the bot's actual rights. Restriction permissions
are ordinary Telegram `ChatPermissions`, so restrictions can also be lifted.
Reuse `request_id` when retrying the same action, and inspect `telegram_receipt`
with `operation="moderate"` (or `"send"`) for the provider outcome. A queue receipt
is not proof of provider delivery.

`telegram_operation` extends the same outbox for the two provider operations
that update an own message: `editMessageText` and `setMessageReaction`. It
accepts Bot API-shaped parameters and retains a new operation identity plus an
optional `original_delivery_id` source reference, so an edit/reaction never
rewrites the original outbound delivery history. A reaction update contains
only Telegram's reaction objects; this skill does not invent a forum topic ID
when the provider's reaction update omits one. Supply a known local topic ID
only for `editMessageText` when the caller has it. Provider rights, reaction
administration requirements, and errors remain authoritative; a queued row is
not a delivery claim.

The outbox runs independently while a Presence turn is reasoning. The model may
call `telegram_send` for an intermediate acknowledgement, continue working, and
later return an answer or silence. There is no automatic acknowledgement policy.

Transport retries deduplicate known events and persisted receipts. A network
interruption after Telegram accepts a request but before the receipt is stored
can still cause a repeated send; no provider-side exactly-once guarantee is
claimed. Telegram's deletion window, bot rights, file limits and member-operation
rules remain provider constraints: https://core.telegram.org/bots/api.

## Message formatting

Text and media captions use the native Telegram skill's reviewed Markdown-to-HTML
presentation helpers included in this payload. Bold, links, lists, code and
bounded monospace tables render without exposing Markdown markers. Set
`markdown: false` on `telegram_send` when punctuation or XML must stay literal.
Automatic Presence replies use Markdown; for an explicitly literal reply, use
`telegram_send` with that flag and finish with `tool_delivered`.

Use `text` and `caption` only for the content people should see. Other arguments
are separate JSON fields, for example:

```json
{"chat_id":"-10042","kind":"document","file_path":"/selected/workspace/report.pdf","caption":"**Report** with [sources](https://example.org)","request_id":"report-v1","markdown":true}
```

The transport does not remove XML-looking text or infer tool arguments from
captions. A caption above 1024 visible UTF-16 units returns an explicit error:
shorten it and send the remainder separately. It is never silently truncated.

New text deliveries freeze their rendered, block-aware chunks in the durable
outbox. Retries preserve exact chunk boundaries, thread/reply IDs and confirmed
receipts. Existing queued rows keep literal presentation; an already partially
delivered legacy row also keeps its original chunk boundaries. Only an explicit
Telegram 400 rejection of an HTML send permits an immediate plain-text fallback;
a timeout or unknown network response never triggers that fallback. The ordinary
bounded outbox retry policy and its documented unknown-acceptance duplicate risk
remain unchanged. See `FORMATTER_PROVENANCE.md` for the included helper source.
