"""Comprehensive unit test suite for the email extension skill.

Tests argument validation, MIME header decoding, IMAP safe peeking vs. marking read,
output discipline & bounding, SMTP recipient handling (bare envelope addresses & BCC),
STARTTLS refusal, preflight connectivity checks, and PluginAPI integration.
"""

from __future__ import annotations

import datetime
import email.message
import email.utils
import imaplib
import os
import smtplib
import ssl
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

# Add current skill directory to sys.path
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if _CURRENT_DIR not in sys.path:
    sys.path.insert(0, _CURRENT_DIR)

from client import (
    DEFAULT_MAX_ATTACHMENTS,
    DEFAULT_MAX_BODY_CHARS,
    MAX_RAW_MESSAGE_BYTES,
    EmailConfig,
    EmailSkillError,
    IMAPClient,
    SMTPClient,
    decode_mime_header,
    extract_bare_addresses,
    extract_email_body_and_metadata,
    format_address_list,
)
import plugin


class TestEmailConfig(unittest.TestCase):
    """Test EmailConfig settings loading and validation."""

    def test_missing_all_settings(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(EmailSkillError) as ctx:
                EmailConfig.from_settings({})
            self.assertEqual(ctx.exception.code, "missing_configuration")
            self.assertIn("EMAIL_IMAP_HOST", ctx.exception.details["missing_keys"])

    def test_valid_settings_and_defaults(self) -> None:
        settings = {
            "EMAIL_IMAP_HOST": "imap.example.com",
            "EMAIL_SMTP_HOST": "smtp.example.com",
            "EMAIL_USER": "user@example.com",
            "EMAIL_PASSWORD": "secret_password",
        }
        cfg = EmailConfig.from_settings(settings)
        self.assertEqual(cfg.imap_host, "imap.example.com")
        self.assertEqual(cfg.smtp_host, "smtp.example.com")
        self.assertEqual(cfg.user, "user@example.com")
        self.assertEqual(cfg.password, "secret_password")
        self.assertEqual(cfg.imap_port, 993)
        self.assertEqual(cfg.smtp_port, 587)
        self.assertEqual(cfg.default_folder, "INBOX")

    def test_custom_ports_and_folder(self) -> None:
        settings = {
            "EMAIL_IMAP_HOST": "imap.example.com",
            "EMAIL_SMTP_HOST": "smtp.example.com",
            "EMAIL_USER": "user@example.com",
            "EMAIL_PASSWORD": "secret_password",
            "EMAIL_IMAP_PORT": "143",
            "EMAIL_SMTP_PORT": "465",
            "EMAIL_DEFAULT_FOLDER": "Archive",
        }
        cfg = EmailConfig.from_settings(settings)
        self.assertEqual(cfg.imap_port, 143)
        self.assertEqual(cfg.smtp_port, 465)
        self.assertEqual(cfg.default_folder, "Archive")


class TestMIMEHelpers(unittest.TestCase):
    """Test MIME decoding, recipient cleaning, and message extraction."""

    def test_decode_mime_header_plain(self) -> None:
        self.assertEqual(decode_mime_header("Simple Subject"), "Simple Subject")
        self.assertEqual(decode_mime_header(None), "")

    def test_decode_mime_header_encoded_utf8(self) -> None:
        encoded = "=?utf-8?B?SGVsbG8gV29ybGQ=?="
        self.assertEqual(decode_mime_header(encoded), "Hello World")

    def test_decode_mime_header_encoded_koi8r(self) -> None:
        encoded = "=?koi8-r?B?8NLJ18XU?="
        self.assertEqual(decode_mime_header(encoded), "Привет")

    def test_format_address_list(self) -> None:
        raw_str = "Alice Smith <alice@example.com>, bob@example.com; Charlie <charlie@example.com>"
        formatted = format_address_list(raw_str)
        self.assertEqual(len(formatted), 3)
        self.assertEqual(formatted[0], "Alice Smith <alice@example.com>")
        self.assertEqual(formatted[1], "bob@example.com")
        self.assertEqual(formatted[2], "Charlie <charlie@example.com>")

    def test_extract_bare_addresses(self) -> None:
        raw_list = ["Alice Smith <alice@example.com>", "bob@example.com", "Invalid Name Only"]
        bare = extract_bare_addresses(raw_list)
        self.assertEqual(bare, ["alice@example.com", "bob@example.com"])

    def test_extract_plain_text_message(self) -> None:
        msg = email.message.EmailMessage()
        msg["Subject"] = "Quarterly Report"
        msg["From"] = "lead@example.com"
        msg["To"] = "team@example.com"
        msg["Date"] = "Wed, 14 Aug 2026 10:00:00 +0000"
        msg.set_content("Here is the plain text body.")

        parsed = extract_email_body_and_metadata(msg.as_bytes())
        self.assertEqual(parsed["subject"], "Quarterly Report")
        self.assertEqual(parsed["from"], "lead@example.com")
        self.assertEqual(parsed["body_text"], "Here is the plain text body.")
        self.assertFalse(parsed["has_attachments"])

    def test_extract_multipart_alternative_html(self) -> None:
        msg = email.message.EmailMessage()
        msg["Subject"] = "HTML Notice"
        msg["From"] = "news@example.com"
        msg["To"] = "reader@example.com"
        msg.set_content("Fallback text")
        msg.add_alternative("<p>Rich <b>HTML</b> content</p>", subtype="html")

        parsed = extract_email_body_and_metadata(msg.as_bytes())
        self.assertEqual(parsed["subject"], "HTML Notice")
        self.assertIn("Fallback text", parsed["body_text"])
        self.assertIn("<p>Rich <b>HTML</b> content</p>", parsed["body_html"])

    def test_extract_attachment_metadata_and_bounding(self) -> None:
        msg = email.message.EmailMessage()
        msg["Subject"] = "With Attachment"
        msg["From"] = "sender@example.com"
        msg["To"] = "receiver@example.com"
        msg.set_content("Check attached file.")
        msg.add_attachment(b"PDF_DUMMY_DATA_12345", maintype="application", subtype="pdf", filename="report.pdf")

        parsed = extract_email_body_and_metadata(msg.as_bytes(), max_attachments=1)
        self.assertTrue(parsed["has_attachments"])
        self.assertEqual(len(parsed["attachments"]), 1)
        self.assertEqual(parsed["attachments"][0]["filename"], "report.pdf")
        self.assertEqual(parsed["attachments"][0]["content_type"], "application/pdf")
        self.assertEqual(parsed["attachments_omitted_count"], 0)

    def test_extract_body_truncation_discipline(self) -> None:
        msg = email.message.EmailMessage()
        msg["Subject"] = "Long message"
        long_body = "A" * 5000
        msg.set_content(long_body)

        parsed = extract_email_body_and_metadata(msg.as_bytes(), max_body_chars=1000)
        self.assertTrue(parsed["body_text_truncated"])
        self.assertEqual(parsed["body_text_total_chars"], 5000)
        self.assertTrue(parsed["body_text"].startswith("A" * 1000))
        self.assertIn("Truncated: 4000 characters omitted", parsed["body_text"])


class TestIMAPClient(unittest.TestCase):
    """Test IMAPClient operations with mock IMAP4_SSL."""

    def setUp(self) -> None:
        self.config = EmailConfig(
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            user="testuser@example.com",
            password="testpassword",
        )

    @patch("imaplib.IMAP4_SSL")
    def test_test_connection_success(self, mock_imap_cls: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_conn.noop.return_value = ("OK", [b"NOOP completed"])
        mock_imap_cls.return_value = mock_conn

        client = IMAPClient(self.config)
        res = client.test_connection()
        self.assertTrue(res["ok"])
        self.assertEqual(res["service"], "IMAP")
        self.assertEqual(res["status"], "authenticated")
        mock_conn.login.assert_called_once_with("testuser@example.com", "testpassword")

    @patch("imaplib.IMAP4_SSL")
    def test_list_unread_messages(self, mock_imap_cls: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_conn.select.return_value = ("OK", [b"10"])
        mock_conn.search.return_value = ("OK", [b"1 2"])

        raw_hdr_1 = b"Subject: First Unread\r\nFrom: user1@example.com\r\nDate: 14 Aug 2026\r\n\r\n"
        raw_hdr_2 = b"Subject: Second Unread\r\nFrom: user2@example.com\r\nDate: 14 Aug 2026\r\n\r\n"

        def mock_fetch(msg_id: str, query: str) -> Tuple[str, List[Any]]:
            if msg_id == "2":
                return ("OK", [(b"2 (FLAGS (\\Recent) BODY[HEADER.FIELDS (...)])", raw_hdr_2)])
            return ("OK", [(b"1 (FLAGS (\\Recent) BODY[HEADER.FIELDS (...)])", raw_hdr_1)])

        mock_conn.fetch.side_effect = mock_fetch
        mock_imap_cls.return_value = mock_conn

        with IMAPClient(self.config) as client:
            res = client.list_unread(folder="INBOX", limit=10)

        mock_conn.select.assert_called_once_with("INBOX", readonly=True)
        self.assertEqual(res["total_unread"], 2)
        self.assertEqual(len(res["messages"]), 2)
        self.assertEqual(res["messages"][0]["id"], "2")
        self.assertEqual(res["messages"][0]["subject"], "Second Unread")
        self.assertEqual(res["messages"][1]["id"], "1")
        self.assertEqual(res["messages"][1]["subject"], "First Unread")

    @patch("imaplib.IMAP4_SSL")
    def test_read_message_peeking_default(self, mock_imap_cls: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_conn.select.return_value = ("OK", [b"10"])

        msg = email.message.EmailMessage()
        msg["Subject"] = "Secret Meeting"
        msg["From"] = "boss@example.com"
        msg["To"] = "testuser@example.com"
        msg["Message-ID"] = "<secret-123@example.com>"
        msg.set_content("Do not mark me as read yet.")
        raw_msg_bytes = msg.as_bytes()

        def mock_fetch_peeking(mid: str, query: str) -> Tuple[str, List[Any]]:
            if query == "(RFC822.SIZE)":
                return ("OK", [(b"1 (RFC822.SIZE 512)",)])
            return ("OK", [(b"1 (BODY[])", raw_msg_bytes)])

        mock_conn.fetch.side_effect = mock_fetch_peeking
        mock_imap_cls.return_value = mock_conn

        with IMAPClient(self.config) as client:
            res = client.read_message(message_id="1", mark_as_read=False, folder="INBOX")

        mock_conn.select.assert_called_once_with("INBOX", readonly=True)
        self.assertEqual(mock_conn.fetch.call_count, 2)
        mock_conn.store.assert_not_called()
        self.assertFalse(res["marked_as_read"])
        self.assertEqual(res["subject"], "Secret Meeting")
        self.assertEqual(res["body_text"], "Do not mark me as read yet.")

    @patch("imaplib.IMAP4_SSL")
    def test_read_message_mark_as_read(self, mock_imap_cls: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_conn.select.return_value = ("OK", [b"10"])

        msg = email.message.EmailMessage()
        msg["Subject"] = "Action Required"
        msg.set_content("Please mark me read.")
        raw_msg_bytes = msg.as_bytes()

        def mock_fetch_mark(mid: str, query: str) -> Tuple[str, List[Any]]:
            if query == "(RFC822.SIZE)":
                return ("OK", [(b"5 (RFC822.SIZE 1024)",)])
            return ("OK", [(b"5 (RFC822)", raw_msg_bytes)])

        mock_conn.fetch.side_effect = mock_fetch_mark
        mock_conn.store.return_value = ("OK", [b"5 (FLAGS (\\Seen))"])
        mock_imap_cls.return_value = mock_conn

        with IMAPClient(self.config) as client:
            res = client.read_message(message_id="5", mark_as_read=True, folder="INBOX")

        mock_conn.select.assert_called_once_with("INBOX", readonly=False)
        self.assertEqual(mock_conn.fetch.call_count, 2)
        mock_conn.store.assert_called_once_with("5", "+FLAGS", "(\\Seen)")
        self.assertTrue(res["marked_as_read"])

    @patch("imaplib.IMAP4_SSL")
    def test_read_message_rejects_oversized(self, mock_imap_cls: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_conn.select.return_value = ("OK", [b"10"])
        # Mock size to exceed 15MB
        mock_conn.fetch.return_value = ("OK", [(b"9 (RFC822.SIZE 20000000)",)])
        mock_imap_cls.return_value = mock_conn

        with IMAPClient(self.config) as client:
            with self.assertRaises(EmailSkillError) as ctx:
                client.read_message(message_id="9", mark_as_read=False, folder="INBOX")
            self.assertEqual(ctx.exception.code, "message_too_large")
            self.assertEqual(mock_conn.fetch.call_count, 1)  # Only RFC822.SIZE called, body download aborted

    def test_read_message_rejects_invalid_message_id_format(self) -> None:
        client = IMAPClient(self.config)
        client.conn = MagicMock()
        for invalid_id in ["1:*", "1,2", "abc", "-1", "0", "", "  ", "1;2"]:
            with self.assertRaises(EmailSkillError) as ctx:
                client.read_message(message_id=invalid_id, mark_as_read=False, folder="INBOX")
            self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_validate_mailbox_name_and_rejections(self) -> None:
        from client import validate_mailbox_name

        # Valid folder names
        self.assertEqual(validate_mailbox_name("INBOX"), "INBOX")
        self.assertEqual(validate_mailbox_name("Archive/2026"), "Archive/2026")
        self.assertEqual(validate_mailbox_name("Sent Items"), "Sent Items")
        self.assertEqual(validate_mailbox_name(""), "INBOX")
        self.assertEqual(validate_mailbox_name(None), "INBOX")

        # Invalid folder names with CRLF / control characters
        for bad_folder in ["INBOX\r\nSELECT", "INBOX\n", "Folder\0", "Folder\r", "Folder;SELECT", "Folder*"]:
            with self.assertRaises(EmailSkillError) as ctx:
                validate_mailbox_name(bad_folder)
            self.assertEqual(ctx.exception.code, "invalid_argument")


class TestSMTPClient(unittest.TestCase):
    """Test SMTPClient outbound sending and security rules."""

    def setUp(self) -> None:
        self.config = EmailConfig(
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            user="sender@example.com",
            password="secret_password",
            smtp_port=587,
        )

    @patch("smtplib.SMTP")
    def test_send_starttls_success(self, mock_smtp_cls: MagicMock) -> None:
        mock_server = MagicMock()
        mock_server.has_extn.return_value = True
        mock_server.send_message.return_value = {}  # Empty dict indicates 0 refused recipients
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        client = SMTPClient(self.config)
        res = client.send(
            to=["Alice Smith <recipient@example.com>"],
            subject="Hello SMTP",
            body="Test body text",
            reply_to_message_id="<parent-msg@example.com>",
            cc="manager@example.com",
            bcc="Audit Team <audit@example.com>",
        )

        self.assertEqual(res["status"], "sent")
        self.assertEqual(res["subject"], "Hello SMTP")
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("sender@example.com", "secret_password")
        mock_server.send_message.assert_called_once()
        # Verify envelope to_addrs contains strictly bare mailbox addresses
        _, kwargs = mock_server.send_message.call_args
        to_addrs = kwargs.get("to_addrs", [])
        self.assertEqual(to_addrs, ["recipient@example.com", "manager@example.com", "audit@example.com"])

    @patch("smtplib.SMTP")
    def test_send_refusal_raises_error(self, mock_smtp_cls: MagicMock) -> None:
        mock_server = MagicMock()
        mock_server.has_extn.return_value = True
        mock_server.send_message.return_value = {"bad@example.com": (550, b"User unknown")}
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        client = SMTPClient(self.config)
        with self.assertRaises(EmailSkillError) as ctx:
            client.send(to="bad@example.com", subject="Test", body="Body")
        self.assertEqual(ctx.exception.code, "recipients_refused")

    @patch("smtplib.SMTP")
    def test_send_refuses_insecure_transport(self, mock_smtp_cls: MagicMock) -> None:
        mock_server = MagicMock()
        mock_server.has_extn.return_value = False  # Server lacks STARTTLS
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        client = SMTPClient(self.config)
        with self.assertRaises(EmailSkillError) as ctx:
            client.send(to="target@example.com", subject="Test", body="Body")

        self.assertEqual(ctx.exception.code, "insecure_transport")
        mock_server.login.assert_not_called()

    @patch("smtplib.SMTP_SSL")
    def test_send_direct_ssl_port_465(self, mock_smtps_cls: MagicMock) -> None:
        ssl_config = EmailConfig(
            imap_host="imap.example.com",
            smtp_host="smtp.example.com",
            user="sender@example.com",
            password="secret_password",
            smtp_port=465,
        )
        mock_server = MagicMock()
        mock_server.send_message.return_value = {}
        mock_smtps_cls.return_value.__enter__.return_value = mock_server

        client = SMTPClient(ssl_config)
        res = client.send(to="client@example.com", subject="SSL Email", body="Sent via port 465")
        self.assertEqual(res["status"], "sent")
        mock_server.login.assert_called_once_with("sender@example.com", "secret_password")
        mock_server.send_message.assert_called_once()


class TestPluginRegistrationAndTools(unittest.TestCase):
    """Test plugin.py tool registration with mock PluginAPI."""

    def test_plugin_register_and_tools(self) -> None:
        registered_tools: Dict[str, Any] = {}

        class MockPluginAPI:
            def register_tool(self, name: str, description: str, schema: Dict[str, Any], handler: Any) -> None:
                registered_tools[name] = {"description": description, "schema": schema, "handler": handler}

            def get_settings(self, keys: Optional[list] = None) -> Dict[str, Any]:
                return {
                    "EMAIL_IMAP_HOST": "imap.mock.com",
                    "EMAIL_SMTP_HOST": "smtp.mock.com",
                    "EMAIL_USER": "mockuser@mock.com",
                    "EMAIL_PASSWORD": "mockpassword",
                }

            def on_unload(self, callback: Any) -> None:
                pass

        api = MockPluginAPI()
        plugin.register(api)

        self.assertIn("email_list_unread", registered_tools)
        self.assertIn("email_read", registered_tools)
        self.assertIn("email_send", registered_tools)
        self.assertIn("email_test_connection", registered_tools)

    @patch("plugin.IMAPClient")
    def test_tool_list_unread_execution(self, mock_imap_cls: MagicMock) -> None:
        mock_instance = MagicMock()
        mock_instance.list_unread.return_value = {"folder": "INBOX", "total_unread": 0, "messages": []}
        mock_imap_cls.return_value.__enter__.return_value = mock_instance

        registered: Dict[str, Any] = {}
        mock_api = MagicMock()
        mock_api.register_tool.side_effect = lambda name, description, schema, handler: registered.update({name: handler})
        mock_api.get_settings.return_value = {
            "EMAIL_IMAP_HOST": "imap.mock.com",
            "EMAIL_SMTP_HOST": "smtp.mock.com",
            "EMAIL_USER": "mockuser@mock.com",
            "EMAIL_PASSWORD": "mockpassword",
        }

        plugin.register(mock_api)
        result_json = registered["email_list_unread"](folder="INBOX")
        self.assertIn('"total_unread": 0', result_json)

    @patch("plugin.IMAPClient")
    def test_tool_read_execution(self, mock_imap_cls: MagicMock) -> None:
        mock_instance = MagicMock()
        mock_instance.read_message.return_value = {
            "id": "42",
            "subject": "Greetings",
            "body_text": "Hello!",
            "marked_as_read": False,
        }
        mock_imap_cls.return_value.__enter__.return_value = mock_instance

        registered: Dict[str, Any] = {}
        mock_api = MagicMock()
        mock_api.register_tool.side_effect = lambda name, description, schema, handler: registered.update({name: handler})
        mock_api.get_settings.return_value = {
            "EMAIL_IMAP_HOST": "imap.mock.com",
            "EMAIL_SMTP_HOST": "smtp.mock.com",
            "EMAIL_USER": "mockuser@mock.com",
            "EMAIL_PASSWORD": "mockpassword",
        }

        plugin.register(mock_api)
        result_json = registered["email_read"](message_id="42")
        self.assertIn('"subject": "Greetings"', result_json)
        self.assertIn('"body_text": "Hello!"', result_json)


if __name__ == "__main__":
    unittest.main()
