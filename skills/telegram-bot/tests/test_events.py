from telegram_bot.events import parse_telegram_update


def test_parses_exact_actor_chat_topic_message_and_media_provenance():
    event = parse_telegram_update(
        {
            "update_id": 77,
            "message": {
                "message_id": 9,
                "message_thread_id": 42,
                "date": 1_700_000_000,
                "caption": "See both files",
                "reply_to_message": {"message_id": 8},
                "from": {
                    "id": 123,
                    "is_bot": False,
                    "username": "reader",
                    "first_name": "R",
                    "last_name": "D",
                    "language_code": "en",
                },
                "chat": {"id": -456, "type": "supergroup", "title": "Community"},
                "photo": [
                    {"file_id": "small", "file_unique_id": "p1", "file_size": 10},
                    {"file_id": "large", "file_unique_id": "p2", "file_size": 20},
                ],
                "document": {
                    "file_id": "doc",
                    "file_unique_id": "d1",
                    "file_name": "notes.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 30,
                },
            },
        },
        bot_account_id="900",
    )

    assert event is not None
    assert event.source_event_id == "telegram:900:77"
    assert event.account_id == "900"
    assert event.conversation_id == "-456"
    assert event.thread_id == "42"
    assert event.conversation_key == "telegram:900:-456:42"
    assert event.actor["platform_actor_id"] == "123"
    assert event.conversation["topic_id"] == 42
    assert event.message["message_id"] == 9
    assert event.message["sent_at_epoch"] == 1_700_000_000
    assert event.message["reply_to_message_id"] == 8
    assert [item["file_id"] for item in event.message["attachments"]] == [
        "large",
        "doc",
    ]
    assert set(event.to_dict()) == {
        "source_event_id",
        "provider",
        "account_id",
        "conversation_id",
        "thread_id",
        "conversation_key",
        "actor",
        "conversation",
        "message",
        "text",
    }
    assert event.to_dict()["text"] == "See both files"


def test_ignores_bot_edits_service_and_empty_messages():
    base = {
        "update_id": 1,
        "message": {
            "message_id": 2,
            "from": {"id": 3, "is_bot": False},
            "chat": {"id": 4, "type": "private"},
        },
    }
    assert parse_telegram_update(base, bot_account_id="9") is None
    bot = {
        **base,
        "message": {**base["message"], "text": "x", "from": {"id": 3, "is_bot": True}},
    }
    assert parse_telegram_update(bot, bot_account_id="9") is None
    edited = {"update_id": 1, "edited_message": {**base["message"], "text": "x"}}
    parsed_edit = parse_telegram_update(edited, bot_account_id="9")
    assert parsed_edit is not None
    assert parsed_edit.message["event_kind"] == "edited_message"
    reaction = {
        "update_id": 2,
        "message_reaction": {
            "user": {"id": 3, "is_bot": False},
            "chat": {"id": 4, "type": "private"},
            "message_id": 2,
            "old_reaction": [],
            "new_reaction": [{"type": "emoji", "emoji": "👍"}],
        },
    }
    parsed_reaction = parse_telegram_update(reaction, bot_account_id="9")
    assert parsed_reaction is not None
    assert parsed_reaction.message["event_kind"] == "message_reaction"
    assert parsed_reaction.text == ""


def test_configured_group_is_context_not_owner_authority():
    update = {
        "update_id": 8,
        "message": {
            "message_id": 12,
            "text": "/restart is conversation text",
            "from": {"id": 30, "is_bot": False},
            "chat": {"id": -42, "type": "group"},
        },
    }
    event = parse_telegram_update(
        update, bot_account_id="9", management_group_id="-42"
    ).to_dict()
    assert event["conversation"]["configured_room"] == "management_group"
    assert event["text"] == "/restart is conversation text"
    assert "owner" not in event and "role" not in event["actor"]
    assert (
        "configured_room"
        not in parse_telegram_update(
            update, bot_account_id="9", management_group_id="-43"
        ).conversation
    )
    update["message"]["from"]["is_bot"] = True
    assert (
        parse_telegram_update(update, bot_account_id="9", management_group_id="-42")
        is None
    )
