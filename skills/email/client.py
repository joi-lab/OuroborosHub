"""Core IMAP and SMTP client implementation for the email extension skill.

Zero external dependencies — uses Python standard library imaplib, smtplib, email, ssl.
Provides robust MIME decoding, safe peeking defaults, output bounding, and strict SSL/TLS security.
"""

from __future__ import annotations

import datetime
import email
import email.header
import email.message
import email.policy
import email.utils
import imaplib
import logging
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

log = logging.getLogger(__name__)

# Default timeout in seconds for network socket operations
DEFAULT_SOCKET_TIMEOUT = 15.0

# Output bounding limits to prevent overwhelming LLM context or tool result buffers
DEFAULT_MAX_BODY_CHARS = 25_000
DEFAULT_MAX_HTML_CHARS = 25_000
DEFAULT_MAX_HEADER_CHARS = 1_000
DEFAULT_MAX_ATTACHMENTS = 50

# Maximum raw email byte size allowed before aborting to protect process memory (15 MB)
MAX_RAW_MESSAGE_BYTES = 15 * 1024 * 1024

# Port defaults
DEFAULT_IMAP_SSL_PORT = 993
DEFAULT_IMAP_PLAIN_PORT = 143
DEFAULT_SMTP_SUBMISSION_PORT = 587
DEFAULT_SMTP_SSL_PORT = 465
DEFAULT_SMTP_PLAIN_PORT = 25

# Declared settings keys required by the skill
SETTINGS_KEYS = [
    "EMAIL_IMAP_HOST",
    "EMAIL_SMTP_HOST",
    "EMAIL_USER",
    "EMAIL_PASSWORD",
    "EMAIL_IMAP_PORT",
    "EMAIL_SMTP_PORT",
    "EMAIL_DEFAULT_FOLDER",
]


class EmailSkillError(Exception):
    """Base exception for email skill errors with structured details."""

    def __init__(self, message: str, code: str = "email_error", details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"error": self.message, "code": self.code}
        if self.details:
            result["details"] = self.details
        return result


@dataclass
class EmailConfig:
    """Resolved credentials and endpoint configuration."""

    imap_host: str
    smtp_host: str
    user: str
    password: str
    imap_port: int = DEFAULT_IMAP_SSL_PORT
    smtp_port: int = DEFAULT_SMTP_SUBMISSION_PORT
    default_folder: str = "INBOX"

    @classmethod
    def from_settings(cls, settings: Optional[Dict[str, Any]] = None) -> EmailConfig:
        """Load and validate configuration from settings dict or environment."""
        cfg = settings or {}

        def get_val(key: str) -> str:
            val = cfg.get(key)
            if val is None or str(val).strip() == "":
                val = os.environ.get(key, "")
            return str(val).strip()

        imap_host = get_val("EMAIL_IMAP_HOST")
        smtp_host = get_val("EMAIL_SMTP_HOST")
        user = get_val("EMAIL_USER")
        password = get_val("EMAIL_PASSWORD")

        missing: List[str] = []
        if not imap_host:
            missing.append("EMAIL_IMAP_HOST")
        if not smtp_host:
            missing.append("EMAIL_SMTP_HOST")
        if not user:
            missing.append("EMAIL_USER")
        if not password:
            missing.append("EMAIL_PASSWORD")

        if missing:
            raise EmailSkillError(
                f"Missing required email settings: {', '.join(missing)}. "
                "Please configure them in Settings -> Secrets & Grants.",
                code="missing_configuration",
                details={"missing_keys": missing},
            )

        # Parse optional ports
        imap_port_raw = get_val("EMAIL_IMAP_PORT")
        if imap_port_raw:
            try:
                imap_port = int(imap_port_raw)
            except ValueError:
                imap_port = DEFAULT_IMAP_SSL_PORT
        else:
            imap_port = DEFAULT_IMAP_SSL_PORT

        smtp_port_raw = get_val("EMAIL_SMTP_PORT")
        if smtp_port_raw:
            try:
                smtp_port = int(smtp_port_raw)
            except ValueError:
                smtp_port = DEFAULT_SMTP_SUBMISSION_PORT
        else:
            smtp_port = DEFAULT_SMTP_SUBMISSION_PORT

        default_folder = get_val("EMAIL_DEFAULT_FOLDER") or "INBOX"

        return cls(
            imap_host=imap_host,
            smtp_host=smtp_host,
            user=user,
            password=password,
            imap_port=imap_port,
            smtp_port=smtp_port,
            default_folder=default_folder,
        )


def decode_mime_header(header_value: Optional[str], max_chars: int = DEFAULT_MAX_HEADER_CHARS) -> str:
    """Decode RFC 2047 MIME encoded-word header into clean Unicode string with length bounds."""
    if not header_value:
        return ""

    try:
        decoded_fragments = email.header.decode_header(header_value)
        result_parts: List[str] = []
        for fragment, encoding in decoded_fragments:
            if isinstance(fragment, bytes):
                enc = encoding or "utf-8"
                try:
                    result_parts.append(fragment.decode(enc, errors="replace"))
                except (LookupError, UnicodeDecodeError):
                    result_parts.append(fragment.decode("utf-8", errors="replace"))
            elif isinstance(fragment, str):
                result_parts.append(fragment)
            else:
                result_parts.append(str(fragment))
        full_text = " ".join("".join(result_parts).split())
        if len(full_text) > max_chars:
            return full_text[:max_chars] + "..."
        return full_text
    except Exception as exc:
        log.debug("Header decoding fallback: %s", exc)
        raw_str = str(header_value)
        if len(raw_str) > max_chars:
            return raw_str[:max_chars] + "..."
        return raw_str


def validate_mailbox_name(folder: Optional[str]) -> str:
    """Validate and sanitize IMAP mailbox folder name against CRLF / control character injection."""
    if folder is None:
        return "INBOX"
    raw_str = str(folder)
    if any(c in raw_str for c in ("\r", "\n", "\0")):
        raise EmailSkillError(
            f"Invalid folder name containing control characters: {repr(folder)}",
            code="invalid_argument",
            details={"folder": folder},
        )
    cleaned = raw_str.strip()
    if not cleaned:
        return "INBOX"
    if not re.match(r"^[A-Za-z0-9_./\- \[\]]{1,100}$", cleaned):
        raise EmailSkillError(
            f"Invalid folder name format: '{folder}'. Expected standard mailbox characters (letters, numbers, spaces, dots, slashes, dashes).",
            code="invalid_argument",
            details={"folder": folder},
        )
    return cleaned


def format_address_list(addresses: Union[str, List[str], Tuple[str, ...], None]) -> List[str]:
    """Normalize and clean a list of email addresses with optional display names."""
    if not addresses:
        return []
    if isinstance(addresses, str):
        raw_list = [addr.strip() for addr in re.split(r"[,;]", addresses) if addr.strip()]
    else:
        raw_list = [str(addr).strip() for addr in addresses if str(addr).strip()]

    cleaned: List[str] = []
    for item in raw_list:
        name, email_part = email.utils.parseaddr(item)
        if email_part:
            if name:
                cleaned.append(f"{name} <{email_part}>")
            else:
                cleaned.append(email_part)
        elif item:
            cleaned.append(item)
    return cleaned


def extract_bare_addresses(address_list: Union[str, List[str], Tuple[str, ...], None]) -> List[str]:
    """Extract strictly bare RFC 5321 mailbox addresses (no display names) for SMTP envelope to_addrs."""
    if not address_list:
        return []
    if isinstance(address_list, str):
        raw_list = [addr.strip() for addr in re.split(r"[,;]", address_list) if addr.strip()]
    else:
        raw_list = [str(addr).strip() for addr in address_list if str(addr).strip()]

    bare: List[str] = []
    for item in raw_list:
        _, email_part = email.utils.parseaddr(item)
        clean_addr = email_part.strip()
        if clean_addr and "@" in clean_addr:
            bare.append(clean_addr)
    return bare


def extract_email_body_and_metadata(
    raw_msg_bytes: bytes,
    max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
    max_html_chars: int = DEFAULT_MAX_HTML_CHARS,
    max_attachments: int = DEFAULT_MAX_ATTACHMENTS,
) -> Dict[str, Any]:
    """Parse raw RFC 822 email message bytes into structured body, headers, and metadata with bounds."""
    try:
        msg = email.message_from_bytes(raw_msg_bytes, policy=email.policy.default)
    except Exception as exc:
        log.warning("Failed to parse message with default policy, trying compat: %s", exc)
        msg = email.message_from_bytes(raw_msg_bytes, policy=email.policy.compat32)

    subject = decode_mime_header(msg.get("Subject", ""))
    from_header = decode_mime_header(msg.get("From", ""))
    to_header = decode_mime_header(msg.get("To", ""))
    cc_header = decode_mime_header(msg.get("Cc", ""))
    date_header = decode_mime_header(msg.get("Date", ""))
    reply_to_header = decode_mime_header(msg.get("Reply-To", ""))
    message_id_header = decode_mime_header(msg.get("Message-ID", ""))
    in_reply_to_header = decode_mime_header(msg.get("In-Reply-To", ""))
    references_header = decode_mime_header(msg.get("References", ""))

    text_parts: List[str] = []
    html_parts: List[str] = []
    attachments: List[Dict[str, Any]] = []
    attachments_omitted_count = 0

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get_content_disposition() or "").lower()

            filename = part.get_filename()
            if filename or content_disposition == "attachment":
                decoded_fn = decode_mime_header(filename or "unnamed_attachment")
                # Calculate approximate payload size from raw encoded payload length without decoding into memory
                raw_payload = part.get_payload(decode=False)
                size_est = len(raw_payload) if isinstance(raw_payload, (bytes, str)) else 0

                if len(attachments) < max_attachments:
                    attachments.append({
                        "filename": decoded_fn,
                        "content_type": content_type,
                        "size_bytes": size_est,
                    })
                else:
                    attachments_omitted_count += 1
                continue

            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        text_parts.append(payload.decode(charset, errors="replace"))
                    except (LookupError, UnicodeDecodeError):
                        text_parts.append(payload.decode("utf-8", errors="replace"))
            elif content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        html_parts.append(payload.decode(charset, errors="replace"))
                    except (LookupError, UnicodeDecodeError):
                        html_parts.append(payload.decode("utf-8", errors="replace"))
    else:
        content_type = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                decoded_text = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                decoded_text = payload.decode("utf-8", errors="replace")

            if content_type == "text/html":
                html_parts.append(decoded_text)
            else:
                text_parts.append(decoded_text)

    body_text_full = "\n\n".join(text_parts).strip()
    body_html_full = "\n\n".join(html_parts).strip()

    if not body_text_full and body_html_full:
        clean_text = re.sub(r"<[^>]+>", " ", body_html_full)
        clean_text = re.sub(r"\s+", " ", clean_text).strip()
        body_text_full = clean_text

    # Apply discipline and output bounding
    body_text = body_text_full
    body_text_truncated = False
    body_text_total_chars = len(body_text_full)
    if len(body_text_full) > max_body_chars:
        omitted = len(body_text_full) - max_body_chars
        body_text = body_text_full[:max_body_chars] + f"\n\n[... Truncated: {omitted} characters omitted ...]"
        body_text_truncated = True

    body_html = body_html_full
    body_html_truncated = False
    body_html_total_chars = len(body_html_full)
    if len(body_html_full) > max_html_chars:
        omitted_html = len(body_html_full) - max_html_chars
        body_html = body_html_full[:max_html_chars] + f"\n\n<!-- Truncated: {omitted_html} characters omitted -->"
        body_html_truncated = True

    return {
        "subject": subject,
        "from": from_header,
        "to": to_header,
        "cc": cc_header,
        "date": date_header,
        "reply_to": reply_to_header,
        "message_id": message_id_header,
        "in_reply_to": in_reply_to_header,
        "references": references_header,
        "body_text": body_text,
        "body_text_truncated": body_text_truncated,
        "body_text_total_chars": body_text_total_chars,
        "body_html": body_html,
        "body_html_truncated": body_html_truncated,
        "body_html_total_chars": body_html_total_chars,
        "has_attachments": len(attachments) > 0 or attachments_omitted_count > 0,
        "attachments": attachments,
        "attachments_count": len(attachments),
        "attachments_omitted_count": attachments_omitted_count,
    }


class IMAPClient:
    """Secure IMAP connection manager and operations handler."""

    def __init__(self, config: EmailConfig, timeout: float = DEFAULT_SOCKET_TIMEOUT):
        self.config = config
        self.timeout = timeout
        self.conn: Optional[imaplib.IMAP4] = None

    def connect(self) -> imaplib.IMAP4:
        """Establish secure IMAP connection and authenticate."""
        context = ssl.create_default_context()
        host = self.config.imap_host
        port = self.config.imap_port

        try:
            if port == DEFAULT_IMAP_PLAIN_PORT:
                conn = imaplib.IMAP4(host=host, port=port, timeout=self.timeout)
                conn.starttls(ssl_context=context)
            else:
                conn = imaplib.IMAP4_SSL(host=host, port=port, ssl_context=context, timeout=self.timeout)

            conn.login(self.config.user, self.config.password)
            self.conn = conn
            return conn
        except ssl.SSLError as exc:
            raise EmailSkillError(
                f"SSL/TLS error connecting to IMAP server {host}:{port}: {exc}",
                code="ssl_error",
                details={"host": host, "port": port},
            ) from exc
        except imaplib.IMAP4.error as exc:
            raise EmailSkillError(
                f"IMAP authentication or protocol error for {self.config.user}@{host}: {exc}",
                code="imap_error",
                details={"host": host, "user": self.config.user},
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise EmailSkillError(
                f"Network connection failed to IMAP server {host}:{port}: {exc}",
                code="network_error",
                details={"host": host, "port": port},
            ) from exc

    def test_connection(self) -> Dict[str, Any]:
        """Perform a lightweight connection and authentication preflight check."""
        t0 = time.time()
        conn = self.connect()
        try:
            status, _ = conn.noop()
            elapsed_ms = round((time.time() - t0) * 1000, 1)
            return {
                "ok": status == "OK",
                "service": "IMAP",
                "host": self.config.imap_host,
                "port": self.config.imap_port,
                "user": self.config.user,
                "latency_ms": elapsed_ms,
                "status": "authenticated" if status == "OK" else "noop_failed",
            }
        finally:
            self.close()

    def close(self) -> None:
        """Cleanly logout and close IMAP connection."""
        if self.conn:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    def __enter__(self) -> IMAPClient:
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def list_unread(self, folder: Optional[str] = None, since: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        """List unread messages in the specified mailbox folder with metadata."""
        if not self.conn:
            self.connect()
        assert self.conn is not None

        target_folder = validate_mailbox_name(folder or self.config.default_folder or "INBOX")
        limit = max(1, min(limit, 200))

        try:
            status, count_data = self.conn.select(target_folder, readonly=True)
            if status != "OK":
                raise EmailSkillError(
                    f"Failed to select IMAP folder '{target_folder}': {count_data}",
                    code="folder_not_found",
                    details={"folder": target_folder},
                )
        except imaplib.IMAP4.error as exc:
            raise EmailSkillError(
                f"Error selecting folder '{target_folder}': {exc}",
                code="imap_select_error",
                details={"folder": target_folder},
            ) from exc

        search_terms = ["UNSEEN"]
        if since:
            since_str = since.strip()
            # Strictly validate date pattern to prevent IMAP protocol / CRLF injection
            if not re.match(r"^[0-9A-Za-z-]{4,20}$", since_str) or "\r" in since_str or "\n" in since_str:
                raise EmailSkillError(
                    f"Invalid date format for 'since': '{since}'. Expected YYYY-MM-DD or DD-Mon-YYYY.",
                    code="invalid_argument",
                    details={"since": since},
                )
            try:
                if "-" in since_str:
                    parts = since_str.split("-")
                    if len(parts) == 3 and len(parts[0]) == 4:
                        dt = datetime.datetime.strptime(since_str, "%Y-%m-%d")
                        formatted_since = dt.strftime("%d-%b-%Y")
                    else:
                        formatted_since = since_str
                else:
                    formatted_since = since_str
                search_terms.extend(["SINCE", formatted_since])
            except Exception as exc:
                log.debug("Date format parse fallback: %s", exc)
                search_terms.extend(["SINCE", since_str])

        try:
            status, search_data = self.conn.search(None, *search_terms)
            if status != "OK":
                raise EmailSkillError(
                    f"IMAP search failed in folder '{target_folder}'",
                    code="search_error",
                    details={"folder": target_folder, "criteria": search_terms},
                )
        except Exception as exc:
            raise EmailSkillError(
                f"Failed searching messages in '{target_folder}': {exc}",
                code="imap_search_error",
            ) from exc

        raw_ids = search_data[0].split() if search_data and search_data[0] else []
        msg_ids = [mid.decode("ascii", errors="replace") for mid in reversed(raw_ids)]
        total_unread = len(msg_ids)
        selected_ids = msg_ids[:limit]

        results: List[Dict[str, Any]] = []
        accumulated_chars = 0
        MAX_TOTAL_LIST_CHARS = 50000  # 50KB total character budget for list_unread response
        omitted_due_to_budget = 0
        omitted_reason: Optional[str] = None

        loop_start_time = time.monotonic()
        # Bound loop execution to timeout minus 5s safety headroom (minimum 10s)
        max_loop_duration = max(10.0, float(self.timeout) - 5.0)
        processed_count = 0

        for mid in selected_ids:
            if accumulated_chars >= MAX_TOTAL_LIST_CHARS:
                omitted_due_to_budget = len(selected_ids) - processed_count
                omitted_reason = f"output budget limit reached (max {MAX_TOTAL_LIST_CHARS} chars)"
                break
            if time.monotonic() - loop_start_time > max_loop_duration:
                omitted_due_to_budget = len(selected_ids) - processed_count
                omitted_reason = f"network time limit reached ({int(max_loop_duration)}s timeout)"
                break

            processed_count += 1
            try:
                fetch_status, fetch_data = self.conn.fetch(
                    mid,
                    "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO CC DATE MESSAGE-ID)] FLAGS)",
                )
                if fetch_status != "OK" or not fetch_data:
                    continue

                header_bytes = b""
                flags_list: List[str] = []
                for item in fetch_data:
                    if isinstance(item, tuple) and len(item) >= 2:
                        header_bytes += item[1]
                        if len(item) > 0 and isinstance(item[0], bytes):
                            flag_match = re.search(r"FLAGS \(([^)]*)\)", item[0].decode("ascii", errors="replace"))
                            if flag_match:
                                flags_list = flag_match.group(1).split()
                    elif isinstance(item, bytes):
                        flag_match = re.search(r"FLAGS \(([^)]*)\)", item.decode("ascii", errors="replace"))
                        if flag_match:
                            flags_list = flag_match.group(1).split()

                msg_header = email.message_from_bytes(header_bytes)
                subject = decode_mime_header(msg_header.get("Subject", "(no subject)"))
                from_hdr = decode_mime_header(msg_header.get("From", ""))
                to_hdr = decode_mime_header(msg_header.get("To", ""))
                date_hdr = decode_mime_header(msg_header.get("Date", ""))
                msg_id_hdr = decode_mime_header(msg_header.get("Message-ID", ""))

                entry = {
                    "id": mid,
                    "subject": subject,
                    "from": from_hdr,
                    "to": to_hdr,
                    "date": date_hdr,
                    "message_id_header": msg_id_hdr,
                    "flags": flags_list,
                }
                results.append(entry)
                accumulated_chars += len(subject) + len(from_hdr) + len(to_hdr) + len(date_hdr) + len(msg_id_hdr) + 50
            except Exception as exc:
                log.warning("Failed to parse headers for message %s: %s", mid, exc)
                err_entry = {
                    "id": mid,
                    "subject": "(parse error)",
                    "from": "",
                    "date": "",
                    "error": str(exc),
                }
                results.append(err_entry)
                accumulated_chars += 100

        res_payload: Dict[str, Any] = {
            "folder": target_folder,
            "total_unread": total_unread,
            "returned_count": len(results),
            "messages": results,
        }
        if omitted_due_to_budget > 0:
            res_payload["omitted_messages_count"] = omitted_due_to_budget
            reason_str = omitted_reason or f"output budget limit reached (max {MAX_TOTAL_LIST_CHARS} chars)"
            res_payload["omission_note"] = f"⚠️ OMISSION NOTE: {omitted_due_to_budget} unread messages were omitted ({reason_str})."

        return res_payload

    def read_message(
        self,
        message_id: str,
        mark_as_read: bool = False,
        folder: Optional[str] = None,
        max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
    ) -> Dict[str, Any]:
        """Fetch and parse full email content.

        Safe default: peeks with BODY.PEEK without marking as read unless mark_as_read=True.
        """
        if not self.conn:
            self.connect()
        assert self.conn is not None

        target_folder = validate_mailbox_name(folder or self.config.default_folder or "INBOX")
        mid_str = str(message_id).strip()
        if not mid_str or not re.match(r"^\d+$", mid_str) or int(mid_str) <= 0:
            raise EmailSkillError(
                f"message_id must be a single positive integer sequence number (got '{message_id}'). Multi-message ranges like '1:*' are not permitted.",
                code="invalid_argument",
                details={"message_id": message_id},
            )

        try:
            status, _ = self.conn.select(target_folder, readonly=(not mark_as_read))
            if status != "OK":
                raise EmailSkillError(
                    f"Failed to select folder '{target_folder}'",
                    code="folder_not_found",
                    details={"folder": target_folder},
                )
        except Exception as exc:
            raise EmailSkillError(f"Error selecting folder '{target_folder}': {exc}", code="imap_error") from exc

        try:
            # Pre-check message size via RFC822.SIZE before downloading full message bytes
            try:
                size_status, size_data = self.conn.fetch(mid_str, "(RFC822.SIZE)")
                if size_status == "OK" and size_data and size_data[0]:
                    for item in size_data:
                        raw_item = item[0] if isinstance(item, tuple) and len(item) > 0 else item
                        if isinstance(raw_item, (bytes, str)):
                            raw_str = raw_item.decode("ascii", errors="replace") if isinstance(raw_item, bytes) else str(raw_item)
                            match = re.search(r"RFC822\.SIZE\s+(\d+)", raw_str, re.IGNORECASE)
                            if match:
                                msg_size = int(match.group(1))
                                if msg_size > MAX_RAW_MESSAGE_BYTES:
                                    raise EmailSkillError(
                                        f"Message ID '{mid_str}' exceeds maximum allowed size of {MAX_RAW_MESSAGE_BYTES // (1024*1024)}MB ({msg_size} bytes).",
                                        code="message_too_large",
                                        details={"size_bytes": msg_size, "max_bytes": MAX_RAW_MESSAGE_BYTES},
                                    )
            except EmailSkillError:
                raise
            except Exception as exc:
                log.debug("RFC822.SIZE pre-check note: %s", exc)

            fetch_query = "(RFC822)" if mark_as_read else "(BODY.PEEK[])"
            status, data = self.conn.fetch(mid_str, fetch_query)
            if status != "OK" or not data:
                raise EmailSkillError(
                    f"Message ID '{mid_str}' not found in folder '{target_folder}'",
                    code="message_not_found",
                    details={"message_id": mid_str, "folder": target_folder},
                )

            raw_email = b""
            for response_part in data:
                if isinstance(response_part, tuple) and len(response_part) >= 2:
                    raw_email += response_part[1]

            if not raw_email:
                raise EmailSkillError(
                    f"Empty payload returned for message ID '{mid_str}'",
                    code="empty_message",
                    details={"message_id": mid_str},
                )

            if len(raw_email) > MAX_RAW_MESSAGE_BYTES:
                raise EmailSkillError(
                    f"Message ID '{mid_str}' exceeds maximum raw size of {MAX_RAW_MESSAGE_BYTES // (1024*1024)}MB ({len(raw_email)} bytes).",
                    code="message_too_large",
                    details={"size_bytes": len(raw_email), "max_bytes": MAX_RAW_MESSAGE_BYTES},
                )

            parsed = extract_email_body_and_metadata(
                raw_email,
                max_body_chars=max_body_chars,
                max_html_chars=max_body_chars,
            )

            marked_success = False
            mark_warning: Optional[str] = None
            if mark_as_read:
                try:
                    store_status, _ = self.conn.store(mid_str, "+FLAGS", "(\\Seen)")
                    marked_success = (store_status == "OK")
                    if not marked_success:
                        mark_warning = f"IMAP server returned status '{store_status}' when setting \\Seen flag"
                except Exception as exc:
                    log.warning("Failed to set \\Seen flag explicitly: %s", exc)
                    marked_success = False
                    mark_warning = f"Failed to set \\Seen flag: {exc}"

            res_dict = {
                "id": mid_str,
                "folder": target_folder,
                "subject": parsed["subject"],
                "from": parsed["from"],
                "to": parsed["to"],
                "cc": parsed["cc"],
                "date": parsed["date"],
                "reply_to": parsed["reply_to"],
                "message_id_header": parsed["message_id"],
                "in_reply_to_header": parsed["in_reply_to"],
                "references_header": parsed["references"],
                "body_text": parsed["body_text"],
                "body_text_truncated": parsed["body_text_truncated"],
                "body_text_total_chars": parsed["body_text_total_chars"],
                "body_html": parsed["body_html"],
                "body_html_truncated": parsed["body_html_truncated"],
                "body_html_total_chars": parsed["body_html_total_chars"],
                "has_attachments": parsed["has_attachments"],
                "attachments": parsed["attachments"],
                "attachments_count": parsed["attachments_count"],
                "attachments_omitted_count": parsed["attachments_omitted_count"],
                "marked_as_read": marked_success if mark_as_read else False,
                "attachments_note": "Attachment file extraction is planned for future releases. Metadata listed above.",
            }
            if mark_warning:
                res_dict["mark_as_read_warning"] = mark_warning
            return res_dict
        except EmailSkillError:
            raise
        except Exception as exc:
            raise EmailSkillError(
                f"Failed fetching message ID '{mid_str}': {exc}",
                code="fetch_error",
                details={"message_id": mid_str},
            ) from exc


class SMTPClient:
    """Secure SMTP client for sending outbound email."""

    def __init__(self, config: EmailConfig, timeout: float = DEFAULT_SOCKET_TIMEOUT):
        self.config = config
        self.timeout = timeout

    def test_connection(self) -> Dict[str, Any]:
        """Perform a lightweight SMTP connection, TLS negotiation, and authentication preflight check."""
        t0 = time.time()
        host = self.config.smtp_host
        port = self.config.smtp_port
        context = ssl.create_default_context()

        try:
            if port == DEFAULT_SMTP_SSL_PORT:
                with smtplib.SMTP_SSL(host=host, port=port, context=context, timeout=self.timeout) as server:
                    server.login(self.config.user, self.config.password)
            else:
                with smtplib.SMTP(host=host, port=port, timeout=self.timeout) as server:
                    server.ehlo()
                    if not server.has_extn("STARTTLS"):
                        raise EmailSkillError(
                            f"SMTP server {host}:{port} does not support STARTTLS. Refusing insecure cleartext authentication.",
                            code="insecure_transport",
                            details={"host": host, "port": port},
                        )
                    server.starttls(context=context)
                    server.ehlo()
                    server.login(self.config.user, self.config.password)

            elapsed_ms = round((time.time() - t0) * 1000, 1)
            return {
                "ok": True,
                "service": "SMTP",
                "host": host,
                "port": port,
                "user": self.config.user,
                "latency_ms": elapsed_ms,
                "status": "authenticated",
            }
        except EmailSkillError:
            raise
        except ssl.SSLError as exc:
            raise EmailSkillError(
                f"SSL/TLS error connecting to SMTP server {host}:{port}: {exc}",
                code="ssl_error",
                details={"host": host, "port": port},
            ) from exc
        except smtplib.SMTPAuthenticationError as exc:
            raise EmailSkillError(
                f"SMTP authentication failed for {self.config.user}@{host}: {exc}",
                code="auth_error",
                details={"host": host, "user": self.config.user},
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise EmailSkillError(
                f"Network connection failed to SMTP server {host}:{port}: {exc}",
                code="network_error",
                details={"host": host, "port": port},
            ) from exc

    def send(
        self,
        to: Union[str, List[str]],
        subject: str,
        body: str,
        reply_to_message_id: Optional[str] = None,
        cc: Optional[Union[str, List[str]]] = None,
        bcc: Optional[Union[str, List[str]]] = None,
        body_type: str = "plain",
    ) -> Dict[str, Any]:
        """Compose and send email via authenticated TLS/SSL SMTP with strictly bare envelope addresses."""
        to_list = format_address_list(to)
        if not to_list:
            raise EmailSkillError("At least one valid recipient 'to' address is required", code="invalid_recipient")

        cc_list = format_address_list(cc)
        bcc_list = format_address_list(bcc)

        # Extract strictly bare RFC 5321 email addresses for the SMTP envelope RCPT TO command
        envelope_recipients = extract_bare_addresses(to_list + cc_list + bcc_list)
        if not envelope_recipients:
            raise EmailSkillError("No valid RFC 5321 email addresses found in recipients list", code="invalid_recipient")

        msg = email.message.EmailMessage()
        msg["From"] = self.config.user
        msg["To"] = ", ".join(to_list)
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        msg["Subject"] = subject or "(no subject)"
        msg["Date"] = email.utils.formatdate(localtime=True)

        msg_id = email.utils.make_msgid()
        msg["Message-ID"] = msg_id

        if reply_to_message_id:
            reply_id_clean = reply_to_message_id.strip()
            if not reply_id_clean.startswith("<"):
                reply_id_clean = f"<{reply_id_clean}>"
            msg["In-Reply-To"] = reply_id_clean
            msg["References"] = reply_id_clean

        clean_body_type = (body_type or "plain").strip().lower()
        if clean_body_type not in {"plain", "html"}:
            raise EmailSkillError(
                f"Invalid body_type '{body_type}': must be 'plain' or 'html'",
                code="invalid_argument",
                details={"allowed": ["plain", "html"], "provided": body_type},
            )

        if clean_body_type == "html":
            msg.set_content(body, subtype="html", charset="utf-8")
        else:
            msg.set_content(body, subtype="plain", charset="utf-8")

        host = self.config.smtp_host
        port = self.config.smtp_port
        context = ssl.create_default_context()

        try:
            if port == DEFAULT_SMTP_SSL_PORT:
                with smtplib.SMTP_SSL(host=host, port=port, context=context, timeout=self.timeout) as server:
                    server.login(self.config.user, self.config.password)
                    refused = server.send_message(msg, to_addrs=envelope_recipients)
                    if refused:
                        raise EmailSkillError(
                            f"SMTP server refused delivery to recipients: {list(refused.keys())}",
                            code="recipients_refused",
                            details={"refused_recipients": refused},
                        )
            else:
                with smtplib.SMTP(host=host, port=port, timeout=self.timeout) as server:
                    server.ehlo()
                    if not server.has_extn("STARTTLS"):
                        raise EmailSkillError(
                            f"SMTP server {host}:{port} does not support STARTTLS. Refusing insecure cleartext authentication.",
                            code="insecure_transport",
                            details={"host": host, "port": port},
                        )
                    server.starttls(context=context)
                    server.ehlo()
                    server.login(self.config.user, self.config.password)
                    refused = server.send_message(msg, to_addrs=envelope_recipients)
                    if refused:
                        raise EmailSkillError(
                            f"SMTP server refused delivery to recipients: {list(refused.keys())}",
                            code="recipients_refused",
                            details={"refused_recipients": refused},
                        )

            return {
                "status": "sent",
                "message_id": msg_id,
                "to": to_list,
                "cc": cc_list,
                "bcc": bcc_list,
                "envelope_recipients": envelope_recipients,
                "subject": subject,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        except EmailSkillError:
            raise
        except ssl.SSLError as exc:
            raise EmailSkillError(
                f"SSL/TLS error connecting to SMTP server {host}:{port}: {exc}",
                code="ssl_error",
                details={"host": host, "port": port},
            ) from exc
        except smtplib.SMTPAuthenticationError as exc:
            raise EmailSkillError(
                f"SMTP authentication failed for {self.config.user}@{host}: {exc}",
                code="auth_error",
                details={"host": host, "user": self.config.user},
            ) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise EmailSkillError(
                f"Recipients refused by SMTP server: {exc}",
                code="recipients_refused",
                details={"recipients": envelope_recipients},
            ) from exc
        except smtplib.SMTPException as exc:
            raise EmailSkillError(
                f"SMTP protocol error: {exc}",
                code="smtp_error",
                details={"host": host, "port": port},
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise EmailSkillError(
                f"Network connection failed to SMTP server {host}:{port}: {exc}",
                code="network_error",
                details={"host": host, "port": port},
            ) from exc
