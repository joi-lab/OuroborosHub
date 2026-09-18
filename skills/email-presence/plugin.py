"""Email tools and configuration; the companion owns automatic intake/delivery."""
from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

from starlette.responses import JSONResponse

from .lib.client import MailClient
from .lib.host_adapter import HostContractError, normalize_binding_id
from .lib.store import EmailStore
from .lib.delivery import tool_origin

SETTING_KEYS = ["EMAIL_IMAP_HOST", "EMAIL_IMAP_PORT", "EMAIL_SMTP_HOST", "EMAIL_SMTP_PORT",
                "EMAIL_USER", "EMAIL_PASSWORD", "EMAIL_DEFAULT_FOLDER", "EMAIL_AUTH_MODE",
                "EMAIL_OAUTH_ACCESS_TOKEN", "EMAIL_OAUTH_REFRESH_TOKEN", "EMAIL_OAUTH_CLIENT_ID",
                "EMAIL_OAUTH_CLIENT_SECRET"]


def _load(api):
    path = Path(api.get_state_dir()) / "settings.json"
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Email settings must be a JSON object")
    return value


def register(api):
    state = Path(api.get_state_dir())
    api.register_companion_process("email_poller")

    def store():
        return EmailStore(state)

    def client():
        return MailClient(api.get_settings(SETTING_KEYS))

    def send(ctx=None, *, to, body, subject="", reply_to_message_id="", references=None, request_id=""):
        recipients = [x.strip() for x in str(to).split(",") if x.strip()]
        if not recipients or not str(body).strip():
            raise ValueError("to and body are required")
        rid = request_id or uuid.uuid4().hex
        inserted = store().enqueue_outbox(request_id=rid, recipients=recipients, subject=subject,
                                          body=body, in_reply_to=reply_to_message_id, references=references or [],
                                          reporting={"version": -1, "origin": tool_origin(ctx)})
        return {"ok": True, "request_id": rid, "deduplicated": not inserted, "receipt": store().receipt(rid)}

    def search(**kwargs):
        kwargs.setdefault("folder", client().settings.get("EMAIL_DEFAULT_FOLDER") or "INBOX")
        return client().search(**kwargs)

    def read(**kwargs):
        kwargs.setdefault("folder", client().settings.get("EMAIL_DEFAULT_FOLDER") or "INBOX")
        return client().read(**kwargs)

    string = {"type": "string"}
    integer = {"type": "integer"}
    array = {"type": "array", "items": string}

    def tool(name, handler, description, properties, required=()):
        api.register_tool(name, handler, description=description,
                          schema={"type": "object", "properties": properties, "required": list(required)},
                          timeout_sec=120)

    tool("email_send", send, "Queue a text email or threaded reply. Reuse request_id to deduplicate; query email_receipt for delivery.",
         {"to": string, "body": string, "subject": string, "reply_to_message_id": string,
          "references": array, "request_id": string}, ("to", "body"))
    tool("email_search", search, "Search all mail, including mail before activation. IMAP criteria tokens, e.g. ['UNSEEN'], ['FROM', '\"alice@example.org\"'], ['HEADER', 'Message-ID', '\"<id>\"']. Returns stable UID and UIDVALIDITY.",
         {"folder": string, "criteria": array, "limit": integer})
    tool("email_read", read, "Read any old or new message using UID and UIDVALIDITY from email_search. Peeks unless mark_as_read is true.",
         {"uid": integer, "uidvalidity": integer, "folder": string, "mark_as_read": {"type": "boolean"}}, ("uid", "uidvalidity"))
    tool("email_mailbox", lambda **kw: client().mailbox(**kw),
         "List/create folders, copy/move a message, or add/remove flags (Seen, Flagged, Deleted). Move requires server UID MOVE. No global expunge.",
         {"action": {"type": "string", "enum": ["list", "create", "copy", "move", "flags"]},
          "folder": string, "uid": integer, "uidvalidity": integer, "destination": string,
          "flags": array, "remove_flags": {"type": "boolean"}})
    tool("email_draft", lambda **kw: client().draft(**kw),
         "Save a text draft without sending it. Choose the real draft folder from email_mailbox list.",
         {"to": string, "subject": string, "body": string, "folder": string,
          "reply_to_message_id": string, "references": array}, ("to", "subject", "body"))
    tool("email_receipt", lambda request_id: store().receipt(request_id),
         "Get queued/completed/failed/uncertain email delivery and its stable Message-ID. Uncertain SMTP acceptance requires inspection before any new send.",
         {"request_id": string}, ("request_id",))
    tool("email_test_connection", lambda: client().test_connection(),
         "Check IMAP/SMTP TLS and authentication without sending or ingesting messages.", {})

    async def status(_request=None):
        settings = api.get_settings(SETTING_KEYS)
        payload = store().status()
        payload.update(binding_id_configured=bool(_load(api).get("binding_id")),
                       imap_host_configured=bool(settings.get("EMAIL_IMAP_HOST")),
                       smtp_host_configured=bool(settings.get("EMAIL_SMTP_HOST")),
                       poll_health=store().get("poll_health"))
        return JSONResponse(payload)

    async def save(request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected JSON object")
            current = _load(api)
            if "binding_id" in body:
                current["binding_id"] = normalize_binding_id(body["binding_id"])
            if "poll_interval_sec" in body:
                current["poll_interval_sec"] = max(5, min(3600, int(body["poll_interval_sec"])))
            if "folder" in body:
                current["folder"] = str(body["folder"]).strip() or "INBOX"
        except (ValueError, TypeError, HostContractError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        state.mkdir(parents=True, exist_ok=True)
        temporary = state / f"settings.tmp.{time.time_ns()}"
        temporary.write_text(json.dumps(current, indent=2) + "\n")
        temporary.replace(state / "settings.json")
        return JSONResponse({"ok": True, "message": "Saved. Toggle the skill to restart its companion."})

    api.register_route("status", status, methods=("GET",))
    api.register_route("settings/save", save, methods=("POST",))
    api.register_ui_tab("email_presence", title="Email Presence", render={
        "kind": "declarative", "schema_version": 1, "components": [
            {"type": "action", "target": "status", "route": "status", "method": "GET", "label": "Refresh delivery status", "fields": []},
            {"type": "status", "target": "status", "idle": "Refresh to inspect mailbox intake and delivery receipts.", "loading": "Reading local status…", "success": "Status loaded.", "error": "Status unavailable."},
            {"type": "json", "target": "status"}]})
    api.register_settings_section("email_presence", title="Email Presence", schema={"components": [
        {"type": "markdown", "text": "Configure granted IMAP/SMTP credentials and a Presence binding. First connection records the intake baseline; older mail remains accessible through tools."},
        {"type": "form", "route": "settings/save", "method": "POST", "submit_label": "Save email settings", "fields": [
            {"name": "binding_id", "label": "Presence Binding ID", "type": "text"},
            {"name": "folder", "label": "Intake folder", "type": "text", "placeholder": "INBOX"},
            {"name": "poll_interval_sec", "label": "Maximum idle poll interval (seconds)", "type": "number", "placeholder": "30"}]}]})
