from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from lib.slack_api import SlackClient, SlackConfigurationError, chunk_message


def test_chunking_is_bounded_and_lossless() -> None:
    text = ("one two three\n" * 700) + "tail"
    chunks = chunk_message(text, max_length=128)
    assert all(0 < len(chunk) <= 128 for chunk in chunks)
    assert "".join(chunks) == text


def test_missing_or_wrong_token_types_fail_before_network() -> None:
    with pytest.raises(SlackConfigurationError, match="SLACK_BOT_TOKEN"):
        SlackClient("", "xapp-good")
    with pytest.raises(SlackConfigurationError, match="bot token"):
        SlackClient("xoxp-user", "xapp-good")
    with pytest.raises(SlackConfigurationError, match="app-level"):
        SlackClient("xoxb-good", "xoxb-not-app")


def test_private_file_download_uses_bot_authorization_and_stages_bytes(
    tmp_path,
) -> None:
    asyncio.run(
        _private_file_download_uses_bot_authorization_and_stages_bytes(tmp_path)
    )


async def _private_file_download_uses_bot_authorization_and_stages_bytes(
    tmp_path,
) -> None:
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"private bytes")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    slack = SlackClient("xoxb-secret", "xapp-secret", http_client=http)
    staged = await slack.stage_private_files(
        [
            {
                "file_id": "F1",
                "name": "../report.txt",
                "mimetype": "text/plain",
                "size": 13,
                "url_private": "https://files.slack.com/files-pri/T/F/report.txt",
            }
        ],
        destination=tmp_path / "staged",
    )

    assert observed["authorization"] == "Bearer xoxb-secret"
    assert staged[0].name == "report.txt"
    assert (tmp_path / "staged" / "00-report.txt").read_bytes() == b"private bytes"
    await slack.aclose()
    assert slack.closed is True
    assert http.is_closed is False
    await http.aclose()


def test_directory_pages_and_mutations_use_exact_slack_methods():
    asyncio.run(_directory_pages_and_mutations_use_exact_slack_methods())


async def _directory_pages_and_mutations_use_exact_slack_methods():
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, dict(request.url.params), request.content))
        path = request.url.path
        if path.endswith("conversations.list"):
            return httpx.Response(200, json={"ok": True, "channels": [{"id": "C1", "name": "room", "url": "https://slack.com/archives/C1"}], "response_metadata": {"next_cursor": "c2"}})
        if path.endswith("users.list"):
            return httpx.Response(200, json={"ok": True, "members": [{"id": "U1", "name": "reader"}], "response_metadata": {"next_cursor": "u2"}})
        if path.endswith("users.lookupByEmail"):
            return httpx.Response(200, json={"ok": True, "user": {"id": "U1", "profile": {"email": "reader@example.org"}}})
        if path.endswith("conversations.members"):
            return httpx.Response(200, json={"ok": True, "members": ["U1"], "response_metadata": {"next_cursor": ""}})
        return httpx.Response(200, json={"ok": True, "channel": {"id": "C1"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
        channels = await slack.list_conversations()
        users = await slack.list_users(cursor="u1", limit=10)
        user = await slack.lookup_user_by_email("reader@example.org")
        members = await slack.conversation_members("C1")
        joined = await slack.join_conversation("C1")
    assert channels["channels"][0]["id"] == "C1" and channels["complete"] is False
    assert users["members"][0]["id"] == "U1" and users["next_cursor"] == "u2"
    assert user["id"] == "U1" and members["members"] == ["U1"] and joined["ok"]
    assert calls[0][2]["types"] == "public_channel,private_channel,mpim,im"
    assert calls[1][2]["cursor"] == "u1"
    assert all(path.startswith("/api/") for _, path, _, _ in calls)


def test_external_upload_stages_three_provider_phases_and_keeps_completion_uncertain(tmp_path):
    asyncio.run(_external_upload_stages_three_provider_phases_and_keeps_completion_uncertain(tmp_path))


async def _external_upload_stages_three_provider_phases_and_keeps_completion_uncertain(tmp_path):
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.content, dict(request.headers)))
        if request.url.path.endswith("files.getUploadURLExternal"):
            return httpx.Response(200, json={"ok": True, "upload_url": "https://uploads.example/upload", "file_id": "F1"})
        if request.url.host == "uploads.example":
            assert request.content == b"immutable bytes"
            return httpx.Response(200, text="ok")
        return httpx.Response(200, json={"ok": True, "files": [{"id": "F1"}]})
    source = tmp_path / "hello.txt"
    source.write_bytes(b"immutable bytes")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
        result = await slack.upload_file(path=source, filename="hello.txt", channel="C1", thread_ts="1.0")
    assert result["files"][0]["id"] == "F1"
    assert calls[0][0] == "POST" and json.loads(calls[0][2])["length"] == len(b"immutable bytes")
    assert calls[1][1] == "https://uploads.example/upload"
    assert json.loads(calls[2][2])["channel_id"] == "C1"
