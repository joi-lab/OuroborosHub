import asyncio
from types import SimpleNamespace

from telegram_bot.api import TelegramClient
from telegram_bot.custody import CustodyStore
from telegram_bot.runtime import TelegramTransportRuntime
from telegram_bot.tools import make_moderation_tool, make_operation_tool, make_receipt_tool


class Transport:
    def __init__(self):
        self.calls = []

    async def post_json(self, url, payload, timeout_sec):
        self.calls.append((url.rsplit("/", 1)[-1], payload))
        return {"ok": True, "result": True}


def test_moderation_exact_targets_and_provider_methods(tmp_path):
    async def run():
        transport = Transport()
        client = TelegramClient("test-token", transport=transport)
        api = SimpleNamespace(get_state_dir=lambda: str(tmp_path))
        moderate, receipt = make_moderation_tool(api), make_receipt_tool(api)
        runtime = TelegramTransportRuntime(
            state_dir=tmp_path,
            token_provider=lambda: "test-token",
            logger=None,
            submitter=None,
        )
        # Identity was already established by the poller; no network in this test.
        runtime._client, runtime._client_token = client, "test-token"
        operations = [
            {"action": "delete_message", "message_id": 123},
            {
                "action": "restrict_member",
                "user_id": 44,
                "permissions": {"can_send_messages": False},
                "until_date": 1900000000,
            },
            {"action": "ban_member", "user_id": 44},
            {"action": "unban_member", "user_id": 44},
        ]
        for index, operation in enumerate(operations):
            request_id = f"moderate-{index}"
            assert (
                moderate(chat_id="-42", request_id=request_id, **operation)["state"]
                == "queued"
            )
            assert (
                moderate(chat_id="-42", request_id=request_id, **operation)["state"]
                == "already_queued"
            )
            assert await runtime.process_one_outbox()
            result = receipt(request_id=request_id, operation="moderate")
            assert (
                result["state"] == "delivered"
                and result["provider_receipt"]["result"] is True
            )
        assert [name for name, _ in transport.calls] == [
            "deleteMessage",
            "restrictChatMember",
            "banChatMember",
            "unbanChatMember",
        ]
        assert transport.calls[0][1] == {"chat_id": "-42", "message_id": 123}
        assert transport.calls[1][1] == {
            "chat_id": "-42",
            "user_id": 44,
            "permissions": {"can_send_messages": False},
            "use_independent_chat_permissions": True,
            "until_date": 1900000000,
        }
        assert transport.calls[-1][1] == {
            "chat_id": "-42",
            "user_id": 44,
            "only_if_banned": True,
        }
        # Reopening the store after a restart preserves receipt and deduplication.
        assert not CustodyStore(tmp_path / "custody.sqlite3").enqueue_outbox(
            "telegram-moderate:moderate-0", {}
        )
        assert not await runtime.process_one_outbox()

    asyncio.run(run())


def test_moderation_requires_exact_ids_and_typed_permissions(tmp_path):
    tool = make_moderation_tool(SimpleNamespace(get_state_dir=lambda: str(tmp_path)))
    assert (
        tool(action="ban_member", chat_id="@some_name", user_id=42, request_id="r")[
            "ok"
        ]
        is False
    )
    assert (
        tool(action="delete_message", chat_id="-42", message_id=True, request_id="r")[
            "ok"
        ]
        is False
    )
    assert (
        tool(
            action="restrict_member",
            chat_id="-42",
            user_id=42,
            permissions={"can_send_messages": "false"},
            request_id="r",
        )["ok"]
        is False
    )
    assert (
        tool(action="ban_member", chat_id="-42", user_id=42, request_id="")["ok"]
        is False
    )


def test_edit_operation_plain_and_formatted_text_reach_provider_unchanged(tmp_path):
    async def run():
        transport = Transport()
        client = TelegramClient("test-token", transport=transport)
        edit = make_operation_tool(SimpleNamespace(get_state_dir=lambda: str(tmp_path)))
        runtime = TelegramTransportRuntime(
            state_dir=tmp_path, token_provider=lambda: "test-token", logger=None, submitter=None,
        )
        runtime._client, runtime._client_token = client, "test-token"
        # Omission is the advertised plain-text form. Retain old explicit-empty calls too.
        cases = [({}, "*literal*"), ({"parse_mode": ""}, "<literal>"),
                 ({"parse_mode": "HTML"}, "<b>bold</b>"),
                 ({"parse_mode": "MarkdownV2"}, "*bold*")]
        for index, (options, text) in enumerate(cases):
            assert edit(method="editMessageText", chat_id="-42", message_id=123,
                        request_id=f"format-{index}", text=text, **options)["ok"]
            assert await runtime.process_one_outbox()
            expected = {"chat_id": "-42", "message_id": 123, "text": text}
            if options.get("parse_mode"):
                expected.update(options)
            assert transport.calls[-1] == ("editMessageText", expected)

    asyncio.run(run())
