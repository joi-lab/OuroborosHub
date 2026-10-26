"""Observable mail facts; attention remains the host's and the mind's choice."""
import re
from email import message_from_bytes
from email.policy import default
from pathlib import Path

from .mime import extract_body


def mailbox_identity(account):
    entry = {"id": account, "label": account, "kind": "user"}
    return {"sends_as": entry, "reads_as": [dict(entry)]}


def email_facts(item, account, words=()):
    headers = item.context.get("headers") or {}
    precedence = {value.strip().casefold() for value in headers.get("precedence", [])}
    bulk = bool(precedence & {"bulk", "list", "junk"})
    facts = ["any_message"]
    recipients = (*item.recipients, *item.cc, *item.context.get("cc", []))
    if (account and account.casefold() in {value.casefold() for value in recipients}
            and not any(name in headers for name in ("list-id", "list-unsubscribe")) and not bulk):
        facts.append("direct_message")
    local = item.sender.partition("@")[0].casefold()
    if (bulk or any(value.strip().casefold() != "no" for value in headers.get("auto-submitted", []))
            or re.fullmatch(r"(?:no[-_.]?reply|do[-_.]?not[-_.]?reply)(?:\+.*)?", local)):
        facts.append("from_bot")
    if words:
        body = item.body
        source = item.context.get("source_artifact") or {}
        if source.get("path"):
            # Match the retained body, including text past the displayed excerpt.
            message = message_from_bytes(Path(source["path"]).read_bytes(), policy=default)
            body = extract_body(message, max_chars=None)
        text = (item.subject + "\n" + body).casefold()
        if any(re.search(r"(?<!\w)" + re.escape(word.casefold()) + r"(?!\w)", text) for word in words if word):
            facts.append("signal.words")
    return facts
