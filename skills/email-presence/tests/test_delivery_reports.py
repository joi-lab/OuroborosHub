import asyncio
import copy
import json
import sqlite3
import smtplib
from contextlib import contextmanager
from email.parser import BytesParser
from email.policy import default

import httpx
import pytest

from lib.host_adapter import LoopbackPresenceHostAdapter, HostContractError, HostDelivery, HostTurnStatus
from lib.runtime import EmailRuntime
from lib.store import EmailStore, InboxItem
from test_transport import FakeClient, FakeIMAP


class Host:
    available = True
    delivery_reporting_status = "supported"

    def __init__(self):
        self.calls, self.records = [], {}
        self.lose_ack = False
        self.entered = self.release = None

    async def discover_delivery_support(self):
        return 1

    async def report_delivery(self, report):
        self.calls.append(copy.deepcopy(report))
        self.records.setdefault((report["delivery_id"], report["part_id"], report["state"]), report)
        if self.entered:
            self.entered.set()
            await self.release.wait()
        if self.lose_ack:
            self.lose_ack = False
            raise OSError("ACK lost after history acceptance")


def setup(tmp_path):
    store = EmailStore(tmp_path)
    client = FakeClient(FakeIMAP(1800000000))
    host = Host()
    runtime = EmailRuntime(store, client, host, account="bot@example.org")
    return store, client, host, runtime


def enqueue(store, request_id="first", recipients=None, mode=1):
    store.enqueue_outbox(request_id=request_id, recipients=recipients or ["reader@example.org"],
                         subject="Subject", body="Body", in_reply_to="<parent>", references=["<root>"],
                         reporting={"version": mode, "origin": {"kind": "tool", "task_id": "task1"}})


def ready_reports(store):
    with sqlite3.connect(store.path) as db:
        for row_id, raw in db.execute("SELECT id,reports_json FROM outbox").fetchall():
            reports = json.loads(raw)
            for entry in reports:
                entry["available_at"] = 1
            db.execute("UPDATE outbox SET reports_json=?,report_due_at=1 WHERE id=?", (json.dumps(reports), row_id))


def test_smtp_acceptance_report_ack_loss_restart_no_resend(tmp_path):
    async def run():
        store, client, host, runtime = setup(tmp_path)
        enqueue(store)
        assert store.next_delivery_report() is None
        assert runtime.process_outbox()
        assert store.receipt("first")["state"] == "completed"
        host.lose_ack = True
        await runtime.process_delivery_report()
        ready_reports(store)
        restarted = EmailRuntime(EmailStore(tmp_path), client, host)
        assert not restarted.process_outbox()
        await restarted.process_delivery_report()
        assert len(client.sent) == 1 and len(host.records) == 1
        assert host.calls[0] == host.calls[1]
        report = host.calls[0]
        assert len(report) == 12 and "binding_id" not in report
        assert report["state"] == "accepted" and report["text"] == client.sent[0].get_content()
        assert report["account_id"] == "bot@example.org"
        assert report["conversation_id"] == report["thread_id"] == "<root>"
        assert report["message"]["references"] == ["<root>", "<parent>"]
        assert report["message"]["message_id"] == client.sent[0]["Message-ID"]
        assert store.next_delivery_report() is None
    asyncio.run(run())


def test_smtp_partial_acceptance_is_not_full_delivery(tmp_path):
    async def run():
        store, client, host, runtime = setup(tmp_path)
        enqueue(store, recipients=["accepted@example.org", "refused@example.org"])
        client.send_message = lambda message: {"refused@example.org": (550, b"No mailbox")}
        runtime.process_outbox()
        assert store.receipt("first")["state"] == "uncertain"
        while await runtime.process_delivery_report():
            pass
        accepted = next(r for r in host.calls if r["state"] == "accepted")
        failed = next(r for r in host.calls if r["state"] == "failed")
        assert accepted["message"]["recipients"] == ["accepted@example.org"]
        assert failed["message"]["recipients"] == ["refused@example.org"]
        assert failed["message"]["refused_recipients"]["refused@example.org"]["code"] == 550
        assert failed["text"] == "" and not runtime.process_outbox()
    asyncio.run(run())


@pytest.mark.parametrize("partial", [True, False])
def test_named_smtp_envelope_reports_survive_ack_loss_and_restart(tmp_path, partial):
    class SMTP(smtplib.SMTP):
        def __init__(self):
            super().__init__(local_hostname="localhost")  # No connection or DNS lookup.
            self.calls = []

        def ehlo_or_helo_if_needed(self):
            pass

        def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
            self.calls.append((from_addr, list(to_addrs), msg))
            return {"refused@example.org": (550, b"No mailbox")} if partial else {}

    async def run():
        store, client, host, runtime = setup(tmp_path)
        names = ["Reader <accepted@example.org>", "Refused <refused@example.org>"]
        enqueue(store, recipients=names)
        smtp = SMTP()

        @contextmanager
        def connection():
            yield smtp

        client.smtp = connection
        assert runtime.process_outbox()
        assert len(smtp.calls) == 1
        envelope = smtp.calls[0][1]
        assert envelope == ["accepted@example.org", "refused@example.org"]
        sent = BytesParser(policy=default).parsebytes(smtp.calls[0][2])
        assert str(sent["To"]) == ", ".join(names)
        assert store.receipt("first")["state"] == ("uncertain" if partial else "completed")

        host.lose_ack = True
        assert await runtime.process_delivery_report()
        accepted = host.calls[0]
        assert accepted["state"] == "accepted"
        assert accepted["message"]["recipients"] == (envelope[:1] if partial else envelope)
        assert accepted["message"]["to"] == str(sent["To"])
        ready_reports(store)
        restarted = EmailRuntime(EmailStore(tmp_path), client, host)
        assert not restarted.process_outbox()
        while await restarted.process_delivery_report():
            pass
        assert host.calls[0] == host.calls[1]
        assert len(smtp.calls) == 1
        assert len(host.records) == (2 if partial else 1)
        if partial:
            failed = next(r for r in host.calls if r["state"] == "failed")
            assert failed["message"]["recipients"] == envelope[1:]
            assert set(accepted["message"]["recipients"]).isdisjoint(failed["message"]["recipients"])

    asyncio.run(run())


def test_ambiguous_smtp_and_crash_remain_uncertain_without_resend(tmp_path):
    async def run():
        store, client, host, runtime = setup(tmp_path)
        enqueue(store)
        client.fail_data = True
        runtime.process_outbox()
        await runtime.process_delivery_report()
        assert host.calls[0]["state"] == "uncertain" and host.calls[0]["text"] == ""
        assert not runtime.process_outbox() and len(client.sent) == 1
        enqueue(store, "crashed")
        item = store.claim_outbox(lease_seconds=-1)
        store.set_reporting(item, {**item.reporting, "account_id": "bot@example.org"})
        store.mark_sending(item)
        assert EmailStore(tmp_path).claim_outbox() is None
        await runtime.process_delivery_report()
        assert host.calls[-1]["delivery_id"] == "crashed" and host.calls[-1]["state"] == "uncertain"
    asyncio.run(run())


def test_blocked_report_does_not_block_single_smtp_worker(tmp_path):
    async def run():
        store, client, host, runtime = setup(tmp_path)
        enqueue(store)
        runtime.process_outbox()
        host.entered, host.release = asyncio.Event(), asyncio.Event()
        stop, sent = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        original = client.send_message
        def send(message):
            result = original(message)
            loop.call_soon_threadsafe(sent.set)
            return result
        client.send_message = send
        runtime.poll = lambda: 0
        task = asyncio.create_task(runtime.run(stop))
        try:
            await asyncio.wait_for(host.entered.wait(), 1)
            enqueue(store, "second")
            await asyncio.wait_for(sent.wait(), 2)
            assert len(client.sent) == 2 and not host.release.is_set()
        finally:
            stop.set()
            host.release.set()
            await asyncio.wait_for(task, 2)
    asyncio.run(run())


@pytest.mark.parametrize("advertised, echoed, deferred", [(0, 0, False), (1, 1, False), (1, 0, False), (1, 1, True), (1, 0, True)])
def test_host_negotiation_freezes_actual_result_mode(monkeypatch, advertised, echoed, deferred):
    async def run():
        monkeypatch.setenv("EMAIL_USER", "bot@example.org")
        def handler(request):
            if request.url.path == "/identity":
                return httpx.Response(200, json={"ok": True, "presence_delivery_version": advertised})
            if request.method == "POST":
                payload = json.loads(request.content)
                assert (payload.get("delivery_reporting_version") == 1) is bool(advertised)
                return httpx.Response(200, json={"status": "completed", "outcome": "deferred" if deferred else "message", "text": "Early" if deferred else "Final", "work_ref": "work1" if deferred else "", "turn_ref": "turn1", "delivery_reporting_version": echoed})
            return httpx.Response(200, json={"status": "completed", "outcome": "message", "text": "Final", "delivery_reporting_version": 1})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            host = LoopbackPresenceHostAdapter("a" * 32, host_service_url="http://127.0.0.1:8767", skill_token="test", http_client=http)
            item = InboxItem(1, "lease", "INBOX", 4, "<m>", "", (), "sender@example.org", "S", "B", (), "", 1)
            ref = await host.submit(item)
            assert (await host.status(ref)).delivery_reporting_version == echoed
            assert (await host.deliver(ref)).delivery_reporting_version == echoed
    asyncio.run(run())


def test_legacy_rows_and_old_host_send_without_history_claim(tmp_path):
    store, client, host, runtime = setup(tmp_path)
    store.enqueue_outbox(request_id="old", recipients=["r@example.org"], subject="S", body="B")
    store.put("delivery_support", {"version": 0, "status": "unsupported"})
    enqueue(store, "new-old-host", mode=-1)
    runtime.process_outbox()
    runtime.process_outbox()
    assert len(client.sent) == 2 and store.next_delivery_report() is None
    assert store.receipt("new-old-host")["delivery_reporting"]["status"] == "unsupported"


@pytest.mark.parametrize("mode", [0, 1])
def test_automatic_outbox_origin_and_mode_follow_original_turn(tmp_path, mode):
    class AutomaticHost(Host):
        async def submit(self, item):
            return "saved"
        async def status(self, reference):
            return HostTurnStatus("ready", texts=("Early",), delivery_reporting_version=mode, turn_ref="turn1")
        async def deliver(self, reference):
            return HostDelivery(("Final",), mode, "turn1")
    store, client, _, _ = setup(tmp_path)
    store.ingest({"uid": 4, "message_id": "<incoming>", "sender": "reader@example.org"})
    runtime = EmailRuntime(store, client, AutomaticHost(), account="bot@example.org")
    asyncio.run(runtime.process_inbox())
    for text in ("Early", "Final"):
        item = EmailStore(tmp_path).claim_outbox()
        assert item.body == text and item.reporting["version"] == mode
        assert item.reporting["origin"] == {"kind": "automatic", "task_id": "turn1", "source_event_id": "INBOX:0:4:<incoming>"}
        store.complete_outbox(item.row_id, item.lease_token)


def test_report_requires_recorded_ack():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True, "recorded": False}))) as http:
            host = LoopbackPresenceHostAdapter("", host_service_url="http://127.0.0.1:8767", skill_token="test", http_client=http)
            with pytest.raises(HostContractError, match="acknowledged"):
                await host.report_delivery({})
    asyncio.run(run())


def test_migration_preserves_old_receipts_without_importing_speech(tmp_path):
    store, client, _, runtime = setup(tmp_path)
    store.enqueue_outbox(request_id="old", recipients=["reader@example.org"], subject="S", body="B")
    runtime.process_outbox()
    message_id = store.receipt("old")["provider_message_id"]
    with sqlite3.connect(store.path) as db:
        db.execute("DROP INDEX outbox_report_due")
        for column in ("reporting_json", "reports_json", "report_due_at"):
            db.execute(f"ALTER TABLE outbox DROP COLUMN {column}")
    migrated = EmailStore(tmp_path)
    assert migrated.receipt("old")["state"] == "completed"
    assert migrated.receipt("old")["provider_message_id"] == message_id
    assert migrated.next_delivery_report() is None
    assert migrated.claim_outbox() is None and len(client.sent) == 1
