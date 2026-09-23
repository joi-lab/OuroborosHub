from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from lib import directory, read_tools
from lib.directory import resolve_directory
from lib.slack_api import SlackClient
from lib.store import BridgeStore


def _api(tmp_path):
    return SimpleNamespace(get_state_dir=lambda: str(tmp_path))


def _page(entries, cursor="", *, kind="user", more=False):
    return httpx.Response(200, json={"ok": True, "members" if kind == "user" else "channels": entries,
                                    "response_metadata": {"next_cursor": cursor}, "has_more": more})


def test_walks_all_pages_preserves_ambiguity_and_reuses_complete_cache_across_queries(tmp_path):
    async def run():
        calls = []
        def provider(request):
            cursor = request.url.params.get("cursor", "")
            calls.append(cursor)
            assert request.url.params["limit"] == "200"
            if not cursor:
                return _page([{"id": "U1", "real_name": "Morgan", "deleted": True}], "page2")
            return _page([{"id": "U2", "profile": {"display_name": "Morgan", "title": "Writer"}},
                          {"id": "U3", "real_name": "Casey"}])
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            first = await resolve_directory(_api(tmp_path), slack, "xoxb-test", query="Morgan", kind="user")
            second = await resolve_directory(_api(tmp_path), slack, "xoxb-test", query="Casey", kind="user")
            absent = await resolve_directory(_api(tmp_path), slack, "xoxb-test", query="Nobody", kind="user")
        assert calls == ["", "page2"]
        assert [c["id"] for c in first["candidates"]] == ["U1", "U2"]
        assert first["complete"] and first["entries_scanned"] == 3 and first["pages_fetched"] == 2
        assert not first["cache_hit"] and first["observed_at"]
        assert second["candidates"][0]["id"] == "U3" and second["cache_hit"]
        assert second["observed_at"] == first["observed_at"] and second["pages_fetched"] == 0
        assert absent["complete"] and absent["candidates"] == [] and absent["cache_hit"]
        assert "directory_cache" not in json.dumps(BridgeStore(tmp_path).status())
        assert "Morgan" not in json.dumps(BridgeStore(tmp_path).status())
    asyncio.run(run())


@pytest.mark.parametrize(("query", "kind", "endpoint", "param", "value"), [
    ("U123ABC45", "user", "users.info", "user", "U123ABC45"),
    ("<@U123ABC45>", "user", "users.info", "user", "U123ABC45"),
    ("https://example.slack.com/team/U123ABC45", "user", "users.info", "user", "U123ABC45"),
    ("person@example.org", "user", "users.lookupByEmail", "email", "person@example.org"),
    ("C123ABC45", "channel", "conversations.info", "channel", "C123ABC45"),
    ("<#C123ABC45|room>", "channel", "conversations.info", "channel", "C123ABC45"),
    ("https://example.slack.com/archives/C123ABC45/p123456", "channel", "conversations.info", "channel", "C123ABC45"),
    ("https://app.slack.com/client/T123ABC45/C123ABC45", "channel", "conversations.info", "channel", "C123ABC45"),
])
def test_exact_lookup_does_not_scan_or_read_cache(tmp_path, query, kind, endpoint, param, value):
    async def run():
        calls = []
        def provider(request):
            calls.append(request.url.path)
            assert request.url.path == "/api/" + endpoint and request.url.params[param] == value
            return httpx.Response(200, json={"ok": True, "user" if kind == "user" else "channel": {"id": value}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            result = await resolve_directory(_api(tmp_path), SlackClient("xoxb-test", "xapp-test", http_client=http),
                                             "xoxb-test", query=query, kind=kind)
        assert len(calls) == 1 and result["coverage"] == "exact_lookup" and result["complete"]
        assert result["source"] == endpoint and not (tmp_path / "slack_bridge.sqlite3").exists()
    asyncio.run(run())


def test_refresh_expiry_and_credential_rotation_do_not_reuse_old_observations(tmp_path, monkeypatch):
    async def run():
        observed = 1000.0
        monkeypatch.setattr(directory.time, "time", lambda: observed)
        calls = []
        def provider(request):
            calls.append(request.headers["authorization"])
            return _page([{"id": "U1", "real_name": "Current " + str(len(calls))}])
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            a = SlackClient("xoxb-one", "xapp-test", http_client=http)
            b = SlackClient("xoxb-two", "xapp-test", http_client=http)
            first = await resolve_directory(_api(tmp_path), a, "xoxb-one", query="current", kind="user")
            refreshed = await resolve_directory(_api(tmp_path), a, "xoxb-one", query="current", kind="user", refresh=True)
            observed += directory.CACHE_MAX_AGE_SEC + 1
            expired = await resolve_directory(_api(tmp_path), a, "xoxb-one", query="current", kind="user")
            rotated = await resolve_directory(_api(tmp_path), b, "xoxb-two", query="current", kind="user")
        assert len(calls) == 4
        assert [r["candidates"][0]["real_name"] for r in (first, refreshed, expired, rotated)] == ["Current 1", "Current 2", "Current 3", "Current 4"]
        assert not any(r["cache_hit"] for r in (first, refreshed, expired, rotated))
        assert b"xoxb-one" not in (tmp_path / "slack_bridge.sqlite3").read_bytes()
    asyncio.run(run())


def test_rate_limit_keeps_partial_candidates_and_resumes_from_saved_prefix(tmp_path):
    async def run():
        calls = []
        def provider(request):
            cursor = request.url.params.get("cursor", "")
            calls.append(cursor)
            if not cursor:
                return _page([{"id": "U1", "real_name": "Morgan"}], "next")
            if len(calls) == 2:
                return httpx.Response(429, headers={"Retry-After": "20"}, json={"ok": False, "error": "ratelimited"})
            return _page([{"id": "U2", "real_name": "Morgan"}])
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            partial = await resolve_directory(_api(tmp_path), slack, "xoxb-test", query="Morgan", kind="user")
            full = await resolve_directory(_api(tmp_path), slack, "xoxb-test", query="Morgan", kind="user", cursor=partial["next_cursor"])
        assert calls == ["", "next", "next"]
        assert not partial["ok"] and not partial["complete"] and partial["error"]["retry_after"] == 20
        assert partial["candidates"][0]["id"] == "U1"
        assert full["complete"] and [v["id"] for v in full["candidates"]] == ["U1", "U2"]
    asyncio.run(run())


def test_partial_empty_cache_is_not_negative_lookup_and_new_query_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(directory, "SCAN_MAX_PAGES", 1)
    async def run():
        calls = []
        def provider(request):
            cursor = request.url.params.get("cursor", "")
            calls.append(cursor)
            return _page([{"id": "U1", "real_name": "Other"}], "next") if not cursor else _page([{"id": "U2", "real_name": "Morgan"}])
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            absent = await resolve_directory(_api(tmp_path), slack, "xoxb-test", query="Nobody", kind="user")
            full = await resolve_directory(_api(tmp_path), slack, "xoxb-test", query="Morgan", kind="user")
        assert absent["candidates"] == [] and not absent["complete"]
        assert absent["error"]["code"] == "directory_scan_page_bound"
        assert full["complete"] and full["candidates"][0]["id"] == "U2" and calls == ["", "next"]
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["timeout", "missing_cursor", "repeated_cursor", "invalid_cursor"])
def test_bounded_failures_keep_results_and_never_claim_complete(tmp_path, failure):
    async def run():
        calls = []
        def provider(request):
            calls.append(request)
            if len(calls) == 1:
                return _page([{"id": "U1", "real_name": "Morgan"}], "next" if failure != "missing_cursor" else "", more=True)
            if failure == "timeout":
                raise httpx.ReadTimeout("timeout", request=request)
            if failure == "invalid_cursor":
                return httpx.Response(200, json={"ok": False, "error": "invalid_cursor"})
            return _page([], "next")
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            result = await resolve_directory(_api(tmp_path), SlackClient("xoxb-test", "xapp-test", http_client=http),
                                             "xoxb-test", query="Morgan", kind="user")
        assert not result["ok"] and not result["complete"] and result["continuation_note"]
        assert result["candidates"][0]["id"] == "U1" and len(calls) <= 2
        if failure != "timeout":
            assert result["next_cursor"] is None and "refresh=true" in result["continuation_note"]
    asyncio.run(run())


def test_external_cursor_cannot_claim_whole_directory_or_fill_complete_cache(tmp_path):
    async def run():
        calls = []
        def provider(request):
            calls.append(request.url.params.get("cursor", ""))
            return _page([{"id": "U1", "real_name": "Morgan"}])
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            tail = await resolve_directory(_api(tmp_path), slack, "xoxb-test", query="Morgan", kind="user", cursor="external")
            full = await resolve_directory(_api(tmp_path), slack, "xoxb-test", query="Morgan", kind="user")
        assert tail["coverage"] == "from_supplied_cursor" and not tail["complete"]
        assert full["complete"] and calls == ["external", ""]
    asyncio.run(run())


def test_registered_resolver_returns_json_and_declares_refresh(tmp_path, monkeypatch):
    class API:
        def __init__(self): self.tools = {}
        def get_settings(self, _keys): return {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test"}
        def get_state_dir(self): return str(tmp_path)
        def register_tool(self, name, handler, **metadata): self.tools[name] = (handler, metadata)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: _page([{"id": "C1", "name": "general"}], kind="channel"))) as http:
            monkeypatch.setattr(read_tools, "SlackClient", lambda bot, app: SlackClient(bot, app, http_client=http))
            api = API()
            read_tools.register_read_tools(api)
            handler, meta = api.tools["slack_resolve"]
            result = json.loads(await handler(query="#general", refresh=True))
            assert result["candidates"][0]["id"] == "C1" and result["complete"]
            assert meta["schema"]["properties"]["refresh"]["default"] is False
            assert meta["timeout_sec"] == 60
    asyncio.run(run())


def test_older_scan_cannot_overwrite_newer_cached_snapshot(tmp_path):
    store = BridgeStore(tmp_path)
    key = "user:" + hashlib.sha256(b"token").hexdigest()
    store.save_directory_snapshot(key, {"observed_at": 20, "entries": [{"id": "new"}], "complete": True})
    store.save_directory_snapshot(key, {"observed_at": 10, "entries": [{"id": "old"}], "complete": False})
    assert store.directory_snapshot(key)["entries"] == [{"id": "new"}]


def test_large_workspace_scan_does_not_require_one_model_round_per_page(tmp_path):
    async def run():
        calls = []
        def provider(request):
            page = int(request.url.params.get("cursor", "0"))
            calls.append(page)
            entries = [{"id": f"U{page:03}{i:06}", "real_name": f"Member {page}-{i}"} for i in range(200)]
            if page in (3, 23):
                entries[-1]["real_name"] = "Casey Example"
            return _page(entries, str(page + 1) if page < 23 else "")
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            result = await resolve_directory(_api(tmp_path), SlackClient("xoxb-test", "xapp-test", http_client=http),
                                             "xoxb-test", query="Casey Example", kind="user")
        assert calls == list(range(24))
        assert result["complete"] and result["entries_scanned"] == 4800
        assert len(result["candidates"]) == 2 and result["pages_fetched"] == 24
    asyncio.run(run())


def test_exact_lookup_error_stays_provider_error_without_directory_fallback(tmp_path, monkeypatch):
    class API:
        def __init__(self): self.tools = {}
        def get_settings(self, _keys): return {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test"}
        def get_state_dir(self): return str(tmp_path)
        def register_tool(self, name, handler, **metadata): self.tools[name] = (handler, metadata)
    async def run():
        calls = []
        def provider(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={"ok": False, "error": "user_not_found"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            monkeypatch.setattr(read_tools, "SlackClient", lambda bot, app: SlackClient(bot, app, http_client=http))
            api = API()
            read_tools.register_read_tools(api)
            result = json.loads(await api.tools["slack_resolve"][0](query="U123ABC45", kind="user"))
        assert calls == ["/api/users.info"]
        assert not result["ok"] and result["error"]["code"] == "user_not_found"
        assert "candidates" not in result and not (tmp_path / "slack_bridge.sqlite3").exists()
    asyncio.run(run())


def test_plain_at_handle_is_a_name_query_not_an_email_lookup(tmp_path):
    async def run():
        def provider(request):
            assert request.url.path == "/api/users.list"
            return _page([{"id": "U1", "name": "casey.example"}])
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            result = await resolve_directory(_api(tmp_path), SlackClient("xoxb-test", "xapp-test", http_client=http),
                                             "xoxb-test", query="@casey.example", kind="user")
        assert result["complete"] and result["candidates"][0]["id"] == "U1"
    asyncio.run(run())


def test_actual_scan_deadline_keeps_partial_results_and_resumes(tmp_path, monkeypatch):
    # On Python 3.10 wait_for raises asyncio.TimeoutError, not builtin TimeoutError.
    monkeypatch.setattr(directory, "SCAN_TIMEOUT_SEC", 0.05)
    async def run():
        calls = []
        async def provider(request):
            cursor = request.url.params.get("cursor", "")
            calls.append(cursor)
            if not cursor:
                return _page([{"id": "U1", "real_name": "Morgan"}], "next")
            if len(calls) == 2:
                await asyncio.sleep(1)
            return _page([{"id": "U2", "real_name": "Morgan"}])
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            slack = SlackClient("xoxb-test", "xapp-test", http_client=http)
            partial = await resolve_directory(_api(tmp_path), slack, "xoxb-test", query="Morgan", kind="user")
            key = "user:" + hashlib.sha256(b"xoxb-test").hexdigest()
            saved = BridgeStore(tmp_path).directory_snapshot(key)
            assert saved["entries"] == [{"id": "U1", "real_name": "Morgan"}]
            assert saved["next_cursor"] == partial["next_cursor"] == "next"
            assert not partial["ok"] and not partial["complete"]
            assert partial["error"]["code"] == "lookup_timeout"
            assert partial["candidates"] == saved["entries"]
            complete = await resolve_directory(_api(tmp_path), slack, "xoxb-test", query="Morgan", kind="user", cursor="next")
        assert calls == ["", "next", "next"]
        assert complete["ok"] and complete["complete"]
        assert [entry["id"] for entry in complete["candidates"]] == ["U1", "U2"]
    asyncio.run(run())
