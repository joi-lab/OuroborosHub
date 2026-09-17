"""Email extension skill plugin entrypoint.

Registers extension tools for listing unread messages, reading full email bodies (with safe peeking),
sending emails via authenticated SMTP, and running connection preflights.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Union

# Ensure sibling client module can be imported reliably in all execution modes
_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

from client import (
    DEFAULT_MAX_BODY_CHARS,
    SETTINGS_KEYS,
    EmailConfig,
    EmailSkillError,
    IMAPClient,
    SMTPClient,
)

log = logging.getLogger(__name__)

# JSON Schemas for tool registration matching PluginAPI and ToolRegistry contracts
_LIST_UNREAD_SCHEMA = {
    "type": "object",
    "properties": {
        "folder": {
            "type": "string",
            "description": "IMAP mailbox folder to inspect (defaults to EMAIL_DEFAULT_FOLDER or 'INBOX').",
        },
        "since": {
            "type": "string",
            "description": "Optional search filter date in format YYYY-MM-DD or DD-Mon-YYYY (e.g. '2026-08-01' or '01-Aug-2026').",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum number of unread email summaries to return (default 50, clamp 1-200).",
            "default": 50,
            "minimum": 1,
            "maximum": 200,
        },
    },
}

_READ_SCHEMA = {
    "type": "object",
    "required": ["message_id"],
    "properties": {
        "message_id": {
            "type": "string",
            "description": "The positive integer message sequence number (e.g. '1', '2', '101') on the IMAP server as returned by email_list_unread.",
        },
        "mark_as_read": {
            "type": "boolean",
            "description": "Safe default false: peeks with BODY.PEEK[] without altering unread flags. Set true to mark message as read.",
            "default": False,
        },
        "folder": {
            "type": "string",
            "description": "IMAP mailbox folder containing the message (defaults to EMAIL_DEFAULT_FOLDER or 'INBOX').",
        },
        "max_body_chars": {
            "type": "integer",
            "description": "Maximum characters of text/HTML body to return before truncating with an explicit omission note (default 25000, max 100000).",
            "default": DEFAULT_MAX_BODY_CHARS,
            "minimum": 500,
            "maximum": 100000,
        },
    },
}

_SEND_SCHEMA = {
    "type": "object",
    "required": ["to", "subject", "body"],
    "properties": {
        "to": {
            "anyOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
            "description": "Recipient email address, comma-separated list of addresses, or list of email address strings.",
        },
        "subject": {
            "type": "string",
            "description": "Email subject line.",
        },
        "body": {
            "type": "string",
            "description": "Body text of the message.",
        },
        "reply_to_message_id": {
            "type": "string",
            "description": "Optional Message-ID string to populate In-Reply-To and References headers for thread replies.",
        },
        "cc": {
            "anyOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
            "description": "Optional CC recipient address, comma-separated list, or list of address strings.",
        },
        "bcc": {
            "anyOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
            "description": "Optional BCC recipient address, comma-separated list, or list of address strings.",
        },
        "body_type": {
            "type": "string",
            "enum": ["plain", "html"],
            "description": "Content type of the body: 'plain' (default) or 'html'.",
            "default": "plain",
        },
    },
}

_TEST_CONNECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "check_smtp": {
            "type": "boolean",
            "description": "Whether to test SMTP TLS negotiation and authentication in addition to IMAP (default true).",
            "default": True,
        },
    },
}


def register(api: Any) -> None:
    """Register extension tools and cleanup hooks on the PluginAPI instance."""

    def _get_config() -> EmailConfig:
        """Fetch settings from the host PluginAPI and construct EmailConfig."""
        try:
            settings_dict = api.get_settings(keys=SETTINGS_KEYS)
        except TypeError:
            try:
                settings_dict = api.get_settings(SETTINGS_KEYS)
            except TypeError:
                settings_dict = api.get_settings()
            except Exception as exc:
                raise EmailSkillError(f"Failed to read settings from host: {exc}", code="settings_read_error") from exc
        except Exception as exc:
            raise EmailSkillError(f"Failed to read settings from host: {exc}", code="settings_read_error") from exc

        return EmailConfig.from_settings(settings_dict)

    def tool_email_list_unread(
        folder: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 50,
    ) -> str:
        """List unread email headers in the specified IMAP folder without marking read."""
        try:
            config = _get_config()
            with IMAPClient(config) as client:
                res = client.list_unread(folder=folder, since=since, limit=limit)
            return json.dumps(res, indent=2, ensure_ascii=False)
        except EmailSkillError as exc:
            log.warning("email_list_unread domain error: %s", exc)
            return json.dumps(exc.to_dict(), indent=2, ensure_ascii=False)
        except Exception as exc:
            log.exception("email_list_unread unexpected failure")
            return json.dumps({"error": f"Internal error listing unread emails: {exc}", "code": "internal_error"}, indent=2)

    def tool_email_read(
        message_id: str,
        mark_as_read: bool = False,
        folder: Optional[str] = None,
        max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
    ) -> str:
        """Fetch and parse full email message body. Peeks safely by default unless mark_as_read=True."""
        try:
            config = _get_config()
            with IMAPClient(config) as client:
                res = client.read_message(
                    message_id=message_id,
                    mark_as_read=mark_as_read,
                    folder=folder,
                    max_body_chars=max_body_chars,
                )
            return json.dumps(res, indent=2, ensure_ascii=False)
        except EmailSkillError as exc:
            log.warning("email_read domain error: %s", exc)
            return json.dumps(exc.to_dict(), indent=2, ensure_ascii=False)
        except Exception as exc:
            log.exception("email_read unexpected failure")
            return json.dumps({"error": f"Internal error reading email ID '{message_id}': {exc}", "code": "internal_error"}, indent=2)

    def tool_email_send(
        to: Union[str, List[str]],
        subject: str,
        body: str,
        reply_to_message_id: Optional[str] = None,
        cc: Optional[Union[str, List[str]]] = None,
        bcc: Optional[Union[str, List[str]]] = None,
        body_type: str = "plain",
    ) -> str:
        """Compose and send an email via authenticated SMTP with optional reply-to threading headers."""
        try:
            config = _get_config()
            client = SMTPClient(config)
            res = client.send(
                to=to,
                subject=subject,
                body=body,
                reply_to_message_id=reply_to_message_id,
                cc=cc,
                bcc=bcc,
                body_type=body_type,
            )
            return json.dumps(res, indent=2, ensure_ascii=False)
        except EmailSkillError as exc:
            log.warning("email_send domain error: %s", exc)
            return json.dumps(exc.to_dict(), indent=2, ensure_ascii=False)
        except Exception as exc:
            log.exception("email_send unexpected failure")
            return json.dumps({"error": f"Internal error sending email: {exc}", "code": "internal_error"}, indent=2)

    def tool_email_test_connection(check_smtp: bool = True) -> str:
        """Validate configured IMAP and SMTP endpoints, TLS/SSL certificates, and authentication credentials."""
        results: Dict[str, Any] = {"status": "ok", "checks": []}
        try:
            config = _get_config()
            imap_client = IMAPClient(config)
            imap_res = imap_client.test_connection()
            results["checks"].append(imap_res)

            if check_smtp:
                smtp_client = SMTPClient(config)
                smtp_res = smtp_client.test_connection()
                results["checks"].append(smtp_res)

            # Validate that every performed check actually succeeded
            for check in results["checks"]:
                if not check.get("ok"):
                    results["status"] = "error"
                    results["failing_service"] = check.get("service")
                    break

            return json.dumps(results, indent=2, ensure_ascii=False)
        except EmailSkillError as exc:
            log.warning("email_test_connection domain error: %s", exc)
            err_dict = exc.to_dict()
            err_dict["status"] = "error"
            return json.dumps(err_dict, indent=2, ensure_ascii=False)
        except Exception as exc:
            log.exception("email_test_connection unexpected failure")
            return json.dumps({"status": "error", "error": f"Internal error during preflight check: {exc}", "code": "internal_error"}, indent=2)

    # Register all four tools on the PluginAPI instance
    api.register_tool(
        name="email_list_unread",
        description="List unread email headers (id, subject, sender, date, flags) in an IMAP folder (default INBOX) without marking read.",
        schema=_LIST_UNREAD_SCHEMA,
        handler=tool_email_list_unread,
    )

    api.register_tool(
        name="email_read",
        description="Fetch and parse full email message body (plain text / HTML fallback) by message ID. Peeks safely by default unless mark_as_read=True.",
        schema=_READ_SCHEMA,
        handler=tool_email_read,
    )

    api.register_tool(
        name="email_send",
        description="Compose and send an email via authenticated SMTP (STARTTLS on port 587 or direct SSL on port 465) with optional reply-to threading headers.",
        schema=_SEND_SCHEMA,
        handler=tool_email_send,
    )

    api.register_tool(
        name="email_test_connection",
        description="Validate configured IMAP and SMTP endpoints, TLS/SSL certificates, and authentication credentials without sending mail.",
        schema=_TEST_CONNECTION_SCHEMA,
        handler=tool_email_test_connection,
    )

    # Register unload cleanup handler
    def _on_unload_cleanup() -> None:
        log.info("Email extension skill unloaded cleanly.")

    api.on_unload(_on_unload_cleanup)
