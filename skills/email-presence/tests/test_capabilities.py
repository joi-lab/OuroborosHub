import os
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default

from lib.client import MailClient
from lib.host_adapter import email_presence_event
from lib.mime import build_message, parse_message, reply_all_recipients
from lib.store import EmailStore
from lib.runtime import EmailRuntime


def test_mime_cc_bcc_html_alternative_and_attachment_roundtrip():
    message = build_message(
        sender="bot@example.org",
        recipients=["to@example.org"],
        cc=["copy@example.org"],
        bcc=["blind@example.org"],
        subject="Rich",
        body="Plain fallback",
        html_body="<p>Rich <b>body</b></p>",
        in_reply_to="<parent@example.org>",
        references=["<root@example.org>"],
        attachments=[{"filename": "note.txt", "content_type": "text/plain", "data": b"hello"}],
    )
    assert str(message["To"]) == "to@example.org"
    assert str(message["Cc"]) == "copy@example.org"
    assert message["Bcc"] is None
    raw = message.as_bytes()
    parsed = BytesParser(policy=default).parsebytes(raw)
    assert parsed.is_multipart()
    assert parsed["In-Reply-To"] == "<parent@example.org>"
    assert "<root@example.org>" in parsed["References"]
    parts = [part for part in parsed.walk() if part.get_filename()]
    assert parts[0].get_payload(decode=True) == b"hello"
    inbound = parse_message(raw, folder="INBOX", uid=4, include_attachment_data=True)
    assert inbound["body"] == "Plain fallback"
    assert inbound["attachments"][0]["data"] == b"hello"


def test_reply_all_excludes_mailbox_and_deduplicates_across_to_and_cc():
    result = reply_all_recipients({
        "sender": "sender@example.org",
        "reply_to": ["reply@example.org"],
        "recipients": ["bot@example.org", "reply@example.org", "to@example.org"],
        "cc": ["to@example.org", "copy@example.org"],
    }, "bot@example.org")
    assert result == {"to": ["reply@example.org"], "cc": ["to@example.org", "copy@example.org"]}


def test_attachment_staging_is_atomic_immutable_and_survives_outbox_claim(tmp_path):
    source = tmp_path / "source.txt"
    source.write_bytes(b"before")
    store = EmailStore(tmp_path / "state")
    staged = store.stage_outbound_attachments("request-1", [{"path": str(source), "filename": "report.txt"}])
    path = tmp_path / "state" / "staged" / "outbound"
    staged_path = staged[0]["path"]
    assert staged_path.startswith(str(path))
    assert os.stat(staged_path).st_mode & 0o222 == 0
    source.write_bytes(b"after")
    assert open(staged_path, "rb").read() == b"before"
    store.enqueue_outbox(request_id="request-1", to=["to@example.org"], cc=["copy@example.org"],
                         bcc=["blind@example.org"], subject="S", body="B", html_body="<b>B</b>",
                         attachments=staged)
    item = store.claim_outbox()
    message = MailClient({"EMAIL_USER": "bot@example.org"}).message(item)
    assert message["To"] == "to@example.org"
    assert message["Cc"] == "copy@example.org"
    assert message["Bcc"] is None
    assert [part.get_filename() for part in message.walk() if part.get_filename()] == ["report.txt"]
    assert message.get_payload()[1].get_payload(decode=True) == b"before"


def test_inbound_staged_files_are_persisted_and_sent_to_host_separately(tmp_path):
    source = EmailMessage()
    source["From"] = "sender@example.org"
    source["To"] = "bot@example.org"
    source["Message-ID"] = "<incoming@example.org>"
    source.set_content("See attached")
    source.add_attachment(b"payload", maintype="application", subtype="octet-stream", filename="blob.bin")
    message = parse_message(source.as_bytes(), folder="INBOX", uid=9, include_attachment_data=True)
    store = EmailStore(tmp_path)
    staged = store.stage_inbound_attachments(message)
    message["uidvalidity"] = 3
    store.ingest(message)
    item = store.claim_inbox()
    assert item.staged_files[0]["path"] == staged[0]["path"]
    event = email_presence_event(item, account_id="bot@example.org")
    assert event["message"]["attachments"][0]["filename"] == "blob.bin"
    assert event["message"]["attachments"][0]["size"] == 7
    assert "data" not in str(event)


def test_oversized_inbound_attachment_is_described_without_wedging_cursor(tmp_path):
    class Store:
        def _stage_files(self, attachments, **_kwargs):
            item = attachments[0]
            if item.get("too_large"):
                raise ValueError("attachment batch exceeds 50 MiB")
            return [{"filename": item["filename"], "path": str(tmp_path / item["filename"])}]

    runtime = EmailRuntime(Store(), object(), object())
    message = {"message_id": "<large@example.org>", "attachments": [
        {"filename": "large.bin", "too_large": True, "content_type": "application/octet-stream"},
        {"filename": "small.txt", "content_type": "text/plain"},
    ]}
    runtime._stage_inbound_best_effort(message)
    assert message["staged_files"][0]["filename"] == "small.txt"
    assert message["attachments"][0]["filename"] == "small.txt"
    assert message["attachments"][1]["filename"] == "large.bin"
    assert message["attachments"][1]["content_available"] is False
    assert message["attachment_stage_note"]
