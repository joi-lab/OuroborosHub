from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from confluence_skill_under_test.client import Config, ConfluenceClient, ConfluenceError, SETTINGS_KEYS
from confluence_skill_under_test.operations import Operations
from confluence_skill_under_test import plugin


def config(mode="personal", **overrides):
    settings = {
        "CONFLUENCE_SITE_URL": "https://example.atlassian.net/wiki/",
        "CONFLUENCE_EMAIL": "reader@example.org",
        "CONFLUENCE_API_TOKEN": "unit-test-value",
        "CONFLUENCE_AUTH_MODE": mode,
    }
    settings.update(overrides)
    return Config.from_settings(settings)


def connect(handler, tmp_path, mode="personal", **overrides):
    client = ConfluenceClient(config(mode, **overrides), transport=httpx.MockTransport(handler))
    return client, Operations(client, tmp_path)


def page(identifier="123", version=2, body="<p>Complete</p>", status="current"):
    return {"id": identifier, "title": "Guide", "spaceId": "12", "status": status,
            "version": {"number": version}, "body": {"storage": {"representation": "storage", "value": body}}}


@pytest.mark.parametrize("mode,prefix,authorization", [
    ("personal", "https://example.atlassian.net", "Basic"),
    ("scoped", "https://api.atlassian.com/ex/confluence/cloud-123", "Basic"),
    ("scoped_bearer", "https://api.atlassian.com/ex/confluence/cloud-123", "Bearer"),
])
def test_explicit_auth_route_and_identity(mode, prefix, authorization, tmp_path):
    requests = []
    def provider(request):
        requests.append(request)
        assert str(request.url).startswith(prefix + "/wiki/")
        header = request.headers["Authorization"]
        assert header.startswith(authorization + " ")
        if authorization == "Basic":
            assert base64.b64decode(header.split()[1]).decode() == "reader@example.org:unit-test-value"
        if request.url.path.endswith("/user/current"):
            return httpx.Response(200, json={"accountId": "actor-1", "displayName": "Reader", "type": "known"})
        return httpx.Response(200, json={"results": [], "_links": {}})
    client, tools = connect(provider, tmp_path, mode, CONFLUENCE_CLOUD_ID="cloud-123")
    with client:
        result = tools.test_connection()
    assert result["ok"] is True
    assert result["checks"]["identity"]["actor"]["accountId"] == "actor-1"
    assert result["checks"]["spaces"]["accessible_count_on_first_page"] == 0
    assert result["writes_tested"] is False
    assert len(requests) == 2


def test_scoped_cloud_discovery_has_no_credentials_or_cookies(tmp_path):
    requests = []
    def provider(request):
        requests.append(request)
        if len(requests) == 1:
            assert str(request.url) == "https://example.atlassian.net/_edge/tenant_info"
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"cloudId": "resolved-123"}, headers={"Set-Cookie": "auth=private; Domain=.atlassian.net"})
        assert str(request.url).startswith("https://api.atlassian.com/ex/confluence/resolved-123/wiki/api/v2/spaces")
        assert "cookie" not in request.headers
        return httpx.Response(200, json={"results": []})
    client, tools = connect(provider, tmp_path, "scoped")
    with client:
        assert tools.list_spaces()["complete"] is True
    assert len(requests) == 2


@pytest.mark.parametrize("changes", [
    {"CONFLUENCE_SITE_URL": "http://example.atlassian.net"},
    {"CONFLUENCE_SITE_URL": "https://name:password@example.atlassian.net"},
    {"CONFLUENCE_SITE_URL": "https://example.atlassian.net/wiki/spaces/DOCS"},
    {"CONFLUENCE_AUTH_MODE": "auto"},
    {"CONFLUENCE_EMAIL": ""},
    {"CONFLUENCE_API_TOKEN": ""},
    {"CONFLUENCE_CLOUD_ID": "../different"},
])
def test_invalid_config_fails_before_network(changes):
    with pytest.raises(ConfluenceError):
        config(**changes)


def test_bearer_does_not_require_email():
    assert config("scoped_bearer", CONFLUENCE_EMAIL="").mode == "scoped_bearer"


def test_preflight_keeps_auth_and_resource_results_separate(tmp_path):
    def provider(request):
        if request.url.path.endswith("user/current"):
            return httpx.Response(401, json={"message": "Unauthorized; scope does not match"})
        if request.url.path.endswith("spaces"):
            return httpx.Response(200, json={"results": [{"id": "12"}]})
        return httpx.Response(404, json={"message": "Page not accessible"})
    client, tools = connect(provider, tmp_path)
    with client:
        result = tools.test_connection("123")
    assert result["ok"] is False
    assert result["checks"]["identity"]["error"]["http_status"] == 401
    assert "scope" in result["checks"]["identity"]["error"]["message"]
    assert result["checks"]["spaces"]["ok"] is True
    assert result["checks"]["page"]["error"]["http_status"] == 404


def test_full_page_url_read_not_truncated(tmp_path):
    body = "<p>" + "knowledge " * 30000 + "</p>"
    def provider(request):
        assert request.url.path == "/wiki/api/v2/pages/123"
        assert request.url.params["body-format"] == "storage"
        return httpx.Response(200, json=page(body=body))
    client, tools = connect(provider, tmp_path)
    with client:
        result = tools.get_page("https://example.atlassian.net/wiki/spaces/DOCS/pages/123/Guide")
        assert tools.get_page("https://example.atlassian.net/wiki/pages/viewpage.action?pageId=123")["page_id"] == "123"
    assert result["page"]["body"]["storage"]["value"] == body
    assert result["version"] == 2
    assert result["url"].startswith("https://example.atlassian.net/wiki/")


def test_missing_full_body_is_not_success(tmp_path):
    client, tools = connect(lambda _: httpx.Response(200, json={"id": "123"}), tmp_path)
    with client, pytest.raises(ConfluenceError, match="storage body"):
        tools.get_page("123")


@pytest.mark.parametrize("link_prefix", ["/wiki/rest/api", "/rest/api"])
def test_search_scoped_pagination_keeps_resource_and_query(tmp_path, link_prefix):
    calls = []
    def provider(request):
        calls.append(request)
        if len(calls) == 1:
            assert request.url.params["cql"] == 'type=page AND space="DOCS"'
            return httpx.Response(200, json={"results": [{"content": {"id": "123"}}],
                "_links": {"next": link_prefix + "/search?cql=type%3Dpage&cursor=next"}})
        assert request.url.params["cursor"] == "next"
        assert request.url.params["cql"] == "type=page"
        return httpx.Response(200, json={"results": [{"content": {"id": "124"}}]})
    client, tools = connect(provider, tmp_path, "scoped", CONFLUENCE_CLOUD_ID="cloud-123")
    with client:
        first = tools.search('type=page AND space="DOCS"')
        assert first["complete"] is False
        assert "/ex/confluence/cloud-123/wiki/rest/api/search" in first["next_url"]
        second = tools.search('type=page AND space="DOCS"', next_url=first["next_url"])
    assert second["complete"] is True
    assert second["results"][0]["content"]["id"] == "124"


@pytest.mark.parametrize("next_url", [
    "https://unrelated.example/wiki/api/v2/pages?cursor=1",
    "https://api.atlassian.com/ex/confluence/other/wiki/api/v2/pages?cursor=1",
    "/wiki/api/v2/spaces?cursor=1",
    "https://name:password@api.atlassian.com/ex/confluence/cloud-123/wiki/api/v2/pages",
])
def test_pagination_refuses_cross_site_tenant_and_resource(next_url, tmp_path):
    called = []
    client, tools = connect(lambda request: called.append(request), tmp_path, "scoped", CONFLUENCE_CLOUD_ID="cloud-123")
    with client, pytest.raises(ConfluenceError):
        tools.list_pages(next_url=next_url)
    assert called == []


def test_link_header_continuation_and_metadata_filters(tmp_path):
    def provider(request):
        assert request.url.params["space-id"] == "12"
        assert request.url.params["status"] == "draft"
        assert request.url.params["title"] == "Draft"
        return httpx.Response(200, json={"results": []}, headers={"Link": '</wiki/api/v2/pages?cursor=more>; rel="next"'})
    client, tools = connect(provider, tmp_path)
    with client:
        result = tools.list_pages(space_id="12", title="Draft", status="draft")
    assert result["complete"] is False
    assert result["next_url"].endswith("?cursor=more")


def test_create_and_update_draft_have_receipts_and_expected_version(tmp_path):
    calls = []
    stored = page(version=1, body="<p>Draft</p>", status="draft")
    replacement = "<h2>Updated</h2><p>Complete new draft body.</p>"
    def provider(request):
        calls.append(request)
        if request.method == "GET":
            assert request.url.params["status"] == "draft"
            assert request.url.params["get-draft"] == "true"
            return httpx.Response(200, json=stored)
        payload = json.loads(request.content)
        assert payload["status"] == "draft"
        if request.method == "POST":
            assert payload["body"] == {"representation": "storage", "value": "<p>Draft</p>"}
            assert payload["spaceId"] == "12" and payload["parentId"] == "122"
            return httpx.Response(201, json=stored)
        if payload["version"]["number"] != 1:
            return httpx.Response(400, json={"message": "DRAFT pages do not support multiple versions. Expected version: [1]."})
        assert payload["version"] == {"number": 1, "message": "Revision"}
        assert payload["body"] == {"representation": "storage", "value": replacement}
        stored["body"]["storage"] = payload["body"]
        return httpx.Response(200, json=stored)
    client, tools = connect(provider, tmp_path)
    with client:
        created = tools.create_page("12", "Draft", "<p>Draft</p>", parent_id="122", status="draft")
        updated = tools.update_page("123", "Draft", replacement, 1, status="draft", version_message="Revision")
        read_back = tools.get_page("123", status="draft")
    assert created["page_id"] == "123" and created["version"] == 1
    assert updated["version"] == 1 and updated["url"]
    assert read_back["page"]["body"]["storage"]["value"] == replacement
    assert [r.method for r in calls] == ["POST", "GET", "PUT", "GET"]


@pytest.mark.parametrize("status,expected,observed", [("current", 4, 5), ("draft", 0, 1)])
def test_stale_update_does_not_write(tmp_path, status, expected, observed):
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(200, json=page(version=observed, status=status))
    client, tools = connect(provider, tmp_path)
    with client, pytest.raises(ConfluenceError) as failure:
        tools.update_page("123", "Title", "<p>Body</p>", expected, status=status)
    assert failure.value.error["current_version"] == observed
    assert failure.value.error["write_attempted"] is False
    assert [r.method for r in calls] == ["GET"]


def test_provider_draft_version_zero_can_be_updated(tmp_path):
    def provider(request):
        if request.method == "GET":
            assert request.url.params["get-draft"] == "true"
            return httpx.Response(200, json=page(version=0, status="draft"))
        assert json.loads(request.content)["version"]["number"] == 1
        return httpx.Response(200, json=page(version=1, status="draft"))
    client, tools = connect(provider, tmp_path)
    with client:
        assert tools.get_page("123", status="draft")["version"] == 0
        assert tools.update_page("123", "Draft", "<p>New</p>", 0, status="draft")["version"] == 1


@pytest.mark.parametrize("action", ["create", "comment", "upload"])
def test_missing_mutation_receipt_is_unknown_not_success(action, tmp_path):
    source = tmp_path / "upload.txt"
    source.write_text("A file", encoding="utf-8")
    client, tools = connect(lambda _: httpx.Response(200, json={}), tmp_path)
    with client, pytest.raises(ConfluenceError) as failure:
        if action == "create":
            tools.create_page("12", "Title", "<p>Body</p>")
        elif action == "comment":
            tools.add_comment("<p>Comment</p>", page_id="123")
        else:
            tools.upload_attachment("123", str(source))
    assert failure.value.error["outcome"] == "unknown"


def test_concurrent_update_conflict_is_not_retried(tmp_path):
    calls = []
    def provider(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=page(version=4))
        assert json.loads(request.content)["version"]["number"] == 5
        return httpx.Response(409, json={"message": "Version conflict"})
    client, tools = connect(provider, tmp_path)
    with client, pytest.raises(ConfluenceError) as failure:
        tools.update_page("123", "Title", "<p>Body</p>", 4)
    assert failure.value.error["code"] == "version_conflict"
    assert failure.value.error["http_status"] == 409
    assert len(calls) == 2


def test_comments_include_bodies_and_explicit_reply_target(tmp_path):
    calls = []
    def provider(request):
        calls.append(request)
        if request.method == "POST":
            assert json.loads(request.content) == {"parentCommentId": "99", "body": {"representation": "storage", "value": "<p>Reply</p>"}}
            return httpx.Response(201, json={"id": "100"})
        assert request.url.path == "/wiki/api/v2/inline-comments/99/children"
        assert request.url.params["body-format"] == "storage"
        return httpx.Response(200, json={"results": [{"id": "100", "body": {"storage": {"value": "<p>Note</p>"}}}]})
    client, tools = connect(provider, tmp_path)
    with client:
        result = tools.list_comments(parent_comment_id="99", kind="inline")
        assert result["results"][0]["body"]["storage"]["value"] == "<p>Note</p>"
        assert tools.add_comment("<p>Reply</p>", parent_comment_id="99")["comment"]["id"] == "100"
        with pytest.raises(ConfluenceError):
            tools.add_comment("text", page_id="123", parent_comment_id="99")
    assert len(calls) == 2


def test_provider_error_redacts_auth_and_retains_retry_after(tmp_path):
    encoded = base64.b64encode(b"reader@example.org:unit-test-value").decode()
    client, tools = connect(lambda _: httpx.Response(429, json={"message": f"unit-test-value Basic {encoded}"},
                                                    headers={"Retry-After": "17"}), tmp_path)
    with client, pytest.raises(ConfluenceError) as failure:
        tools.list_spaces()
    error = failure.value.error
    assert error["http_status"] == 429 and error["retry_after"] == "17"
    assert "unit-test-value" not in str(error) and encoded not in str(error)


def test_ambiguous_create_has_no_automatic_retry(tmp_path):
    calls = []
    def provider(request):
        calls.append(request)
        raise httpx.ReadTimeout("Response timed out", request=request)
    client, tools = connect(provider, tmp_path)
    with client, pytest.raises(ConfluenceError) as failure:
        tools.create_page("12", "Title", "<p>Body</p>")
    assert failure.value.error["outcome"] == "unknown"
    assert failure.value.error["automatic_retry"] is False
    assert len(calls) == 1


def test_api_redirect_is_not_followed(tmp_path):
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://unrelated.example/capture"})
    client, tools = connect(provider, tmp_path)
    with client, pytest.raises(ConfluenceError):
        tools.list_spaces()
    assert len(calls) == 1


def test_upload_uses_multipart_no_overwrite_and_utf8_comment(tmp_path):
    path = tmp_path / "guide.txt"
    path.write_text("Complete content", encoding="utf-8")
    def provider(request):
        assert request.method == "POST" and request.url.path == "/wiki/rest/api/content/123/child/attachment"
        assert request.headers["X-Atlassian-Token"] == "nocheck"
        assert "multipart/form-data" in request.headers["Content-Type"]
        assert b'filename="guide.txt"' in request.content
        assert b"text/plain; charset=utf-8" in request.content
        assert "Résumé".encode() in request.content
        assert b"Complete content" in request.content
        return httpx.Response(200, json={"results": [{"id": "att456"}]})
    client, tools = connect(provider, tmp_path)
    with client:
        assert tools.upload_attachment("123", str(path), comment="Résumé")["attachments"][0]["id"] == "att456"


def test_attachment_download_preserves_bytes_and_drops_all_auth_after_cross_origin(tmp_path):
    calls = []
    def provider(request):
        calls.append(request)
        if len(calls) == 1:
            assert request.headers["Authorization"].startswith("Basic ")
            return httpx.Response(302, headers={"Location": "https://cdn.example/file?signed=download",
                                                "Set-Cookie": "secret=cookie; Domain=.atlassian.net"})
        assert "Authorization" not in request.headers and "Cookie" not in request.headers
        if len(calls) == 2:
            return httpx.Response(302, headers={"Location": "https://example.atlassian.net/wiki/download/file"})
        return httpx.Response(200, content=b"complete file bytes", headers={"Content-Type": "text/plain"})
    client, tools = connect(provider, tmp_path)
    with client:
        result = tools.download_attachment("123", "att456", "guide.txt")
    output = Path(result["path"])
    assert output.is_relative_to(tmp_path / "jobs") and output.read_bytes() == b"complete file bytes"
    assert result["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert result["bytes"] == len(output.read_bytes())
    assert not list(tmp_path.rglob("*.partial"))


def test_gateway_download_other_tenant_never_gets_auth(tmp_path):
    calls = []
    def provider(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={"Location": "https://api.atlassian.com/ex/confluence/other/wiki/download/file"})
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=b"file")
    client, tools = connect(provider, tmp_path, "scoped", CONFLUENCE_CLOUD_ID="cloud-123")
    with client:
        assert tools.download_attachment("123", "456")["ok"] is True


@pytest.mark.parametrize("filename", ["../outside", "/absolute", "..", "folder\\file", "D:escape", "D:", "c:report.txt", "bad\0name"])
def test_download_filename_is_confined(filename, tmp_path):
    calls = []
    client, tools = connect(lambda r: calls.append(r), tmp_path)
    with client, pytest.raises(ConfluenceError) as error:
        tools.download_attachment("123", "456", filename)
    assert error.value.error["code"] == "invalid_argument"
    assert not calls and not list(tmp_path.iterdir())


@pytest.mark.parametrize("filename", ["guide.txt", "Отчёт 2026.pdf", "draft.v2.txt"])
def test_download_plain_filename_preserves_bytes(filename, tmp_path):
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(200, content=b"complete attachment")
    client, tools = connect(provider, tmp_path)
    with client:
        result = tools.download_attachment("123", "456", filename)
    output = Path(result["path"])
    assert result["ok"] and len(calls) == 1
    assert output.name == filename and output.resolve().is_relative_to(tmp_path.resolve() / "jobs")
    assert output.read_bytes() == b"complete attachment"
    assert not list(tmp_path.rglob("*.partial"))


def test_download_rejects_plain_http_redirect_without_credentials(tmp_path):
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "http://cdn.example/file"})
    client, tools = connect(provider, tmp_path)
    with client, pytest.raises(ConfluenceError):
        tools.download_attachment("123", "456")
    assert len(calls) == 1 and not list(tmp_path.rglob("*.partial"))


def test_interrupted_download_removes_partial_file(tmp_path):
    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial bytes"
            raise httpx.ReadError("download interrupted")
    client, tools = connect(lambda _: httpx.Response(200, stream=BrokenStream()), tmp_path)
    with client, pytest.raises(ConfluenceError) as failure:
        tools.download_attachment("123", "456", "test.txt")
    assert failure.value.error["code"] == "download_failed"
    assert not list(tmp_path.rglob("*.partial")) and not list(tmp_path.rglob("test.txt"))


def test_registration_has_only_declared_tools_and_defers_settings(tmp_path):
    class API:
        def __init__(self): self.tools = {}
        def register_tool(self, name, handler, **kwargs): self.tools[name] = (handler, kwargs)
        def get_settings(self, keys):
            assert keys == SETTINGS_KEYS
            return {}
        def get_state_dir(self): return tmp_path
    api = API()
    plugin.register(api)
    assert len(api.tools) == 12
    assert all(len(name) <= 24 for name in api.tools)
    handler, descriptor = api.tools["update_page"]
    assert "expected_version" in descriptor["schema"]["required"]
    assert json.loads(handler(page_id="123", title="T", body="B", expected_version=1))["ok"] is False
