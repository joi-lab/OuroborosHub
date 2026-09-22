from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class SlackFile:
    file_id: str
    name: str
    mimetype: str
    size: int
    url_private: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "mimetype": self.mimetype,
            "size": self.size,
            "url_private": self.url_private,
        }


@dataclass(frozen=True)
class SlackEvent:
    envelope_id: str
    event_id: str
    team_id: str
    enterprise_id: str
    event_type: str
    subtype: str
    actor_user_id: str
    actor_team_id: str
    channel_id: str
    channel_type: str
    message_ts: str
    thread_ts: str
    event_ts: str
    client_msg_id: str
    text: str
    files: tuple[SlackFile, ...]
    structured: dict[str, Any]

    @property
    def root_thread_ts(self) -> str:
        return self.thread_ts or self.message_ts

    @property
    def ordering_key(self) -> str:
        return f"{self.team_id}:{self.channel_id}:{self.root_thread_ts}"


@dataclass(frozen=True)
class ParsedEnvelope:
    envelope_id: str
    event_id: str
    accepted: bool
    reason: str
    event: SlackEvent | None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _files(raw: Any) -> tuple[SlackFile, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    parsed: list[SlackFile] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        file_id = _text(item.get("id"))
        url_private = _text(item.get("url_private_download") or item.get("url_private"))
        if not file_id or not url_private:
            continue
        parsed.append(
            SlackFile(
                file_id=file_id,
                name=_text(item.get("name") or item.get("title") or file_id),
                mimetype=_text(item.get("mimetype") or "application/octet-stream"),
                size=_int(item.get("size")),
                url_private=url_private,
            )
        )
    return tuple(parsed)


def parse_socket_envelope(
    payload: Mapping[str, Any],
    *,
    bot_user_id: str = "",
    bot_id: str = "",
    app_id: str = "",
) -> ParsedEnvelope:
    """Parse one Socket Mode envelope without adding policy or prompt text.

    Unsupported envelopes still return a stable classification so the caller can
    durably record them before acknowledging Slack.
    """

    envelope_id = _text(payload.get("envelope_id"))
    wrapper = payload.get("payload")
    wrapper = wrapper if isinstance(wrapper, Mapping) else {}
    event_id = _text(wrapper.get("event_id"))
    if _text(payload.get("type")) != "events_api":
        return ParsedEnvelope(envelope_id, event_id, False, "not_events_api", None)

    event = wrapper.get("event")
    event = event if isinstance(event, Mapping) else {}
    event_type = _text(event.get("type"))
    subtype = _text(event.get("subtype"))
    # Slack wraps changed messages under event.message and deleted messages
    # under previous_message. Normalize only provider shape, retaining both
    # nested objects in structured facts for the model and receipts.
    nested = event.get("message") if isinstance(event.get("message"), Mapping) else {}
    previous = event.get("previous_message") if isinstance(event.get("previous_message"), Mapping) else {}
    message = nested if subtype == "message_changed" else event
    actor_user_id = _text(message.get("user") or event.get("user") or previous.get("user"))
    bot_event_id = _text(message.get("bot_id") or event.get("bot_id") or previous.get("bot_id"))
    app_event_id = _text(message.get("app_id") or event.get("app_id") or previous.get("app_id"))
    if not actor_user_id and bot_event_id:
        actor_user_id = bot_event_id
    if not actor_user_id and app_event_id:
        actor_user_id = app_event_id
    item = event.get("item") if isinstance(event.get("item"), Mapping) else {}
    channel_id = _text(event.get("channel") or message.get("channel") or previous.get("channel") or item.get("channel"))
    message_ts = _text(message.get("ts") or event.get("deleted_ts") or item.get("ts") or event.get("ts") or previous.get("ts"))
    files = _files(message.get("files") or event.get("files"))
    structured: dict[str, Any] = {}
    blocks = message.get("blocks") or event.get("blocks")
    if isinstance(blocks, Sequence) and not isinstance(blocks, (str, bytes, bytearray)):
        structured["blocks"] = [dict(item) for item in blocks if isinstance(item, Mapping)]
    if subtype == "message_changed":
        structured.update({"change": "edited", "message": dict(message), "previous_message": dict(previous)})
    elif subtype == "message_deleted":
        structured.update({"change": "deleted", "deleted_ts": _text(event.get("deleted_ts")), "previous_message": dict(previous)})
    if event_type in {"reaction_added", "reaction_removed"}:
        structured["reaction"] = {
            "kind": "added" if event_type == "reaction_added" else "removed",
            "name": _text(event.get("reaction")),
            "user_id": actor_user_id,
            "item": dict(event.get("item")) if isinstance(event.get("item"), Mapping) else {},
        }
    if event_type not in {"message", "reaction_added", "reaction_removed"}:
        return ParsedEnvelope(envelope_id, event_id, False, "unsupported_event", None)
    if ((bot_id and bot_event_id == bot_id) or (app_id and app_event_id == app_id)
            or (bot_user_id and actor_user_id == bot_user_id)):
        return ParsedEnvelope(envelope_id, event_id, False, "self_message", None)
    if not actor_user_id:
        return ParsedEnvelope(envelope_id, event_id, False, "missing_actor_provenance", None)
    if not channel_id or not message_ts:
        return ParsedEnvelope(
            envelope_id, event_id, False, "missing_message_provenance", None
        )
    text = str(message.get("text") or event.get("text") or "")
    if not text and not files and not structured:
        return ParsedEnvelope(envelope_id, event_id, False, "empty_message", None)

    channel_type = _text(event.get("channel_type"))
    parsed = SlackEvent(
        envelope_id=envelope_id,
        event_id=event_id,
        team_id=_text(wrapper.get("team_id") or event.get("team")),
        enterprise_id=_text(wrapper.get("enterprise_id") or event.get("enterprise")),
        event_type=event_type,
        subtype=subtype,
        actor_user_id=actor_user_id,
        actor_team_id=_text(event.get("user_team") or event.get("team")),
        channel_id=channel_id,
        channel_type=channel_type,
        message_ts=message_ts,
        thread_ts=_text(event.get("thread_ts") or message.get("thread_ts") or item.get("thread_ts")),
        event_ts=_text(event.get("event_ts") or wrapper.get("event_time")),
        client_msg_id=_text(event.get("client_msg_id")),
        text=text,
        files=files,
        structured=structured,
    )
    return ParsedEnvelope(envelope_id, event_id, True, "accepted", parsed)
