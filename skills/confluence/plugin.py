"""Register ordinary synchronous Confluence tools through PluginAPI 2.0."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .client import Config, ConfluenceClient, ConfluenceError, SETTINGS_KEYS
from .operations import Operations


def text(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


PAGE_ID = text("Numeric page ID or full page URL on the configured site.")
BODY = text("Complete Confluence storage XHTML, for example <p>Hello</p>; never Markdown.")
LIMIT = {"type": "integer", "minimum": 1, "maximum": 250, "default": 25,
         "description": "One API page's requested size. Follow next_url until complete is true."}
NEXT = text("next_url returned by the same listing/search tool. Keep the other arguments unchanged.")
STATUS = {"type": "string", "enum": ["current", "draft"], "default": "current"}
PAGINATION = {"limit": LIMIT, "next_url": NEXT}

# The host adds the skill namespace to these short names.
TOOLS = {
    "test_connection": (
        "Read-only connection check: report the authenticated actor, space access and optional page access separately; does not test writes.",
        {"page_id": PAGE_ID}, [],
    ),
    "list_spaces": (
        "List accessible Confluence spaces, optionally matching a space key. Returns one page and explicit continuation.",
        {"space_key": text("Optional space key, such as DOCS."), **PAGINATION}, [],
    ),
    "list_pages": (
        "List page metadata, optionally filtered by numeric space ID and exact title. Use get_page for full content.",
        {"space_id": text("Optional numeric space ID from list_spaces, not the space key."),
         "title": text("Optional exact title."), "status": STATUS, **PAGINATION}, [],
    ),
    "search": (
        "Search with Confluence Query Language (CQL); results are search matches/excerpts, not full page content. Follow pagination explicitly.",
        {"cql": text('CQL expression, e.g. type=page AND space="DOCS"'), **PAGINATION}, ["cql"],
    ),
    "get_page": (
        "Read a complete page including full storage XHTML, title, space and current version. No local body truncation.",
        {"page_id": PAGE_ID, "status": STATUS,
         "version": {"type": "integer", "minimum": 1, "description": "Optional historical version; omit for latest."}}, ["page_id"],
    ),
    "create_page": (
        "Create a page or draft with complete storage XHTML. Returns page ID, version and URL. Never automatically retried.",
        {"space_id": text("Numeric space ID from list_spaces."), "title": text("Page title."),
         "body": BODY, "parent_id": PAGE_ID, "status": STATUS}, ["space_id", "title", "body"],
    ),
    "update_page": (
        "Replace the complete page body after checking expected_version. Published pages increment the version and detect concurrent conflicts; drafts use fixed revision 1, so the precheck cannot protect against concurrent draft edits. Never automatically retried.",
        {"page_id": PAGE_ID, "title": text("Complete desired title."), "body": BODY,
         "expected_version": {"type": "integer", "minimum": 0, "description": "Version observed in the page you read, including 0 for a draft when returned by the provider; never guess."},
         "status": STATUS, "version_message": text("Optional edit summary.")},
        ["page_id", "title", "body", "expected_version"],
    ),
    "list_comments": (
        "Read root footer/inline comments on a page or children of one comment, including storage bodies. Root results do not include all nested replies.",
        {"page_id": PAGE_ID, "parent_comment_id": text("Optional parent comment ID; selects its children instead of page roots."),
         "kind": {"type": "string", "enum": ["footer", "inline"], "default": "footer"}, **PAGINATION}, [],
    ),
    "add_comment": (
        "Create a footer comment on a page or reply to a footer comment. Supply exactly one target. Never automatically retried.",
        {"body": BODY, "page_id": PAGE_ID, "parent_comment_id": text("Footer comment ID to reply to, instead of page_id.")}, ["body"],
    ),
    "list_attachments": (
        "List attachment metadata for a page, including IDs and original names; returns explicit pagination.",
        {"page_id": PAGE_ID, **PAGINATION}, ["page_id"],
    ),
    "download_attachment": (
        "Download one attachment into a unique skill-state job directory; returns file path, byte count and SHA-256. Signed CDN hops receive no API credentials.",
        {"page_id": PAGE_ID, "attachment_id": text("Attachment ID from list_attachments, numeric or att-prefixed."),
         "filename": text("Optional plain output filename, including extension; not a path.")}, ["page_id", "attachment_id"],
    ),
    "upload_attachment": (
        "Create a new page attachment from an explicitly selected local file. Does not overwrite an existing attachment or retry an ambiguous upload.",
        {"page_id": PAGE_ID, "file_path": text("Path of the local regular file to upload."),
         "comment": text("Optional attachment version comment."), "minor_edit": {"type": "boolean", "default": False}},
        ["page_id", "file_path"],
    ),
}


def _handler(api: Any, operation: str):
    def run(**kwargs: Any) -> str:
        try:
            config = Config.from_settings(api.get_settings(SETTINGS_KEYS))
            with ConfluenceClient(config) as client:
                result = getattr(Operations(client, Path(api.get_state_dir())), operation)(**kwargs)
        except ConfluenceError as exc:
            result = exc.result()
        except OSError as exc:
            result = ConfluenceError("local_io_error", "Could not read or write the selected local file.",
                                     exception_type=type(exc).__name__).result()
        except (ValueError, TypeError):
            result = ConfluenceError("invalid_argument", "Arguments or settings do not match the tool schema.").result()
        return json.dumps(result, ensure_ascii=False)
    return run


def register(api: Any) -> None:
    for name, (description, properties, required) in TOOLS.items():
        api.register_tool(
            name, _handler(api, name), description=description,
            schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            timeout_sec=180,
        )
