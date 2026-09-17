"""Standard IMAP/SMTP operations shared by mailbox tools and the companion."""
from __future__ import annotations

from contextlib import contextmanager
import imaplib
from email.policy import SMTP
import os
import re
import smtplib
import ssl
import time

import httpx

from .mime import build_message, parse_message


class MailClient:
    def __init__(self, settings=None):
        self.settings = settings if settings is not None else os.environ
        self._token = ""
        self._expires = 0.0

    def _get(self, key, default=""):
        return str(self.settings.get(key) or default)

    def _access_token(self):
        if self._token and time.time() < self._expires:
            return self._token
        refresh = self._get("EMAIL_OAUTH_REFRESH_TOKEN")
        if refresh:
            # Existing credentials only: this never starts an interactive login.
            response = httpx.post("https://oauth2.googleapis.com/token", data={
                "grant_type": "refresh_token", "refresh_token": refresh,
                "client_id": self._get("EMAIL_OAUTH_CLIENT_ID"),
                "client_secret": self._get("EMAIL_OAUTH_CLIENT_SECRET"),
            }, timeout=30)
            if response.status_code != 200:
                raise RuntimeError(f"OAuth token refresh failed (HTTP {response.status_code})")
            payload = response.json()
            self._token = str(payload.get("access_token") or "")
            self._expires = time.time() + max(0, int(payload.get("expires_in", 3600)) - 60)
        else:
            self._token = self._get("EMAIL_OAUTH_ACCESS_TOKEN")
        if not self._token:
            raise RuntimeError("OAuth access token or refresh credentials are required")
        return self._token

    def _auth(self, client, *, smtp=False):
        user = self._get("EMAIL_USER")
        if not user:
            raise RuntimeError("EMAIL_USER is required")
        if self._get("EMAIL_AUTH_MODE", "password") == "oauth2":
            value = f"user={user}\x01auth=Bearer {self._access_token()}\x01\x01"
            if smtp:
                client.auth("XOAUTH2", lambda challenge=None: value if challenge is None else "")
            else:
                client.authenticate("XOAUTH2", lambda challenge: b"" if challenge else value.encode())
        else:
            password = self._get("EMAIL_PASSWORD")
            if not password:
                raise RuntimeError("EMAIL_PASSWORD is required for password authentication")
            client.login(user, password)

    @staticmethod
    def _ok(result, operation):
        status, data = result
        if status != "OK":
            raise RuntimeError(f"IMAP {operation} failed ({status})")
        return data

    @contextmanager
    def imap(self, folder=None, *, readonly=True):
        host = self._get("EMAIL_IMAP_HOST")
        if not host:
            raise RuntimeError("EMAIL_IMAP_HOST is required")
        box = imaplib.IMAP4_SSL(host, int(self._get("EMAIL_IMAP_PORT", 993)),
                               ssl_context=ssl.create_default_context(), timeout=30)
        try:
            self._auth(box)
            if folder is not None:
                self._ok(box.select(self.quote(folder), readonly=readonly), "select")
            yield box
        finally:
            try:
                box.logout()
            except (OSError, imaplib.IMAP4.error):
                pass

    @staticmethod
    def quote(value):
        value = str(value)
        if "\r" in value or "\n" in value or "\0" in value:
            raise ValueError("IMAP values cannot contain line breaks or NUL")
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

    @staticmethod
    def metadata(box):
        def integer(name):
            _, values = box.response(name)
            if not values or not values[0]:
                raise RuntimeError(f"IMAP server omitted {name}")
            return int(values[0])
        return integer("UIDVALIDITY"), integer("UIDNEXT")

    def uids(self, box, criteria):
        data = self._ok(box.uid("SEARCH", None, *criteria), "search")
        return sorted(int(x) for x in (data[0] or b"").split())

    def fetch(self, box, folder, uid, validity):
        data = self._ok(box.uid("FETCH", str(int(uid)), "(BODY.PEEK[] INTERNALDATE FLAGS)"), "fetch")
        part = next((p for p in data if isinstance(p, tuple) and isinstance(p[1], bytes)), None)
        if part is None:
            raise RuntimeError(f"IMAP UID {uid} disappeared before it could be read")
        message = parse_message(part[1], folder=folder, uid=uid)
        date = imaplib.Internaldate2tuple(part[0])
        if date is None:
            raise RuntimeError("IMAP FETCH omitted INTERNALDATE")
        message.update(uidvalidity=validity, internal_date=time.mktime(date),
                       flags=re.findall(rb"FLAGS \(([^)]*)\)", part[0])[0].decode().split()
                       if b"FLAGS (" in part[0] else [])
        return message

    def search(self, *, folder="INBOX", criteria=None, limit=50):
        criteria = criteria or ["ALL"]
        if not isinstance(criteria, list) or not all(isinstance(c, str) and not any(x in c for x in "\r\n\0") for c in criteria):
            raise ValueError("criteria must be an array of IMAP search tokens without line breaks")
        with self.imap(folder) as box:
            validity, _ = self.metadata(box)
            uids = self.uids(box, criteria)
            chosen = uids[-max(1, min(200, int(limit))):]
            messages = [self.fetch(box, folder, uid, validity) for uid in reversed(chosen)]
        for message in messages:
            message.pop("body", None)
        return {"folder": folder, "uidvalidity": validity, "total": len(uids), "messages": messages}

    def read(self, *, uid, uidvalidity, folder="INBOX", mark_as_read=False):
        with self.imap(folder, readonly=not mark_as_read) as box:
            validity, _ = self.metadata(box)
            if validity != int(uidvalidity):
                raise ValueError("Mailbox UIDVALIDITY changed; search again before addressing a message")
            message = self.fetch(box, folder, uid, validity)
            if mark_as_read:
                self._ok(box.uid("STORE", str(int(uid)), "+FLAGS.SILENT", "(\\Seen)"), "store")
            return message

    def mailbox(self, *, action="list", folder="INBOX", uid=None, uidvalidity=None,
                destination="", flags=None, remove_flags=False):
        with self.imap(None if action in {"list", "create"} else folder, readonly=False) as box:
            if action == "list":
                return {"folders": [x.decode(errors="replace") for x in self._ok(box.list(), "list") if x]}
            if action == "create":
                self._ok(box.create(self.quote(destination)), "create")
                return {"ok": True}
            validity, _ = self.metadata(box)
            if validity != int(uidvalidity or 0) or not uid or int(uid) < 1:
                raise ValueError("A current UID and UIDVALIDITY from search/read are required")
            if action in {"copy", "move"}:
                if action == "move" and b"MOVE" not in box.capabilities:
                    raise RuntimeError("Server does not support atomic UID MOVE; use copy and flags explicitly")
                self._ok(box.uid(action.upper(), str(int(uid)), self.quote(destination)), action)
            elif action == "flags":
                flags = flags or []
                if not flags or any(not re.fullmatch(r"\\?[A-Za-z0-9_-]+", f) for f in flags):
                    raise ValueError("flags must contain IMAP flag atoms")
                self._ok(box.uid("STORE", str(int(uid)), "-FLAGS.SILENT" if remove_flags else "+FLAGS.SILENT", "(" + " ".join(flags) + ")"), "store")
            else:
                raise ValueError("Supported actions: list, create, copy, move, flags")
            return {"ok": True, "uid": int(uid), "uidvalidity": validity}

    def draft(self, *, to, subject, body, folder="Drafts", reply_to_message_id="", references=None):
        recipients = [x.strip() for x in to.split(",") if x.strip()]
        message = build_message(sender=self._get("EMAIL_USER"), recipients=recipients,
                                subject=subject, body=body, in_reply_to=reply_to_message_id,
                                references=references or [])
        with self.imap() as box:
            data = self._ok(box.append(self.quote(folder), "(\\Draft)",
                                      imaplib.Time2Internaldate(time.time()), message.as_bytes(policy=SMTP)), "append draft")
        return {"ok": True, "message_id": str(message["Message-ID"]), "folder": folder,
                "receipt": [x.decode(errors="replace") for x in data if x]}

    @contextmanager
    def smtp(self):
        host = self._get("EMAIL_SMTP_HOST")
        if not host:
            raise RuntimeError("EMAIL_SMTP_HOST is required")
        port = int(self._get("EMAIL_SMTP_PORT", 587))
        context = ssl.create_default_context()
        client = smtplib.SMTP_SSL(host, port, context=context, timeout=30) if port == 465 else smtplib.SMTP(host, port, timeout=30)
        try:
            client.ehlo()
            if port != 465:
                client.starttls(context=context)
                client.ehlo()
            self._auth(client, smtp=True)
            yield client
        finally:
            # A failed QUIT after accepted DATA must not turn success into retry.
            client.close()

    def message(self, item):
        return build_message(sender=self._get("EMAIL_USER"), recipients=list(item.recipients),
                             subject=item.subject, body=item.body, in_reply_to=item.in_reply_to,
                             references=item.references, message_id=item.message_id)

    def test_connection(self):
        with self.imap():
            pass
        with self.smtp():
            pass
        return {"ok": True, "imap": "authenticated", "smtp": "authenticated"}
