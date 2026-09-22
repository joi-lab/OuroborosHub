from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from lib import read_tools
from lib.slack_api import SlackClient


class API:
    def __init__(self):
        self.tools = {}

    def get_settings(self, keys):
        assert keys == ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"]
        return {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test"}

    def register_tool(self, name, handler, **metadata):
        self.tools[name] = (handler, metadata)


def with_tools(monkeypatch, http):
    monkeypatch.setattr(read_tools, "SlackClient", lambda bot, app: SlackClient(bot, app, http_client=http))
    api = API()
    read_tools.register_read_tools(api)
    return api.tools


def test_tool_schemas_match_manifest_scopes_and_specific_reads():
    api = API()
    read_tools.register_read_tools(api)
    assert {
        "slack_user_info", "slack_conversation_info", "slack_history", "slack_thread",
        "slack_list_conversations", "slack_list_users", "slack_lookup_user_email",
        "slack_members", "slack_join", "slack_resolve",
    } <= set(api.tools)
    assert all(len(name) <= 24 for name in api.tools)
    assert api.tools["slack_thread"][1]["schema"]["required"] == ["channel_id", "thread_ts"]
    history_schema = api.tools["slack_history"][1]["schema"]
    assert {"cursor", "oldest", "latest", "inclusive", "limit"} <= set(history_schema["properties"])
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    scopes = set(manifest["oauth_config"]["scopes"]["bot"])
    assert {"users:read", "users:read.email", "channels:read", "groups:read", "im:read", "mpim:read"} <= scopes
    assert {"channels:history", "groups:history", "im:history", "mpim:history", "chat:write"} <= scopes
    assert {"files:write", "channels:join", "reactions:read", "reactions:write",
            "pins:read", "pins:write", "bookmarks:read", "bookmarks:write"} <= scopes


def test_lookup_tools_return_full_current_provider_objects(monkeypatch):
    async def run():
        user = {"id": "U1", "profile": {"display_name": "Reader", "title": "Writer", "custom": {"field": "value"}}}
        room = {"id": "D1", "is_im": True, "purpose": {"value": "Discussion"}}
        def provider(request):
            assert request.headers["authorization"] == "Bearer xoxb-test"
            assert request.method == "GET" and request.content == b""
            assert "Content-Type" not in request.headers
            if request.url.path.endswith("users.info"):
                assert dict(request.url.params) == {"user": "U1", "include_locale": "true"}
                return httpx.Response(200, json={"ok": True, "user": user})
            assert dict(request.url.params) == {"channel": "D1", "include_locale": "true"}
            return httpx.Response(200, json={"ok": True, "channel": room})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            tools = with_tools(monkeypatch, http)
            first = await tools["slack_user_info"][0](user_id="U1")
            second = await tools["slack_conversation_info"][0](channel_id="D1")
        assert first["user"] == user and first["source"] == "users.info" and first["observed_at"]
        assert second["conversation"] == room and second["source"] == "conversations.info"
    asyncio.run(run())


@pytest.mark.parametrize("kind,endpoint", [("slack_history", "history"), ("slack_thread", "replies")])
def test_history_and_thread_keep_full_text_filters_and_cursor(monkeypatch, kind, endpoint):
    async def run():
        calls = []
        long_text = "context " * 20000
        def provider(request):
            assert request.url.path == "/api/conversations." + endpoint
            assert request.method == "GET" and request.content == b""
            params = dict(request.url.params)
            calls.append(params)
            assert params["channel"] == "D1"
            assert params["oldest"] == "1.0" and params["latest"] == "3.0" and params["inclusive"] == "true"
            assert params["limit"] == "15"
            if endpoint == "replies":
                assert params["ts"] == "2.0"
            else:
                assert "ts" not in params
            if len(calls) == 1:
                return httpx.Response(200, json={"ok": True, "messages": [{"ts": "2.0", "user": "U1", "text": long_text}],
                                                "has_more": True, "response_metadata": {"next_cursor": "cursor value"}})
            assert params["cursor"] == "cursor value"
            return httpx.Response(200, json={"ok": True, "messages": [{"ts": "2.1", "thread_ts": "2.0", "user": "U2", "text": "reply"}],
                                            "has_more": False, "response_metadata": {"next_cursor": ""}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            tool = with_tools(monkeypatch, http)[kind][0]
            arguments = {"channel_id": "D1", "oldest": "1.0", "latest": "3.0", "inclusive": True, "limit": 15}
            if endpoint == "replies": arguments["thread_ts"] = "2.0"
            first = await tool(**arguments)
            second = await tool(**arguments, cursor=first["next_cursor"])
        assert first["messages"][0]["text"] == long_text
        assert not first["complete"] and first["has_more"] and first["next_cursor"] == "cursor value"
        assert second["complete"] and second["next_cursor"] is None
        assert second["messages"][0]["thread_ts"] == "2.0" and len(calls) == 2
    asyncio.run(run())


def test_more_without_cursor_stays_explicitly_incomplete(monkeypatch):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={"ok": True, "messages": [{"ts": "2.0", "text": "text"}], "has_more": True}
        ))) as http:
            result = await with_tools(monkeypatch, http)["slack_history"][0](channel_id="D1")
        assert result["complete"] is False and result["next_cursor"] is None and result["continuation_note"]
    asyncio.run(run())


@pytest.mark.parametrize("status,error", [(403, "missing_scope"), (200, "not_allowed_token_type"), (429, "ratelimited")])
def test_provider_denial_is_an_actionable_error_not_empty_thread(monkeypatch, status, error):
    async def run():
        calls = []
        def provider(request):
            calls.append(request)
            return httpx.Response(status, json={"ok": False, "error": error, "needed": "groups:history", "provided": "chat:write"},
                                  headers={"Retry-After": "30"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            result = await with_tools(monkeypatch, http)["slack_thread"][0](channel_id="C1", thread_ts="1.0")
        assert result["ok"] is False and "messages" not in result
        assert result["error"]["code"] == error and result["error"]["http_status"] == status
        assert result["error"]["retry_after"] == 30 and result["error"]["needed"] == "groups:history"
        if status != 429: assert result["error"]["hint"]
        assert len(calls) == 1
    asyncio.run(run())


def test_empty_thread_id_never_falls_back_to_history(monkeypatch):
    async def run():
        calls = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r))) as http:
            result = await with_tools(monkeypatch, http)["slack_thread"][0](channel_id="D1", thread_ts="")
        assert result["ok"] is False and "thread_ts" in result["error"]["message"] and calls == []
    asyncio.run(run())
