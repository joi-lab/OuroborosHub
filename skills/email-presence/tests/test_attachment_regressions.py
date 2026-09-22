"""Regression coverage for attachment custody and SMTP envelope semantics."""
import importlib.util
import sys
import types
from contextlib import contextmanager
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

from lib.client import MailClient
from lib.mime import parse_message
from lib.runtime import EmailRuntime
from lib.store import EmailStore
from test_transport import FakeHost, FakeIMAP


class RawIMAP(FakeIMAP):
    def add_raw(self, uid, raw, arrived=None):
        self.messages[uid] = (raw, self.now if arrived is None else arrived)
        self.next_uid = max(self.next_uid, uid + 1)


def _raw_mail(message_id, *, attachments=(), html=False):
    message = EmailMessage()
    message["From"] = "sender@example.org"
    message["To"] = "bot@example.org"
    message["Subject"] = "Attachment regression"
    message["Message-ID"] = message_id
    if html:
        message.set_content("plain fallback")
        message.add_alternative("<p>rich <b>body</b></p>", subtype="html")
    else:
        message.set_content("See the attached files")
    for filename, content_type, payload in attachments:
        maintype, subtype = content_type.split("/", 1)
        message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes()


def _source_path(records):
    for record in records:
        for key in ("source", "raw_source", "path"):
            value = record.get(key) if isinstance(record, dict) else None
            if value and Path(value).is_file():
                return Path(value)
    return None


def _raw_source(records):
    for record in records:
        if isinstance(record, dict):
            value = record.get("source") or record.get("raw_source")
            if value and Path(value).is_file():
                return Path(value)
    return None


def test_poll_keeps_following_uid_when_attachment_count_exceeds_limit(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    box = RawIMAP(clock[0])
    client = MailClient({"EMAIL_USER": "bot@example.org"})
    client.imap = lambda folder=None, readonly=True: _imap_context(box)
    store = EmailStore(tmp_path)
    runtime = EmailRuntime(store, client, FakeHost(), account="bot@example.org")

    box.add_raw(1, _raw_mail("<baseline@example.org>"))
    assert runtime.poll() == 0
    eleven = [(f"file-{index}.txt", "text/plain", b"x") for index in range(11)]
    raw_too_many = _raw_mail("<too-many@example.org>", attachments=eleven)
    box.add_raw(2, raw_too_many)
    box.add_raw(3, _raw_mail("<following@example.org>"))

    # Staging is best-effort for a bounded attachment batch. An over-limit
    # message remains represented, and the cursor still reaches the next UID.
    assert runtime.poll() == 2
    first = store.claim_inbox()
    assert first.message_id == "<too-many@example.org>"
    assert first.context["attachments"]
    source = _raw_source(first.staged_files) or _raw_source(first.context["attachments"])
    assert source is not None and source.read_bytes() == raw_too_many
    store.complete_inbox(first.row_id, first.lease_token)
    following = store.claim_inbox()
    assert following.message_id == "<following@example.org>"


def test_poll_preserves_raw_source_for_over_50mib_attachment(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    box = RawIMAP(clock[0])
    client = MailClient({"EMAIL_USER": "bot@example.org"})
    client.imap = lambda folder=None, readonly=True: _imap_context(box)
    store = EmailStore(tmp_path)
    runtime = EmailRuntime(store, client, FakeHost(), account="bot@example.org")
    box.add_raw(1, _raw_mail("<baseline@example.org>"))
    assert runtime.poll() == 0
    payload = b"z" * (50 * 1024 * 1024 + 1)
    raw = _raw_mail("<large@example.org>", attachments=(("large.bin", "application/octet-stream", payload),))
    box.add_raw(2, raw)
    box.add_raw(3, _raw_mail("<after-large@example.org>"))

    assert runtime.poll() == 2
    item = store.claim_inbox()
    assert item.message_id == "<large@example.org>"
    source = _raw_source(item.staged_files) or _raw_source(item.context["attachments"])
    assert source is not None and source.read_bytes() == raw
    store.complete_inbox(item.row_id, item.lease_token)
    assert store.claim_inbox().message_id == "<after-large@example.org>"


def test_unsupported_message_rfc822_has_descriptor_error_and_raw_source(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    box = RawIMAP(clock[0])
    client = MailClient({"EMAIL_USER": "bot@example.org"})
    client.imap = lambda folder=None, readonly=True: _imap_context(box)
    store = EmailStore(tmp_path)
    runtime = EmailRuntime(store, client, FakeHost(), account="bot@example.org")
    box.add_raw(1, _raw_mail("<baseline@example.org>"))
    assert runtime.poll() == 0
    nested = EmailMessage()
    nested["From"] = "nested@example.org"
    nested["Subject"] = "Forwarded"
    nested.set_content("nested content")
    message = EmailMessage()
    message["From"] = "sender@example.org"
    message["To"] = "bot@example.org"
    message["Message-ID"] = "<rfc822@example.org>"
    message.set_content("Forwarded message")
    message.add_attachment(nested.as_bytes(), maintype="message", subtype="rfc822", filename="forwarded.eml")
    raw = message.as_bytes()
    metadata = parse_message(raw, folder="INBOX", uid=2)
    descriptor = next(entry for entry in metadata["attachments"] if entry.get("mime_type") == "message/rfc822")
    assert descriptor["content_available"] is False
    assert "data" not in descriptor
    box.add_raw(2, raw)

    assert runtime.poll() == 1
    item = store.claim_inbox()
    descriptor = next(entry for entry in item.context["attachments"] if entry.get("mime_type") == "message/rfc822")
    assert descriptor["content_available"] is False
    assert descriptor.get("error")
    source = descriptor.get("source") or _raw_source(item.staged_files)
    assert source and Path(source).read_bytes() == raw


def test_email_read_stages_the_same_attachment_artifact(tmp_path, monkeypatch):
    raw = _raw_mail("<read@example.org>", attachments=(("read.txt", "text/plain", b"read me"),))
    message = {
        "folder": "INBOX", "uid": 4, "uidvalidity": 7, "message_id": "<read@example.org>",
        "sender": "sender@example.org", "subject": "Read", "body": "read",
        "attachments": [{"filename": "read.txt", "content_type": "text/plain", "data": b"read me"}],
        "_raw_source": raw,
    }
    root = Path(__file__).resolve().parents[1]
    package = types.ModuleType("email_read_regression")
    package.__path__ = [str(root)]
    sys.modules[package.__name__] = package
    spec = importlib.util.spec_from_file_location(package.__name__ + ".plugin", root / "plugin.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)

    class ReadClient:
        def __init__(self, _settings):
            self.settings = {}
        def read(self, **_kwargs):
            return dict(message)

    monkeypatch.setattr(plugin, "MailClient", ReadClient)
    api = _PluginAPI(tmp_path)
    plugin.register(api)
    result = api.tools["email_read"](uid=4, uidvalidity=7)
    staged = result.get("staged_files") or result.get("attachments")
    source = _source_path(staged)
    assert source is not None and source.read_bytes() in {b"read me", raw}


class RecordingSMTPClient(MailClient):
    def __init__(self, state):
        super().__init__({"EMAIL_USER": "bot@example.org"})
        self.state = state
        self.sent = []
        self.refused = {}
        self.fail_connect = False

    @contextmanager
    def smtp(self):
        if self.fail_connect:
            raise ConnectionError("before SMTP DATA")
        yield self

    def send_message(self, message, *, to_addrs=None):
        self.sent.append((message, list(to_addrs or [])))
        return self.refused


def test_html_attachment_outbox_preserves_envelope_partial_and_restart(tmp_path):
    state = tmp_path / "state"
    store = EmailStore(state)
    source = tmp_path / "report.txt"
    source.write_bytes(b"report")
    staged = store.stage_outbound_attachments("rich", [{"path": str(source), "filename": "report.txt"}])
    store.enqueue_outbox(request_id="rich", to=["to@example.org"], cc=["cc@example.org"], bcc=["blind@example.org"],
                         subject="Rich", body="fallback", html_body="<p>rich</p>", attachments=staged)
    client = RecordingSMTPClient(state)
    client.refused = {"blind@example.org": (550, b"No mailbox")}
    runtime = EmailRuntime(store, client, FakeHost())

    assert runtime.process_outbox()
    receipt = store.receipt("rich")
    assert receipt["state"] == "uncertain"
    assert receipt["accepted_recipients"] == ["to@example.org", "cc@example.org"]
    assert "blind@example.org" in receipt["refused_recipients"]
    message, envelope = client.sent[0]
    assert envelope == ["to@example.org", "cc@example.org", "blind@example.org"]
    assert message["To"] == "to@example.org"
    assert message["Cc"] == "cc@example.org"
    assert message["Bcc"] is None
    assert message.is_multipart()
    assert any(part.get_content_type() == "text/html" for part in message.walk())
    assert any(part.get_filename() == "report.txt" for part in message.walk())

    restarted = EmailRuntime(EmailStore(state), client, FakeHost())
    assert restarted.process_outbox() is False
    assert len(client.sent) == 1


def test_pre_send_failure_retries_then_succeeds_without_new_message_id(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    store = EmailStore(tmp_path)
    store.enqueue_outbox(request_id="retry", to=["to@example.org"], subject="Retry", body="body")
    client = RecordingSMTPClient(tmp_path)
    client.fail_connect = True
    runtime = EmailRuntime(store, client, FakeHost())
    message_id = store.receipt("retry")["provider_message_id"]
    assert runtime.process_outbox()
    assert store.receipt("retry")["state"] == "retry"
    client.fail_connect = False
    clock[0] += 10
    assert runtime.process_outbox()
    assert store.receipt("retry")["state"] == "completed"
    assert client.sent[0][0]["Message-ID"] == message_id
    assert len(client.sent) == 1


def test_draft_retains_bcc_for_manual_send(tmp_path):
    class DraftClient(MailClient):
        def __init__(self):
            super().__init__({"EMAIL_USER": "bot@example.org"})
            self.appended = []

        @contextmanager
        def imap(self, folder=None, readonly=True):
            class Box:
                def __init__(self, outer):
                    self.outer = outer
                def append(self, *args):
                    self.outer.appended.append(args)
                    return "OK", [b"APPENDUID 7 20"]
            yield Box(self)

    client = DraftClient()
    receipt = client.draft(to="to@example.org", cc=["cc@example.org"], bcc=["blind@example.org"],
                           subject="Draft", body="body")
    assert receipt["ok"]
    message = BytesParser(policy=default).parsebytes(client.appended[0][3])
    assert message["To"] == "to@example.org"
    assert message["Cc"] == "cc@example.org"
    assert message["Bcc"] == "blind@example.org"


@contextmanager
def _imap_context(box):
    yield box


class _PluginAPI:
    def __init__(self, state):
        self.state = state
        self.tools = {}

    def get_state_dir(self):
        return str(self.state)

    def get_settings(self, _keys):
        return {"EMAIL_USER": "bot@example.org"}

    def register_companion_process(self, _name):
        pass

    def register_tool(self, name, handler, **_kwargs):
        self.tools[name] = handler

    def register_route(self, *_args, **_kwargs):
        pass

    def register_settings_section(self, *_args, **_kwargs):
        pass

    def register_ui_tab(self, *_args, **_kwargs):
        pass
