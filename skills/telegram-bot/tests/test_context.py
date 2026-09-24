"""Context supplied in one provider update survives durable Host delivery."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_bot.custody import CustodyStore
from telegram_bot.events import parse_telegram_update
from telegram_bot.host import PresenceHostClient


def _update():
    return {
        "update_id": 77,
        "message": {
            "message_id": 51,
            "date": 1_700_000_051,
            "message_thread_id": 7,
            "is_topic_message": True,
            "from": {"id": 10, "first_name": "Rowan", "username": "rowan"},
            "chat": {"id": -40, "type": "supergroup", "title": "Workshop"},
            "text": "🙂 Open this with Mira",
            "entities": [
                {
                    "type": "text_link",
                    "offset": 3,
                    "length": 9,
                    "url": "https://example.org/source",
                },
                {
                    "type": "text_mention",
                    "offset": 18,
                    "length": 4,
                    "user": {"id": 11, "first_name": "Mira"},
                },
            ],
            "reply_to_message": {
                "message_id": 50,
                "date": 1_700_000_050,
                "message_thread_id": 7,
                "from": {"id": 11, "first_name": "Mira", "last_name": "Example"},
                "chat": {"id": -40, "type": "supergroup", "title": "Workshop"},
                "text": "Please review the draft before Friday.",
                "entities": [{"type": "bold", "offset": 18, "length": 5}],
            },
            "quote": {
                "text": "before Friday",
                "position": 24,
                "is_manual": True,
                "entities": [{"type": "italic", "offset": 7, "length": 6}],
            },
        },
    }


def test_reply_quote_and_entities_survive_custody_and_host(tmp_path):
    async def check():
        update = _update()
        original = deepcopy(update)
        event = parse_telegram_update(update, bot_account_id="900")
        store = CustodyStore(tmp_path / "custody.sqlite3")
        assert store.commit_update(77, event)
        # Use a new connection owner, as after a worker restart.
        lease = CustodyStore(store.path).claim_inbox()
        (tmp_path / "settings.json").write_text(
            json.dumps({"binding_id": "a" * 32}), encoding="utf-8"
        )
        request = AsyncMock(
            return_value={
                "status": "completed",
                "outcome": "silent",
                "text": "",
            }
        )
        host = PresenceHostClient(
            state_dir=tmp_path,
            token_provider=lambda: "test-token",
            host_base="http://127.0.0.1:8767",
            transport=SimpleNamespace(request_json=request),
        )
        await host.submit(lease.payload, [])
        sent = request.call_args.kwargs["payload"]["event"]
        assert set(sent) == set(event.to_dict())
        assert sent["actor"]["platform_actor_id"] == "10"
        assert sent["actor"]["first_name"] == "Rowan"
        assert sent["conversation"]["title"] == "Workshop"
        assert sent["thread_id"] == "7"
        assert sent["message"]["reply_to_message_id"] == 50
        assert (
            sent["message"]["reply_to_message"]
            == original["message"]["reply_to_message"]
        )
        assert sent["message"]["quote"] == original["message"]["quote"]
        assert sent["message"]["entities"] == original["message"]["entities"]
        assert sent["text"] == original["message"]["text"]
        assert update == original

    asyncio.run(check())


def test_reply_caption_preserves_author_entities_and_media_without_nested_chain():
    update = _update()
    reply = update["message"]["reply_to_message"]
    reply.pop("text")
    reply.pop("entities")
    reply.update(
        {
            "caption": "🙂 Full draft",
            "caption_entities": [
                {
                    "type": "text_link",
                    "offset": 3,
                    "length": 10,
                    "url": "https://example.org/draft",
                }
            ],
            "sender_chat": {"id": -70, "type": "channel", "title": "Public notes"},
            "document": {
                "file_id": "doc1",
                "file_unique_id": "unique1",
                "file_name": "draft.pdf",
                "mime_type": "application/pdf",
                "file_size": 321,
            },
            "reply_to_message": {"message_id": 49, "text": "Do not expand a chain"},
        }
    )
    event = parse_telegram_update(update, bot_account_id="900")
    snapshot = event.message["reply_to_message"]
    assert snapshot["caption"] == "🙂 Full draft"
    assert snapshot["caption_entities"] == reply["caption_entities"]
    assert snapshot["from"] == reply["from"]
    assert snapshot["sender_chat"] == reply["sender_chat"]
    assert snapshot["attachments"] == [{"kind": "document", **reply["document"]}]
    assert "reply_to_message" not in snapshot
    assert event.message["attachments"] == []
    # Provider objects are copied, so a later input mutation cannot rewrite custody.
    reply["from"]["first_name"] = "Changed"
    assert snapshot["from"]["first_name"] == "Mira"


@pytest.mark.parametrize(
    "origin",
    [
        {
            "type": "user",
            "date": 1_700_000_010,
            "sender_user": {"id": 12, "first_name": "Sam"},
        },
        {
            "type": "hidden_user",
            "date": 1_700_000_010,
            "sender_user_name": "Hidden sender",
        },
        {
            "type": "channel",
            "date": 1_700_000_010,
            "chat": {"id": -80, "type": "channel", "title": "Updates"},
            "message_id": 18,
            "author_signature": "Editor",
        },
    ],
)
def test_forward_origin_remains_distinct_from_current_sender(origin):
    update = _update()
    update["message"]["forward_origin"] = origin
    event = parse_telegram_update(update, bot_account_id="900")
    assert event.message["forward_origin"] == origin
    assert event.actor["platform_actor_id"] == "10"
    assert event.conversation_id == "-40"
    assert event.message["message_id"] == 51


def test_direct_caption_entities_and_long_text_are_not_trimmed():
    update = _update()
    update["message"].pop("text")
    update["message"].pop("entities")
    update["message"]["caption"] = "🙂 Draft"
    entities = [
        {
            "type": "text_link",
            "offset": 3,
            "length": 5,
            "url": "https://example.org/draft",
        }
    ]
    update["message"]["caption_entities"] = entities
    event = parse_telegram_update(update, bot_account_id="900")
    assert event.text == "🙂 Draft"
    assert event.message["caption_entities"] == entities
    update["message"]["text"] = "я" * 4096
    update["message"]["reply_to_message"]["text"] = "ю" * 4096
    event = parse_telegram_update(update, bot_account_id="900")
    assert event.text == "я" * 4096
    assert event.message["reply_to_message"]["text"] == "ю" * 4096
