"""One durable inbox/outbox and per-account IMAP cursor; no cognitive state."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


@dataclass(frozen=True)
class InboxItem:
    row_id: int
    lease_token: str
    folder: str
    uid: int
    message_id: str
    in_reply_to: str
    references: tuple[str, ...]
    sender: str
    subject: str
    body: str
    recipients: tuple[str, ...]
    host_reference: str
    attempts: int
    uidvalidity: int = 0
    context: dict = field(default_factory=dict)

    @property
    def thread_key(self):
        return self.references[0] if self.references else (self.in_reply_to or self.message_id)

    @property
    def provider_event_key(self):
        return f"{self.folder}:{self.uidvalidity}:{self.uid}:{self.message_id}"


@dataclass(frozen=True)
class OutboxItem:
    row_id: int
    lease_token: str
    request_id: str
    recipients: tuple[str, ...]
    subject: str
    body: str
    in_reply_to: str
    references: tuple[str, ...]
    attempts: int
    message_id: str


class EmailStore:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / "email_presence.sqlite3"
        with self._connect() as db:
            # Migrate the unpublished prototype without dropping its receipts.
            old = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='inbox'").fetchone()
            if old and "uidvalidity" not in {r[1] for r in db.execute("PRAGMA table_info(inbox)")}:
                db.execute("ALTER TABLE inbox RENAME TO prototype_inbox")
                db.execute("DROP INDEX IF EXISTS inbox_work")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inbox (
                    id INTEGER PRIMARY KEY, folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL,
                    uid INTEGER NOT NULL, message_id TEXT NOT NULL, in_reply_to TEXT NOT NULL,
                    references_json TEXT NOT NULL, sender TEXT NOT NULL, subject TEXT NOT NULL,
                    body TEXT NOT NULL, recipients_json TEXT NOT NULL, thread_key TEXT NOT NULL DEFAULT '',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL DEFAULT 'pending', host_reference TEXT NOT NULL DEFAULT '',
                    lease_token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(folder, uidvalidity, uid), UNIQUE(folder, message_id));
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    recipients_json TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
                    in_reply_to TEXT NOT NULL, references_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', lease_token TEXT NOT NULL DEFAULT '',
                    lease_until REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                    provider_message_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
            """)
            db.execute("BEGIN IMMEDIATE")
            if "context_json" not in {r[1] for r in db.execute("PRAGMA table_info(inbox)")}:
                db.execute("ALTER TABLE inbox ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'")
            if old and db.execute("SELECT name FROM sqlite_master WHERE name='prototype_inbox'").fetchone():
                columns = [r[1] for r in db.execute("PRAGMA table_info(prototype_inbox)")]
                names = ",".join(columns)
                db.execute(f"INSERT OR IGNORE INTO inbox ({names},uidvalidity) SELECT {names},0 FROM prototype_inbox")
                for r in db.execute("SELECT id,references_json,in_reply_to,message_id FROM inbox WHERE thread_key='' ").fetchall():
                    refs = json.loads(r["references_json"])
                    key = refs[0] if refs else (r["in_reply_to"] or r["message_id"])
                    db.execute("UPDATE inbox SET thread_key=? WHERE id=?", (key, r["id"]))
                db.execute("DROP TABLE prototype_inbox")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def get(self, key):
        with self._connect() as db:
            row = db.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        with self._connect() as db:
            self._put(db, key, value)

    @staticmethod
    def _put(db, key, value):
        db.execute("INSERT INTO runtime_state VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                   (key, json.dumps(value), time.time()))

    def cursor_key(self, account, folder):
        return "cursor:" + json.dumps([account, folder])

    def prepare_cursor(self, account, folder, validity, next_uid):
        key = self.cursor_key(account, folder)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
            cursor = json.loads(row[0]) if row else None
            if cursor is None:
                cursor = {"uidvalidity": validity, "uid": next_uid - 1, "activated_at": time.time()}
            elif cursor["uidvalidity"] != validity:
                cursor.update(uidvalidity=validity, uid=0, rescan=True)
            self._put(db, key, cursor)
        return cursor

    def ingest(self, message, *, cursor_key=None, cursor=None):
        now = time.time()
        context = {key: message[key] for key in (
            "sender_name", "cc", "date", "reply_to", "headers", "attachments"
        ) if key in message}
        with self._connect() as db:
            cur = db.execute("""INSERT OR IGNORE INTO inbox
                (folder,uidvalidity,uid,message_id,in_reply_to,references_json,sender,subject,body,recipients_json,thread_key,context_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    message.get("folder", "INBOX"), message.get("uidvalidity", 0), int(message["uid"]),
                    message["message_id"], message.get("in_reply_to", ""), json.dumps(message.get("references", [])),
                    message.get("sender", ""), message.get("subject", ""), message.get("body", ""),
                    json.dumps(message.get("recipients", [])), (message.get("references") or [message.get("in_reply_to") or message["message_id"]])[0], json.dumps(context, ensure_ascii=False), now, now))
            if cursor_key:
                self._put(db, cursor_key, cursor)
            return int(cur.lastrowid or 0), bool(cur.rowcount)

    def _claim(self, table, lease_seconds):
        now = time.time()
        token = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # SMTP DATA can have been accepted before a process disappears.
            if table == "outbox":
                db.execute("UPDATE outbox SET state='uncertain',last_error='Process lost during SMTP delivery; inspect Message-ID before retry',updated_at=? WHERE state='sending' AND lease_until<=?", (now, now))
            fifo = "AND NOT EXISTS (SELECT 1 FROM inbox earlier WHERE earlier.id < inbox.id AND earlier.thread_key = inbox.thread_key AND earlier.state IN ('pending','retry','processing') AND earlier.host_reference='')" if table == "inbox" else ""
            row = db.execute(f"SELECT * FROM {table} WHERE ((state IN ('pending','retry') AND available_at<=?) OR (state='processing' AND lease_until<=?)) {fifo} ORDER BY id LIMIT 1", (now, now)).fetchone()
            if not row:
                return None
            db.execute(f"UPDATE {table} SET state='processing',lease_token=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE id=?", (token, now + lease_seconds, now, row["id"]))
            result = dict(row)
            result.update(lease_token=token, attempts=row["attempts"] + 1)
            return result

    def claim_inbox(self, lease_seconds=2100):
        r = self._claim("inbox", lease_seconds)
        if not r:
            return None
        return InboxItem(r["id"], r["lease_token"], r["folder"], r["uid"], r["message_id"], r["in_reply_to"],
                         tuple(json.loads(r["references_json"])), r["sender"], r["subject"], r["body"],
                         tuple(json.loads(r["recipients_json"])), r["host_reference"], r["attempts"], r["uidvalidity"],
                         json.loads(r["context_json"]))

    def set_host_reference(self, row_id, token, reference):
        with self._connect() as db:
            db.execute("UPDATE inbox SET host_reference=?,updated_at=? WHERE id=? AND lease_token=?", (reference, time.time(), row_id, token))

    def _update(self, table, row_id, token, state, error="", delay=0):
        with self._connect() as db:
            db.execute(f"UPDATE {table} SET state=?,last_error=?,lease_token='',lease_until=0,available_at=?,updated_at=? WHERE id=? AND lease_token=?",
                       (state, error, time.time() + delay, time.time(), row_id, token))

    def complete_inbox(self, row_id, token):
        self._update("inbox", row_id, token, "completed")

    def retry_inbox(self, row_id, token, error, delay=5):
        self._update("inbox", row_id, token, "retry", error, delay)

    def fail_inbox(self, row_id, token, error):
        self._update("inbox", row_id, token, "failed", error)

    def enqueue_outbox(self, *, request_id, recipients, subject, body, in_reply_to="", references=()):
        now = time.time()
        message_id = "<" + hashlib.sha256((str(self.path.resolve()) + ":" + request_id).encode()).hexdigest() + "@ouroboros.local>"
        with self._connect() as db:
            cur = db.execute("""INSERT OR IGNORE INTO outbox(request_id,recipients_json,subject,body,in_reply_to,references_json,provider_message_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", (request_id, json.dumps(list(recipients)), subject, body,
                                               in_reply_to, json.dumps(list(references)), message_id, now, now))
            return bool(cur.rowcount)

    def claim_outbox(self, lease_seconds=300):
        r = self._claim("outbox", lease_seconds)
        if not r:
            return None
        if not r["provider_message_id"]:
            r["provider_message_id"] = f"<{uuid.uuid4().hex}@ouroboros.local>"
            with self._connect() as db:
                db.execute("UPDATE outbox SET provider_message_id=? WHERE id=?", (r["provider_message_id"], r["id"]))
        return OutboxItem(r["id"], r["lease_token"], r["request_id"], tuple(json.loads(r["recipients_json"])),
                          r["subject"], r["body"], r["in_reply_to"], tuple(json.loads(r["references_json"])),
                          r["attempts"], r["provider_message_id"])

    def mark_sending(self, item):
        with self._connect() as db:
            return bool(db.execute("UPDATE outbox SET state='sending',updated_at=? WHERE id=? AND lease_token=? AND state='processing'",
                                   (time.time(), item.row_id, item.lease_token)).rowcount)

    def complete_outbox(self, row_id, token):
        self._update("outbox", row_id, token, "completed")

    def retry_outbox(self, row_id, token, error, delay=5):
        self._update("outbox", row_id, token, "retry", error, delay)

    def uncertain_outbox(self, row_id, token, error):
        self._update("outbox", row_id, token, "uncertain", error)

    def fail_outbox(self, row_id, token, error):
        self._update("outbox", row_id, token, "failed", error)

    def receipt(self, request_id):
        with self._connect() as db:
            r = db.execute("SELECT request_id,state,provider_message_id,attempts,last_error,updated_at FROM outbox WHERE request_id=?", (request_id,)).fetchone()
            return dict(r) if r else None

    def status(self):
        with self._connect() as db:
            counts = {table: {r[0]: r[1] for r in db.execute(f"SELECT state,COUNT(*) FROM {table} GROUP BY state")} for table in ("inbox", "outbox")}
            counts["cursors"] = [json.loads(r[0]) for r in db.execute("SELECT value FROM runtime_state WHERE key LIKE 'cursor:%'")]
            counts["receipts"] = [dict(r) for r in db.execute("SELECT request_id,state,provider_message_id,last_error FROM outbox ORDER BY id DESC LIMIT 20")]
        return counts
