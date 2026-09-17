---
name: email-presence
description: Bidirectional email Presence transport with IMAP polling, durable delivery, and RFC 5322 reply threading.
version: 0.1.0
type: extension
entry: plugin.py
plugin_api: "2.0"
runtime: python3
permissions: [net, read_settings, route, tool, widget, companion_process, presence]
env_from_settings: [EMAIL_IMAP_HOST, EMAIL_IMAP_PORT, EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, EMAIL_USER, EMAIL_PASSWORD, EMAIL_DEFAULT_FOLDER, EMAIL_AUTH_MODE, EMAIL_OAUTH_ACCESS_TOKEN, EMAIL_OAUTH_REFRESH_TOKEN, EMAIL_OAUTH_CLIENT_ID, EMAIL_OAUTH_CLIENT_SECRET]
dependencies: [httpx, starlette]
when_to_use: User wants Ouroboros to receive and reply to email through a Presence binding, or send a proactive email.
timeout_sec: 120
companion_processes:
  - name: email_poller
    command: [python3, scripts/email_daemon.py]
    runtime: python3
    restart_policy: on_failure
    max_restarts: 10
tools:
  - name: email_send
    description: Queue a proactive email or threaded reply for durable delivery.
  - name: email_search
    description: Search old and new mailbox messages using IMAP criteria.
  - name: email_read
    description: Read a message by stable UID and UIDVALIDITY.
  - name: email_mailbox
    description: List/create folders, copy/move messages and update flags.
  - name: email_draft
    description: Save a text draft without sending.
  - name: email_receipt
    description: Read durable delivery state and Message-ID.
  - name: email_test_connection
    description: Check TLS and authentication without sending mail.
---

# Email Presence

This transport connects one IMAP mailbox and SMTP sender to an owner-created
Presence binding. Incoming messages are persisted before they are submitted to
the loopback Presence endpoint. Replies use `Message-ID`, `In-Reply-To`, and
`References` so normal mail clients keep one conversation thread.

The transport carries provider facts only. Identity, memory, behavior, and
capabilities are selected by the host Presence binding. The companion keeps its
SQLite inbox/outbox and cursor in the skill state directory, survives restarts,
and retries transient provider or Host failures with bounded backoff.

## Setup

1. Configure and grant `EMAIL_IMAP_HOST`, `EMAIL_SMTP_HOST`, and `EMAIL_USER`.
   Ports default to IMAPS 993 and SMTP 587 (STARTTLS); SMTP 465 uses TLS directly.
2. Use `EMAIL_AUTH_MODE=password` (default) and `EMAIL_PASSWORD` for an app
   password, or `EMAIL_AUTH_MODE=oauth2` with an existing OAuth access token in
   `EMAIL_OAUTH_ACCESS_TOKEN`. For persistent corporate Gmail access, provide
   existing `EMAIL_OAUTH_CLIENT_ID`, `EMAIL_OAUTH_CLIENT_SECRET` and
   `EMAIL_OAUTH_REFRESH_TOKEN`; access tokens refresh automatically. Gmail uses
   `imap.gmail.com`, `smtp.gmail.com`, and the `https://mail.google.com/` scope.
   The skill does not start login, create accounts or configure domain delegation.
   A service-account JSON by itself is not a mailbox credential for this skill.
3. Create a Presence binding for provider `email`, account `EMAIL_USER`, and a
   selected RFC Message-ID conversation root or `*`; save its binding ID in the
   skill settings. Enable the skill after grants and review.
4. The first successful connection records UIDNEXT as the activation baseline;
   only subsequent arrivals enter Presence. Existing mail remains fully reachable
   through `email_search` and `email_read`. Disabling/re-enabling preserves the
   baseline, so new mail received while offline is processed when it returns.

## Mailbox tools and transport behavior

- `email_search` accepts ordinary IMAP search tokens (ALL, UNSEEN, FROM, SUBJECT,
  SINCE, HEADER) and returns UID/UIDVALIDITY, which `email_read` uses to address
  the same message even if other messages disappear. Mailbox tools do not submit
  historical mail to Presence or alter the automatic intake cursor.
- `email_mailbox` lists/creates folders, copies/moves individual messages and
  adds/removes flags. Archive by moving to the desired folder. UID MOVE needs
  server support; the skill never substitutes a mailbox-wide expunge.
- `email_draft` saves a text draft in a selected mailbox folder without sending.
- `email_send` queues text and supports reply headers. Supply a stable request_id
  when retrying the same logical send; `email_receipt` reports its actual state.
- IMAP intake persists messages before cursor advancement. UIDVALIDITY changes
  rescan from the original activation date and deduplicate by Message-ID.
- The companion backs off from 5 seconds to the configured idle interval.
  Polling, host turns and outbound delivery run independently, so a model can
  send an early message while continuing its work. There is no automatic reply
  text, acknowledgement timer, second scheduler or separate memory engine.
- Normal retries reuse the stored Message-ID. SMTP cannot guarantee exactly-once
  delivery if a connection/process dies after the server accepted DATA but before
  the receipt is stored. Such sends are `uncertain`, never automatically resent.
  Inspect the receipt and sent mailbox before making an explicit new send.
- The `status` extension route exposes poll health, activation/cursor state, queue
  counts and recent receipts. Host Skills/Activity shows companion process health.

Text and reply threading are the transport scope. MIME text extraction supports
HTML-only mail as plain text, with explicit body truncation disclosure. Attachment
transfer, rendered HTML and provider-specific advanced search are deferred.
The separate public `email` utility remains available; this package neither
replaces nor removes its tools.

References: [Gmail XOAUTH2](https://developers.google.com/workspace/gmail/imap/xoauth2-protocol),
[Google refresh tokens](https://developers.google.com/identity/protocols/oauth2/web-server#offline).
