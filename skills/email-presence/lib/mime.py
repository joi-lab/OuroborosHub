"""Small RFC 5322 helpers used by the email Presence companion."""
from __future__ import annotations

import html
import hashlib
import re
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.policy import default
from email.utils import getaddresses, make_msgid, parseaddr
from typing import Any


def decode_header_value(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except (LookupError, UnicodeError, ValueError):
        return str(value)


def _plain_from_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return str(raw or "") if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</p\s*>", "\n", value)
    value = re.sub(r"<[^>]+>", " ", value)
    return html.unescape(re.sub(r"[ \t]+", " ", value)).strip()


def extract_body(message: Message, max_chars: int = 100_000) -> str:
    plain = ""
    html_body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart() or part.get_content_disposition() == "attachment":
                continue
            kind = part.get_content_type().lower()
            value = _plain_from_part(part)
            if kind == "text/plain" and not plain:
                plain = value
            elif kind == "text/html" and not html_body:
                html_body = value
    else:
        kind = message.get_content_type().lower()
        value = _plain_from_part(message)
        plain = value if kind == "text/plain" else ""
        html_body = value if kind == "text/html" else ""
    text = plain.strip() or _html_to_text(html_body)
    return text if len(text) <= max_chars else text[:max_chars] + f"\n[Body truncated: {len(text)} characters total]"


def _message_ids(value: Any) -> list[str]:
    return [item.strip() for item in re.findall(r"<[^>]+>", str(value or "")) if item.strip()]


def parse_message(raw: bytes, *, folder: str, uid: int, max_body_chars: int = 100_000) -> dict[str, Any]:
    msg = message_from_bytes(raw, policy=default)
    message_id = str(msg.get("Message-ID") or "").strip() or f"<sha256-{hashlib.sha256(raw).hexdigest()}@ouroboros.local>"
    sender_name, sender_addr = parseaddr(decode_header_value(msg.get("From")))
    recipients = [addr for _, addr in getaddresses([decode_header_value(msg.get("To"))]) if addr]
    cc = [addr for _, addr in getaddresses([decode_header_value(msg.get("Cc"))]) if addr]
    return {
        "folder": folder,
        "uid": int(uid),
        "message_id": message_id,
        "in_reply_to": str(msg.get("In-Reply-To") or "").strip(),
        "references": _message_ids(msg.get("References")),
        "subject": decode_header_value(msg.get("Subject")),
        "sender": sender_addr or decode_header_value(msg.get("From")),
        "sender_name": sender_name,
        "recipients": recipients,
        "cc": cc,
        "date": str(msg.get("Date") or ""),
        "body": extract_body(msg, max_chars=max_body_chars),
    }


def build_message(*, sender: str, recipients: list[str], subject: str, body: str, in_reply_to: str = "", references: list[str] | tuple[str, ...] = (), message_id: str = "") -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject or "(no subject)"
    message["Message-ID"] = message_id or make_msgid()
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        chain = [str(item).strip() for item in references if str(item).strip()]
        if in_reply_to not in chain:
            chain.append(in_reply_to)
        message["References"] = " ".join(chain)
    message.set_content(str(body or ""))
    return message
