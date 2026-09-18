"""Host-supervised transport workers; all decisions remain with Presence."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import smtplib
import time

from .host_adapter import HostBindingTerminalError

log = logging.getLogger("email_presence")


class EmailRuntime:
    def __init__(self, store, client, host, *, folder="INBOX", account="", poll_interval=30):
        self.store, self.client, self.host = store, client, host
        self.folder, self.account = folder, account
        self.poll_interval = max(5, min(3600, int(poll_interval)))

    def poll(self):
        with self.client.imap(self.folder) as box:
            validity, next_uid = self.client.metadata(box)
            cursor = self.store.prepare_cursor(self.account, self.folder, validity, next_uid)
            criteria = ["UID", f"{cursor['uid'] + 1}:*"]
            if cursor["uid"] == 0:
                # UIDVALIDITY reset: rescan since activation, dedupe Message-ID.
                criteria += ["SINCE", datetime.fromtimestamp(cursor["activated_at"], timezone.utc).strftime("%d-%b-%Y")]
            count = 0
            for uid in self.client.uids(box, criteria):
                # IMAP n:* includes the last UID even when n is beyond it.
                if uid <= cursor["uid"]:
                    continue
                message = self.client.fetch(box, self.folder, uid, validity)
                cursor["uid"] = uid
                key = self.store.cursor_key(self.account, self.folder)
                if not cursor.get("rescan") or message["internal_date"] >= cursor["activated_at"]:
                    _, inserted = self.store.ingest(message, cursor_key=key, cursor=cursor)
                    count += int(inserted)
                else:
                    self.store.put(key, cursor)
            if cursor.pop("rescan", False):
                self.store.put(self.store.cursor_key(self.account, self.folder), cursor)
            self.store.put("poll_health", {"ok": True, "checked_at": time.time(), "new_messages": count})
            return count

    def _reply(self, item, texts, suffix):
        text = "\n".join(texts).strip()
        if text:
            subject = item.subject if item.subject.lower().startswith("re:") else f"Re: {item.subject}"
            self.store.enqueue_outbox(request_id=f"presence:{item.provider_event_key}:{suffix}",
                                      recipients=item.context.get("reply_to") or [item.sender], subject=subject, body=text,
                                      in_reply_to=item.message_id, references=item.references)

    async def process_inbox(self):
        item = self.store.claim_inbox()
        if not item:
            return False
        try:
            if not self.host.available:
                self.store.retry_inbox(item.row_id, item.lease_token, "Presence binding is not configured", 30)
                return True
            ref = item.host_reference or await self.host.submit(item)
            if not item.host_reference:
                self.store.set_host_reference(item.row_id, item.lease_token, ref)
            status = await self.host.status(ref)
            self._reply(item, status.texts, "immediate")
            if status.state == "pending":
                self.store.retry_inbox(item.row_id, item.lease_token, "Host turn pending", 5)
            elif status.state == "failed":
                self.store.fail_inbox(item.row_id, item.lease_token, status.error)
            else:
                delivery = await self.host.deliver(ref)
                self._reply(item, delivery.texts, "final")
                self.store.complete_inbox(item.row_id, item.lease_token)
        except HostBindingTerminalError as exc:
            self.store.fail_inbox(item.row_id, item.lease_token, str(exc))
        except Exception as exc:
            self.store.retry_inbox(item.row_id, item.lease_token, type(exc).__name__, min(60, 2 ** min(item.attempts, 5)))
        return True

    def process_outbox(self):
        item = self.store.claim_outbox()
        if not item:
            return False
        sending = False
        try:
            message = self.client.message(item)
            with self.client.smtp() as smtp:
                if not self.store.mark_sending(item):
                    return True
                sending = True
                refused = smtp.send_message(message)
                if refused:
                    self.store.uncertain_outbox(item.row_id, item.lease_token,
                                               "Some recipients were refused; accepted recipients must not be resent automatically")
                else:
                    self.store.complete_outbox(item.row_id, item.lease_token)
        except (smtplib.SMTPDataError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            # Typed negative SMTP replies prove this transaction was not accepted.
            code = getattr(exc, "smtp_code", 550)
            if isinstance(exc, smtplib.SMTPRecipientsRefused):
                codes = [value[0] for value in exc.recipients.values()]
                code = 450 if codes and all(400 <= c < 500 for c in codes) else 550
            if code < 500 and item.attempts < 5:
                self.store.retry_outbox(item.row_id, item.lease_token, type(exc).__name__, 5)
            else:
                self.store.fail_outbox(item.row_id, item.lease_token, type(exc).__name__)
        except Exception as exc:
            if sending:
                self.store.uncertain_outbox(item.row_id, item.lease_token,
                                           f"{type(exc).__name__} during SMTP delivery; inspect Message-ID before retry")
            elif item.attempts >= 5:
                self.store.fail_outbox(item.row_id, item.lease_token, type(exc).__name__)
            else:
                self.store.retry_outbox(item.row_id, item.lease_token, type(exc).__name__, min(60, 2 ** item.attempts))
        return True

    async def run(self, stop):
        async def wait(seconds):
            try:
                await asyncio.wait_for(stop.wait(), seconds)
            except asyncio.TimeoutError:
                pass

        async def poller():
            interval = self.poll_interval
            while not stop.is_set():
                try:
                    count = await asyncio.to_thread(self.poll)
                    interval = 5 if count else min(self.poll_interval, interval * 2)
                except Exception as exc:
                    self.store.put("poll_health", {"ok": False, "checked_at": time.time(), "error": type(exc).__name__})
                    log.warning("IMAP poll failed: %s", type(exc).__name__)
                    interval = self.poll_interval
                await wait(interval)

        async def inbox():
            while not stop.is_set():
                if not await self.process_inbox():
                    await wait(1)

        async def outbox():
            # Independent from a long Presence call: early tool sends can leave now.
            while not stop.is_set():
                if not await asyncio.to_thread(self.process_outbox):
                    await wait(1)

        await asyncio.gather(poller(), inbox(), outbox())
