from __future__ import annotations

from copy import deepcopy

import pytest

from lib.events import parse_socket_envelope
from lib.host_adapter import slack_presence_event
from lib.store import BridgeStore


def _message(**fields):
    return {"type": "message", "user": "U_AUTHOR", "ts": "123.456",
            "text": "A shared update", **fields}


def _envelope(message, *, previous=None):
    event = {**message, "channel": "C_ROOM", "channel_type": "channel"}
    if previous is not None:
        event = {"type": "message", "subtype": "message_changed", "channel": "C_ROOM",
                 "message": message, "previous_message": previous}
    return {"type": "events_api", "envelope_id": "env-1",
            "payload": {"team_id": "T_WORKSPACE", "event_id": "Ev-1", "event": event}}


@pytest.mark.parametrize("self_id", ["U_SELF", ""])
@pytest.mark.parametrize("text,mentions", [
    ("<@U_OTHER> please check this", ["U_OTHER"]),
    ("<@U_SELF> please check this", ["U_SELF"]),
    ("A shared update without mentions", []),
])
def test_conversation_facts_survive_custody_without_filtering_speech(tmp_path, self_id, text, mentions):
    payload = _envelope(_message(text=text, thread_ts="100.0", parent_user_id="U_PARENT"))
    original = deepcopy(payload)
    parsed = parse_socket_envelope(payload, bot_user_id=self_id)
    assert parsed.accepted and payload == original
    store = BridgeStore(tmp_path)
    store.ingest_envelope(payload, parsed)
    item = BridgeStore(tmp_path).claim_inbox()
    event = slack_presence_event(item)
    assert set(event) == {"source_event_id", "provider", "account_id", "conversation_id",
                          "thread_id", "conversation_key", "actor", "conversation", "message", "text"}
    assert event["text"] == text and event["thread_id"] == "100.0"
    assert event["actor"]["platform_actor_id"] == "U_AUTHOR"
    assert event["message"]["provider_facts"] == {
        **({"self_user_id": self_id} if self_id else {}),
        **({"mentioned_user_ids": mentions} if mentions else {}),
        "parent_user_id": "U_PARENT",
    }


def test_rich_text_mentions_and_text_tokens_are_occurrences_not_name_matching():
    blocks = [{"type": "rich_text", "elements": [{"type": "rich_text_quote", "elements": [
        {"type": "user", "user_id": "U_BLOCK"},
        {"type": "user", "user_id": "U_TEXT"},
        {"type": "text", "text": "U_NAME and @DisplayName"},
    ]}]}]
    payload = _envelope(_message(text="Quoted `<@U_TEXT>`; <@U_TEXT|old label>", blocks=blocks))
    original = deepcopy(payload)
    parsed = parse_socket_envelope(payload)
    assert parsed.accepted and payload == original
    assert parsed.event.structured == {"blocks": blocks, "mentioned_user_ids": ["U_BLOCK", "U_TEXT"]}


@pytest.mark.parametrize("current,expected", [
    (_message(text="<@U_NEW> revised", parent_user_id="U_PARENT_NEW"),
     {"mentioned_user_ids": ["U_NEW"], "parent_user_id": "U_PARENT_NEW"}),
    (_message(text="Revised without mention or parent"), {}),
])
def test_edit_facts_describe_current_message_without_borrowing_previous(current, expected):
    previous = _message(text="<@U_OLD> first", parent_user_id="U_PARENT_OLD")
    payload = _envelope(current, previous=previous)
    # Outer update metadata must not masquerade as the current nested message.
    payload["payload"]["event"].update(text="<@U_WRAPPER>", parent_user_id="U_WRAPPER_PARENT")
    original = deepcopy(payload)
    parsed = parse_socket_envelope(payload, bot_user_id="U_SELF")
    assert parsed.accepted and payload == original
    assert parsed.event.structured == {
        "change": "edited", "message": current, "previous_message": previous,
        "self_user_id": "U_SELF", **expected,
    }


def test_self_facts_do_not_change_empty_self_or_noop_admission():
    empty = parse_socket_envelope(_envelope(_message(text="")), bot_user_id="U_SELF")
    assert not empty.accepted and empty.reason == "empty_message"
    own = parse_socket_envelope(_envelope(_message(user="U_SELF")), bot_user_id="U_SELF")
    assert not own.accepted and own.reason == "self_message"
    previous = _message(text="<@U_SELF> check this")
    current = {**previous, "language": {"locale": "en"}}
    noop = parse_socket_envelope(_envelope(current, previous=previous), bot_user_id="U_SELF")
    assert not noop.accepted and noop.reason == "message_content_unchanged"
    blocks = [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
        {"type": "user", "user_id": "U_OTHER"},
    ]}]}]
    visible = parse_socket_envelope(_envelope(_message(text="", blocks=blocks)), bot_user_id="U_SELF")
    assert visible.accepted
    assert visible.event.structured == {
        "blocks": blocks, "self_user_id": "U_SELF", "mentioned_user_ids": ["U_OTHER"],
    }


@pytest.mark.parametrize("kind", ["message_deleted", "reaction_added", "reaction_removed"])
def test_updates_do_not_invent_current_message_mentions_or_parent(kind):
    if kind == "message_deleted":
        message = {"type": "message", "subtype": kind, "deleted_ts": "123.456",
                   "previous_message": _message(text="<@U_PREVIOUS>", parent_user_id="U_PARENT")}
        expected = {"change": "deleted", "deleted_ts": "123.456",
                    "previous_message": message["previous_message"], "self_user_id": "U_SELF"}
    else:
        message = {"type": kind, "user": "U_AUTHOR", "reaction": "wave",
                   "item": {"type": "message", "channel": "C_ROOM", "ts": "123.456"}}
        expected = {"reaction": {"kind": "added" if kind == "reaction_added" else "removed",
                                 "name": "wave", "user_id": "U_AUTHOR", "item": message["item"]},
                    "self_user_id": "U_SELF"}
    parsed = parse_socket_envelope(_envelope(message), bot_user_id="U_SELF")
    assert parsed.accepted and parsed.event.structured == expected


def test_old_persisted_events_keep_unknown_facts(tmp_path):
    payload = _envelope(_message())
    store = BridgeStore(tmp_path)
    store.ingest_envelope(payload, parse_socket_envelope(payload))
    event = slack_presence_event(BridgeStore(tmp_path).claim_inbox())
    assert event["message"]["provider_facts"] == {}
    assert event["text"] == "A shared update"
