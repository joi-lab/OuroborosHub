from __future__ import annotations

import asyncio
import json
import pathlib
from urllib.parse import parse_qs

import httpx
import pytest

from lib.slack_api import (
    SlackApiError,
    SlackClient,
    SlackConfigurationError,
    chunk_message,
)


def test_chunking_is_bounded_and_lossless() -> None:
    text = ("one two three\n" * 700) + "tail"
    chunks = chunk_message(text, max_length=128)
    assert all(0 < len(chunk) <= 128 for chunk in chunks)
    assert "".join(chunks) == text


def test_generic_web_api_reads_keep_method_path_and_reject_url_escape():
    asyncio.run(_test_generic_web_api_reads_keep_method_path_and_reject_url_escape())


async def _test_generic_web_api_reads_keep_method_path_and_reject_url_escape():
    observed = {}
    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(method=request.method, path=request.url.path, params=dict(request.url.params), auth=request.headers.get("authorization"))
        return httpx.Response(200, json={"ok": True, "channels": [{"id": "C1"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
        result = await slack.generic_request(method="GET", path="/api/conversations.list", params={"limit": 1})
    assert result["channels"][0]["id"] == "C1"
    assert observed == {"method": "GET", "path": "/api/conversations.list", "params": {"limit": "1"}, "auth": "Bearer xoxb-test"}
    with pytest.raises(SlackConfigurationError):
        SlackClient.normalize_method_path("POST", "https://evil.example/api/chat.postMessage")


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
    assert calls[0][0] == "POST"
    assert calls[0][3]["content-type"].startswith("application/x-www-form-urlencoded")
    assert parse_qs(calls[0][2].decode("utf-8")) == {
        "filename": ["hello.txt"], "length": [str(len(b"immutable bytes"))],
    }
    assert calls[1][1] == "https://uploads.example/upload"
    assert json.loads(calls[2][2])["channel_id"] == "C1"


def test_dedicated_bookmark_declares_slack_link_type():
    async def run():
        observed = {}
        def provider(request):
            observed.update(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "bookmark": {"id": "Bk1"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            result = await slack.bookmark(channel="C1", title="test", link="https://example.org")
        assert result["bookmark"]["id"] == "Bk1"
        assert observed == {"channel_id": "C1", "title": "test", "type": "link", "link": "https://example.org"}
    asyncio.run(run())


@pytest.mark.parametrize(
    "url,code",
    [
        ("http://files.slack.com/files-pri/T/F/x.txt", "invalid_private_file_url"),
        ("https://user:pass@files.slack.com/files-pri/T/F/x.txt", "invalid_private_file_url"),
        ("https://files.slack.com:8443/files-pri/T/F/x.txt", "invalid_private_file_url"),
        ("https:///files-pri/T/F/x.txt", "invalid_private_file_url"),
        # urlsplit reads "files.slack.com" here as userinfo; the real host is evil.example.
        ("https://files.slack.com@evil.example/files-pri/T/F/x.txt", "invalid_private_file_url"),
        ("https://files.slack.com.evil.example/files-pri/T/F/x.txt", "private_file_host_not_allowed"),
        ("https://evil-files.slack.com.br/files-pri/T/F/x.txt", "private_file_host_not_allowed"),
        ("https://203.0.113.10/files-pri/T/F/x.txt", "private_file_host_not_allowed"),
        ("https://[::1]/files-pri/T/F/x.txt", "private_file_host_not_allowed"),
        ("https://files.slack.com./files-pri/T/F/x.txt", "private_file_host_not_allowed"),
        ("https://slack.com/files-pri/T/F/x.txt", "private_file_host_not_allowed"),
    ],
)
def test_bot_credential_never_leaves_the_documented_slack_file_host(tmp_path, url, code):
    async def run():
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, content=b"never reached")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            slack = SlackClient("xoxb-secret", "xapp-secret", http_client=http)
            with pytest.raises(SlackApiError) as refusal:
                await slack.stage_private_files(
                    [{"file_id": "F1", "name": "x.txt", "size": 4, "url_private": url}],
                    destination=tmp_path / "staged",
                )
        assert refusal.value.error == code
        # The refusal happens before any request, so no origin ever saw the token.
        assert requests == []
        assert list((tmp_path / "staged").iterdir()) == []

    asyncio.run(run())


def test_private_file_redirect_is_refused_and_never_replays_the_credential(tmp_path):
    async def run():
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.host, request.headers.get("authorization")))
            if request.url.host == "files.slack.com":
                return httpx.Response(
                    302,
                    headers={"Location": "https://evil.example/collect?token=1"},
                )
            return httpx.Response(200, content=b"attacker bytes")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            slack = SlackClient("xoxb-secret", "xapp-secret", http_client=http)
            with pytest.raises(SlackApiError) as refusal:
                await slack.stage_private_files(
                    [{
                        "file_id": "F1", "name": "x.txt", "size": 4,
                        "url_private": "https://files.slack.com/files-pri/T/F/x.txt",
                    }],
                    destination=tmp_path / "staged",
                )
        assert refusal.value.error == "file_redirect_refused_302"
        assert refusal.value.details["redirect_host"] == "evil.example"
        # Exactly one request, to Slack; the redirect was never followed, so the
        # bot token was never re-sent anywhere.
        assert seen == [("files.slack.com", "Bearer xoxb-secret")]
        assert not any(host != "files.slack.com" for host, _auth in seen)
        assert list((tmp_path / "staged").iterdir()) == []

    asyncio.run(run())


def test_file_download_by_id_stages_from_the_documented_host(tmp_path):
    async def run():
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.host, request.url.path))
            if request.url.path.endswith("files.info"):
                return httpx.Response(200, json={"ok": True, "file": {
                    "id": "F1", "name": "brief.pdf", "mimetype": "application/pdf", "size": 5,
                    "url_private_download": "https://files.slack.com/files-pri/T/F/download/brief.pdf",
                }})
            return httpx.Response(200, content=b"bytes")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            slack = SlackClient("xoxb-secret", "xapp-secret", http_client=http)
            staged = await slack.download_file("F1", destination=tmp_path / "downloads")
        assert staged.file_id == "F1" and staged.size == 5
        assert pathlib.Path(staged.path).read_bytes() == b"bytes"
        assert seen[-1][0] == "files.slack.com"

    asyncio.run(run())
