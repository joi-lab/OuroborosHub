---
name: slack-bridge
description: Slack presence transport with durable delivery, directory discovery, provider updates, file transfer, message actions, and provider context.
version: 1.4.2
type: extension
entry: plugin.py
plugin_api: "2.0"
runtime: python3
permissions: [net, read_settings, widget, route, tool, companion_process, presence]
env_from_settings: [SLACK_BOT_TOKEN, SLACK_APP_TOKEN]
dependencies: [httpx, websockets]
when_to_use: User wants Ouroboros to receive Slack messages or files, reply in Slack threads, or send proactive Slack text messages.
timeout_sec: 30
companion_processes:
  - name: slack_socket_mode
    command: [python3, scripts/slack_daemon.py]
    runtime: python3
    restart_policy: on_failure
    max_restarts: 10
tools:
  - name: slack_send
    description: Queue a proactive Slack message or threaded reply with explicit Markdown, Slack mrkdwn, or plain text format.
  - name: slack_user_info
    description: Read one user's available Slack profile facts by exact user ID.
  - name: slack_conversation_info
    description: Read one conversation's provider metadata by exact channel ID.
  - name: slack_history
    description: Read one explicit page of conversation history without trimming message text.
  - name: slack_thread
    description: Read one explicit page of a thread using its root timestamp.
  - name: slack_list_conversations
    description: List one paginated conversation directory page with exact IDs and URLs.
  - name: slack_list_users
    description: List one paginated user directory page with exact IDs and profile facts.
  - name: slack_lookup_user_email
    description: Resolve one email to an exact Slack user ID when the provider permits it.
  - name: slack_members
    description: List one paginated member-ID page for an exact conversation.
  - name: slack_join
    description: Queue an explicit join of one public conversation by exact ID and keep its provider receipt.
  - name: slack_resolve
    description: Return name/email/URL/ID directory candidates without guessing ambiguous matches.
  - name: slack_file_upload
    description: Stage immutable bytes and queue Slack External Upload API delivery.
  - name: slack_file_download
    description: Download one provider file into the skill state artifact directory.
  - name: slack_message_edit
    description: Queue an update of an own Slack message by exact channel and timestamp.
  - name: slack_message_delete
    description: Queue deletion of an own Slack message by exact channel and timestamp.
  - name: slack_reaction_add
    description: Queue adding a reaction to an exact Slack message.
  - name: slack_reaction_remove
    description: Queue removing a reaction from an exact Slack message.
  - name: slack_pin_add
    description: Queue pinning an exact Slack message.
  - name: slack_pin_remove
    description: Queue unpinning an exact Slack message.
  - name: slack_bookmark_add
    description: Queue adding a bookmark to an exact Slack conversation.
  - name: slack_bookmark_remove
    description: Queue removing a bookmark by exact provider bookmark ID.
  - name: slack_api
    description: Call an actual Slack Web API method with a model-selected read or durable write effect.
  - name: slack_receipt
    description: Inspect provider outcome, uncertainty and Host history-report status by request ID.
---

# Slack Bridge

Slack Bridge is a provider-neutral Slack transport. It receives Slack events
through Socket Mode, commits every acknowledged envelope to a local SQLite
queue, preserves exact Slack provenance, stages inbound private files with the
bot credential, and durably delivers text replies.

The skill does not decide who is an administrator, reinterpret slash commands,
or turn Slack messages into owner commands. It transports conversation events
and leaves identity, authority, memory, and turn policy to the host presence
runtime.

## Presence binding

Choose the owner-created exact or account-wide presence binding in this skill's settings.
Create it for provider `slack`, the workspace Team ID shown in the widget, and
an exact channel conversation ID or `*`. The bridge keeps that one 32-character lowercase
hexadecimal Binding ID and submits neutral provider events to the reviewed
loopback presence endpoint using the dedicated `presence` permission. The
host-injected Host Service token is held only as an opaque `SkillToken` and
revealed at each loopback request; it is never logged, persisted, or exposed by
the status route. Immediate text is queued once for Slack; deferred work keeps
its durable work reference and is polled until terminal.

## Slack app setup

1. Create a Slack app from `manifest.json` and enable Socket Mode.
2. Create an app-level token with `connections:write` and save it as
   `SLACK_APP_TOKEN`.
3. Install the app and save its bot token as `SLACK_BOT_TOKEN`.
4. Grant both settings to this reviewed skill, then enable it.
5. Save the owner-created Presence Binding ID in the skill settings.
6. Invite the bot to channels where it should participate.

Every DM, MPDM, public channel, and private channel event that the installed app
can receive is transported; the selected host binding decides admission. Invite the bot where Slack requires explicit
membership; there is no second bridge-local channel allowlist.

Inbound Slack files are downloaded from their authenticated `url_private`
locations into the skill state directory before the host adapter sees them.
`slack_file_download` exposes the same provider-authenticated path for a file
ID. Because those reads carry the bot credential, they are confined to Slack's
documented private-file host `files.slack.com`: another scheme, an embedded
userinfo component, a non-443 port, an IP literal or any other hostname is
refused before the request is built, and a redirect response is refused rather
than followed, so the credential is never replayed to another origin and a
login-page redirect is never staged as a file's bytes.

Outbound `slack_file_upload` copies immutable bytes into the skill state
directory at enqueue, then the companion runs Slack's current three-phase
External Upload API (`files.getUploadURLExternal`, raw bytes POST,
`files.completeUploadExternal`). A lost response after bytes or completion is
recorded as `uncertain`; the bridge never claims a provider-side exactly-once
mutation.

`slack_list_conversations` and `slack_list_users` remain one-page low-level tools:
follow `next_cursor` yourself and treat `complete=false` as incomplete.
`slack_resolve` handles directory pagination internally for names and returns
all matching candidates without choosing a person. Exact IDs, Slack mentions,
conversation/profile permalinks and user emails use direct provider lookups.
It never joins a channel or links identities as part of resolution.

Name scans share a credential-scoped directory cache in the existing bridge
database, reused across queries for up to five minutes. Results disclose
`observed_at`, `cache_hit`, `cache_age_sec`, `coverage`, `entries_scanned`,
`pages_fetched` and `complete`. `refresh=true` starts a new scan; omit `cursor`
then. Exact lookups always use the provider. A directory snapshot is not an
atomic Slack export and may change while being scanned. Private rooms that the
credential cannot see remain invisible; profile observations do not grant authority.

A scan fetches at most 50 pages and spends at most 45 seconds inside the
60-second tool call. `limit` is page size (default 200), not a result count.
Timeouts, missing/repeated cursors and rate limits return partial candidates,
`complete=false`, an error and the available `next_cursor`. Honor
`error.retry_after` before continuing with the same query/kind and cursor.
The cache retains traversed pages across continuation calls; an incomplete
empty result is never a cached proof that someone is absent. If no cursor is
available, retry with `refresh=true`. A caller-supplied cursor without a saved
prefix reports `coverage=from_supplied_cursor`, even at the provider's last page.
Very broad substring queries may still exceed the host's result-size limit;
refine the name or use an exact ID/email. The resolver does not silently prune
candidate matches to fit that limit.

`slack_lookup_user_email` uses Slack's exact `users.lookupByEmail` method.
`slack_members` lists member IDs,
while `slack_join` is an explicit provider mutation: it is queued in the same
durable mutation outbox as the other writes, returns a `request_id`, and its
provider result, terminal refusal (`already_in_channel`, `is_archived`, a
missing scope) or uncertainty is read with `slack_receipt`. It is never an
automatic fallback for a failed history read.

Message edits, deletes, reactions, pins, bookmarks and channel joins use the
durable mutation queue and retain the provider response or an explicit
failed/uncertain result. Every tool returns one JSON object encoded as text, so
a result is machine-readable exactly as documented here.
The app manifest must be reinstalled in a workspace after scope changes; an
edited public manifest does not grant scopes to an already-installed app.

## Provider context and on-demand reads

After the Socket envelope is durably accepted, the inbound worker looks up its
exact author with `users.info` and room with `conversations.info`. These two
bounded calls run concurrently outside the Socket acknowledgement handler. The
worker saves their results, sources and observation times in the existing inbox
before submitting to Host. A retry or restart reuses that event's snapshot rather
than silently substituting a later profile. A subsequent event gets a fresh
snapshot. Existing submitted rows retain their original Host reference.

The model sees available display/real names, Slack username, profile fields
(including email and title when returned), timezone, channel name/type/topic/
purpose, and the workspace name already supplied by `auth.test`. Exact actor,
workspace, channel and thread IDs remain unchanged. Missing scopes, rate limits
or failed lookups appear as explicit `unavailable` observations; the original
message still reaches Presence. Empty or absent fields are not invented. A
stored observation describes the recorded moment, not a claim that the profile
is still current. `slack_user_info` and `slack_conversation_info` can obtain fresh
provider data on demand. Provider profile facts do not link people, infer roles
or grant system ownership; those judgments stay with the model.

`slack_history` and `slack_thread` fetch one page per call and preserve full
provider message text, timestamps, author IDs and thread fields. They never
automatically retrieve a directory or bulk history, feed historical messages
into Presence, or change the transport's intake cursor. Use `slack_user_info` to
resolve an author ID. `slack_thread` needs the root message's timestamp and also
returns the root when Slack includes it. Follow `next_cursor` with the same
filters until `complete=true`; `has_more=true` without a cursor remains explicitly
incomplete and requires an explicit timestamp-range continuation. `oldest`,
`latest`, and `inclusive` expose Slack's normal time filters. Requested page size
is not a completeness guarantee; Slack's app classification may reduce it.

The app manifest declares `users:read` for profiles, `users:read.email` for email,
and `channels:read`, `groups:read`, `im:read`, `mpim:read` for room metadata.
History uses the corresponding existing `*:history` scopes. Existing apps need
the corresponding granted scopes; merely editing this file does not grant them.
All reads use the existing bot token. Method-specific bot-token restrictions,
conversation membership, scopes and rate limits remain provider facts: a refusal
returns its error code, HTTP status, required scopes when supplied, and retry
delay instead of pretending the result was empty. No automatic account login or
HTTP retry is introduced.

## Inbound provider updates

Message edits (`message_changed`) are normalized from Slack's nested
`message`/`previous_message` objects while both objects remain in provider
facts. When both snapshots identify the same message and contain matching
content, changes only to `language` or the `edited` marker do not start another
Presence turn. The complete envelope is still committed as `ignored` with
reason `message_content_unchanged` before acknowledgement. Every other field,
including unknown fields, remains in the comparison; missing comparison facts
do not suppress an update. Real edits retain their original message timestamp
and still reach the model. Slack documents automatic language detection as one
source of [`message_changed`](https://docs.slack.dev/reference/events/message/message_changed/).
This snapshot comparison does not recover an original message missed while
disconnected: an unchanged revision remains ignored even if it arrives first.
History reads remain explicit; the bridge does not backfill old messages.
Deletes preserve the deleted timestamp and previous message facts.
`reaction_added` and `reaction_removed` preserve the reacted message ID,
reaction name and actor. Blocks-only messages are accepted when `blocks` carry
content even if Slack's `text` field is empty. Other bot/app events remain
provider facts and can reach Presence; the bridge drops only its own bot/app
events using the authenticated identity, preserving self-deduplication and
avoiding reply loops. These updates use the existing ordered inbox, host
adapter, and LLM-selected silent/message outcomes; no keyword or semantic gate
is added by the transport.

## Generic Slack Web API access

`slack_api` is the narrow provider escape hatch for methods that do not yet
have a dedicated convenience tool. The `path` is one actual Slack Web API
method name (for example, `conversations.list` or `chat.postMessage`), and
the existing bot credential is supplied by the client; callers never provide
or persist a token. The model selects the provider effect separately from the
HTTP transport method: `effect="read"` executes a read immediately, while
`effect="write"` (the default) queues either GET or POST in the durable
mutation outbox. Slack documents some writes over GET, so the HTTP verb cannot
establish read-only behavior. Writes use a stable `request_id` when the
caller supplies one, provider receipts on completion, and an explicit
`uncertain` result when the response may have been lost after acceptance.
Use `slack_receipt` with that request ID to inspect each durable part's provider
result, failure or uncertainty and the separate Host history-report state.
For `chat.postMessage`, confirmed provider message/channel/timestamp facts
create a speech delivery report through the existing Host history path. For
other provider methods, select `result_kind="message"` only when the write
creates speech; the report still requires those actual provider facts.
`result_kind="operation"` retains the operation receipt without creating
new speech. The method/path validator
rejects full URLs, traversal and malformed method names while leaving the
provider's own scopes, method validation and errors authoritative.

Profile, conversation, history and thread reads use GET query parameters because
Slack's read methods do not reliably consume JSON POST arguments; message sends
continue to use POST JSON.

References: [conversations.list](https://docs.slack.dev/reference/methods/conversations.list/),
[users.list](https://docs.slack.dev/reference/methods/users.list/),
[users.lookupByEmail](https://docs.slack.dev/reference/methods/users.lookupByEmail/),
[conversations.members](https://docs.slack.dev/reference/methods/conversations.members/),
[conversations.join](https://docs.slack.dev/reference/methods/conversations.join/),
[files External Upload](https://docs.slack.dev/messaging/working-with-files/),
[users.info](https://docs.slack.dev/reference/methods/users.info/),
[conversations.info](https://docs.slack.dev/reference/methods/conversations.info/),
[conversations.history](https://docs.slack.dev/reference/methods/conversations.history/),
[conversations.replies](https://docs.slack.dev/reference/methods/conversations.replies/).

## Delivery behavior

- Socket envelopes are acknowledged only after their durable SQLite transaction
  commits.
- Slack retry envelopes and duplicate event IDs are deduplicated.
- Expired leases are reclaimed after a crash.
- Admission is ordered per Slack thread while independent threads may run
  concurrently. Deferred work retains its durable reference and polling, but
  allows later messages in the same thread after its initial acknowledgement.
- An outbound item becomes terminally failed after five delivery attempts; that
  failed item no longer blocks later messages in the same Slack thread.
- Long outbound text is split into Slack-safe chunks before it enters the
  durable outbox.
- The Widgets tab reports connection state, queue depth, failures, and recent
  activity without exposing tokens or message contents.

Save settings before enabling the skill, or toggle it after a settings change.
An absent settings file simply means "not configured yet". A settings file that
exists but cannot be read, is not JSON, or is not a JSON object is reported as
`binding_state: "unreadable"` with `local_settings_error` in the status route,
the companion refuses to start on it, and saving settings returns HTTP 409
without overwriting the bytes nobody could parse.

Delivery is durable and retries are bounded. A network interruption after Slack
accepts a send but before the receipt is stored can still cause a repeated send;
the transport does not claim provider-side exactly-once delivery.

## Message formatting

New automatic replies and `slack_send` calls default to `text_format="markdown"`.
Standard Markdown such as `**bold**`, `*italic*`, fenced code, lists and
`[label](https://example.org)` is sent through Slack's native `markdown_text`
field. The skill does not rewrite markup or maintain a Markdown parser.

Choose `text_format="mrkdwn"` for Slack-native `*bold*` and `<url|label>` syntax,
or `text_format="plain"` to show punctuation/markup literally. These modes use
the ordinary `text` field with `mrkdwn=true` or `false`; Markdown sends never
combine `markdown_text` with `text` or `blocks`. The outbox stores the selected
format with each chunk. Existing rows keep their original Slack-native mrkdwn
interpretation; retries retain the same text, chunks, target, thread and format.
An ambiguous provider error never triggers a second send in another format;
the existing bounded outbox retry policy remains unchanged.

The existing lossless 3,900-character chunker stays in use. Very long code fences
or other markup spanning a chunk boundary may render separately; keep formatted
sections within a chunk or use plain text when exact literal presentation matters.
No message characters are silently dropped.

Reference: [chat.postMessage formatting fields](https://docs.slack.dev/reference/methods/chat.postMessage/).

## Receipt-backed conversation history

The companion discovers `presence_delivery_version` from the authenticated
loopback `/identity` endpoint. On a supporting Host, new Presence submissions
opt into delivery reporting. The actual mode echoed by the original turn is
kept with both its immediate and deferred results; an old cached turn or old
automatic outbox row stays in legacy mode even after an upgrade.

Each new explicit send or mutation captures only compact origin references from its tool
context, never the complete task or credentials. New sends opt into reporting
when Host capability discovery has succeeded. Older Hosts continue sending;
`status` exposes `history_reporting_state` and `history_reporting_limitation`
when receipt-backed history is unavailable. No unknown turn fields are sent to
an older Host, and no old terminal outbox rows are retroactively imported.

After Slack confirms a physical message chunk, the outbox commits its actual
resolved channel, provider timestamp and immutable report payload before any
history callback. The existing outbound workers submit that report through
`/presence/delivery`. Report ACK, lease and bounded-backoff retries live beside
the existing provider receipt in the same outbox. Each existing outbound worker
keeps at most one tracked report task in flight while continuing provider sends;
stopping the worker cancels and awaits that task. A slow or failed report never
requeues the provider send or holds later sends waiting for a report ACK. A restart
or lost ACK retries exactly the same report; the Host owns idempotent history
acceptance. The status route exposes pending/acknowledged report counts and
the last report error separately from provider delivery state.

Reports identify tool versus automatic origin explicitly. Successful chunks
are `delivered`; definitive terminal Slack errors are `failed`, and terminal
network failures are `uncertain`, never a delivered full logical message. An
unresolved user target is retained as requested with `target_resolved=false`;
it does not claim a resolved DM channel. Queued messages are not spoken history.
The previous bounded provider retry policy is unchanged: an ambiguous provider
acceptance followed by retry can still duplicate a Slack message. Host report
deduplication is not a provider-side exactly-once delivery guarantee.
