from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import sqlite3

import pytest

from lib.events import parse_socket_envelope
from lib.socket_mode import SocketModeClient
from lib.store import BridgeStore


def _message():
    return {
        "type": "message", "user": "U_ACTOR", "ts": "123.456",
        "text": "Please check this document.", "blocks": [],
        "language": {"locale": "en", "is_reliable": True},
    }


def _envelope(event_id, message, *, previous=None):
    event = {**deepcopy(message), "channel": "C_ROOM", "channel_type": "channel"}
    if previous is not None:
        event = {
            "type": "message", "subtype": "message_changed",
            "channel": "C_ROOM", "channel_type": "channel",
            "message": deepcopy(message), "previous_message": deepcopy(previous),
        }
    return {
        "type": "events_api", "envelope_id": f"env-{event_id}",
        "payload": {"team_id": "T_WORKSPACE", "event_id": event_id, "event": event},
    }


@pytest.mark.parametrize("change", ["language", "edited", "both", "identical"])
def test_bookkeeping_only_revision_does_not_create_another_turn(change):
    previous = _message()
    current = deepcopy(previous)
    if change in {"language", "both"}:
        current["language"]["locale"] = "ru"
    if change in {"edited", "both"}:
        current["edited"] = {"user": "U_ACTOR", "ts": "124.0"}

    original = parse_socket_envelope(_envelope("Ev-original", previous))
    update = _envelope("Ev-classification", current, previous=previous)
    before = deepcopy(update)
    parsed = parse_socket_envelope(update)

    assert original.accepted
    assert not parsed.accepted and parsed.event is None
    assert parsed.reason == "message_content_unchanged"
    assert parsed.event_id == "Ev-classification"
    assert update == before


@pytest.mark.parametrize("field,old,new", [
    ("text", "first question", "corrected question"),
    ("text", "removed text", ""),
    ("blocks", [{"type": "section", "text": {"type": "plain_text", "text": "old"}}],
     [{"type": "section", "text": {"type": "plain_text", "text": "new"}}]),
    ("blocks", [{"type": "divider"}], []),
    ("attachments", [{"text": "old attachment"}], [{"text": "new attachment"}]),
    ("attachments", [{"text": "attachment"}], []),
    ("files", [{"id": "F1", "url_private": "https://files.slack.com/first"}],
     [{"id": "F2", "url_private": "https://files.slack.com/second"}]),
    ("files", [{"id": "F1", "url_private": "https://files.slack.com/first"}], []),
    ("future_content", {"value": "old"}, {"value": "new"}),
    ("user", "U_ACTOR", "U_OTHER"),
])
def test_real_changes_with_same_timestamp_still_reach_the_model(field, old, new):
    previous = _message()
    previous[field] = old
    current = deepcopy(previous)
    current[field] = new
    current["language"]["locale"] = "ru"
    current["edited"] = {"user": "U_ACTOR", "ts": "124.0"}

    parsed = parse_socket_envelope(_envelope("Ev-edit", current, previous=previous))

    assert parsed.accepted and parsed.event is not None
    assert parsed.event.message_ts == previous["ts"]
    assert parsed.event.structured["message"][field] == new
    assert parsed.event.structured["previous_message"][field] == old


@pytest.mark.parametrize("previous", [None, {}, {"ts": "123.456", "user": "U_ACTOR"}])
def test_missing_comparison_content_is_not_treated_as_unchanged(previous):
    envelope = _envelope("Ev-edit", _message(), previous={})
    envelope["payload"]["event"]["previous_message"] = previous
    assert parse_socket_envelope(envelope).accepted


def test_different_message_timestamp_and_blocks_only_content():
    previous = _message()
    current = {**previous, "ts": "125.0"}
    assert parse_socket_envelope(_envelope("Ev-other", current, previous=previous)).accepted

    previous.pop("text")
    previous["blocks"] = [{"type": "section", "text": {"type": "plain_text", "text": "visible"}}]
    current = deepcopy(previous)
    current["language"]["locale"] = "ru"
    assert parse_socket_envelope(_envelope("Ev-blocks", current, previous=previous)).reason == "message_content_unchanged"

    partial = {"user": "U_ACTOR", "ts": "123.456"}
    assert parse_socket_envelope(_envelope("Ev-partial", partial, previous=partial)).accepted


@pytest.mark.parametrize("revision_first", [False, True])
def test_language_update_is_durable_before_ack_but_only_original_is_claimable(tmp_path, revision_first):
    store = BridgeStore(tmp_path)
    previous = _message()
    current = deepcopy(previous)
    current["language"]["locale"] = "ru"
    original = _envelope("Ev-original", previous)
    update = _envelope("Ev-language", current, previous=previous)
    envelopes = [update, original] if revision_first else [original, update]
    retry = deepcopy(update)
    retry["envelope_id"] = "env-language-retry"
    envelopes.append(retry)
    acknowledged = []

    class WebSocket:
        async def send(self, raw):
            ack = json.loads(raw)
            sent = next(e for e in envelopes if e["envelope_id"] == ack["envelope_id"])
            event_id = sent["payload"]["event_id"]
            with sqlite3.connect(store.path) as db:
                row = db.execute("SELECT state,ignored_reason,raw_json FROM inbox WHERE event_id=?", (event_id,)).fetchone()
            assert row is not None
            if event_id == "Ev-language":
                assert row[:2] == ("ignored", "message_content_unchanged")
                assert json.loads(row[2]) == update
            else:
                assert row[0] == "pending"
            acknowledged.append(ack)

    async def receive():
        socket = SocketModeClient(object(), store, bot_user_id="U_BOT")
        for envelope in envelopes:
            await socket.handle_raw_message(WebSocket(), json.dumps(envelope))

    asyncio.run(receive())
    assert len(acknowledged) == 3
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 2
    claimed = store.claim_inbox()
    assert claimed.event_id == "Ev-original"
    store.complete_inbox(claimed.row_id, claimed.lease_token)
    assert store.claim_inbox() is None
