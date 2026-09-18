"""Unit and Integration Tests for Google Workspace Skill."""

import asyncio
from pathlib import Path
import json
import pytest
import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

# Generate fixture RSA private keys for testing
_TEST_KEY_1 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_TEST_KEY_PEM_1 = _TEST_KEY_1.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")

_TEST_KEY_2 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_TEST_KEY_PEM_2 = _TEST_KEY_2.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode("utf-8")

VALID_SA_INFO_DICT = {
    "type": "service_account",
    "project_id": "test-project-123",
    "private_key_id": "key-id-abc",
    "private_key": _TEST_KEY_PEM_1,
    "client_email": "test-sa@test-project-123.iam.gserviceaccount.com",
    "client_id": "1234567890",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/test-sa",
}
VALID_SA_JSON_STR = json.dumps(VALID_SA_INFO_DICT)

try:
    from .auth import (
        parse_service_account_info,
        create_signed_jwt,
        get_access_token,
        _compute_credential_fingerprint,
        _TOKEN_CACHE,
    )
    from .client import GoogleWorkspaceClient
    from . import plugin
except ImportError:
    from auth import (
        parse_service_account_info,
        create_signed_jwt,
        get_access_token,
        _compute_credential_fingerprint,
        _TOKEN_CACHE,
    )
    from client import GoogleWorkspaceClient
    import plugin


def test_parse_service_account_info_valid():
    parsed = parse_service_account_info(VALID_SA_JSON_STR)
    assert parsed["client_email"] == "test-sa@test-project-123.iam.gserviceaccount.com"
    assert parsed["type"] == "service_account"


def test_parse_service_account_info_invalid():
    with pytest.raises(ValueError, match="Missing Service Account configuration"):
        parse_service_account_info("")

    with pytest.raises(ValueError, match="not valid JSON"):
        parse_service_account_info("invalid json string")

    invalid_type = json.dumps({"type": "authorized_user", "client_email": "foo", "private_key": "bar", "token_uri": "https://oauth2.googleapis.com/token"})
    with pytest.raises(ValueError, match="Invalid credential type"):
        parse_service_account_info(invalid_type)

    # Malicious or untrusted token_uri
    invalid_token_uri = json.dumps({
        "type": "service_account",
        "client_email": "foo@bar.iam.gserviceaccount.com",
        "private_key": _TEST_KEY_PEM_1,
        "token_uri": "https://attacker.example.com/steal_token",
    })
    with pytest.raises(ValueError, match="Must be a secure Google OAuth2 endpoint"):
        parse_service_account_info(invalid_token_uri)


def test_create_signed_jwt_standard_and_delegated():
    sa_info = parse_service_account_info(VALID_SA_JSON_STR)
    # Standard without sub
    jwt_token = create_signed_jwt(sa_info)
    assert jwt_token
    parts = jwt_token.split(".")
    assert len(parts) == 3

    # With explicit subject
    jwt_token_sub = create_signed_jwt(sa_info, subject="user@domain.com")
    assert jwt_token_sub


def test_get_access_token_mocked():
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={"access_token": "ya29.mock_token_12345", "expires_in": 3600, "token_type": "Bearer"},
        )

    mock_client = httpx.Client(transport=httpx.MockTransport(handle_request))
    token = get_access_token(raw_info=VALID_SA_JSON_STR, http_client=mock_client)
    assert token == "ya29.mock_token_12345"


def test_token_cache_credential_key_and_subject_keyed():
    call_count = 0

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json={"access_token": f"ya29.cached_token_{call_count}", "expires_in": 3600})

    mock_client = httpx.Client(transport=httpx.MockTransport(handle_request))

    # Clear cache for this test key
    fp1 = _compute_credential_fingerprint(VALID_SA_INFO_DICT, subject=None)
    fp2 = _compute_credential_fingerprint(VALID_SA_INFO_DICT, subject="user1@domain.com")
    fp3 = _compute_credential_fingerprint(VALID_SA_INFO_DICT, subject="user2@domain.com")
    _TOKEN_CACHE.pop(fp1, None)
    _TOKEN_CACHE.pop(fp2, None)
    _TOKEN_CACHE.pop(fp3, None)

    t1 = get_access_token(raw_info=VALID_SA_JSON_STR, http_client=mock_client, subject=None)
    assert t1 == "ya29.cached_token_1"
    assert call_count == 1

    # Second call for same standard subject uses cache
    t1_cached = get_access_token(raw_info=VALID_SA_JSON_STR, http_client=mock_client, subject=None)
    assert t1_cached == "ya29.cached_token_1"
    assert call_count == 1

    # Different subject triggers fresh token exchange
    t2 = get_access_token(raw_info=VALID_SA_JSON_STR, http_client=mock_client, subject="user1@domain.com")
    assert t2 == "ya29.cached_token_2"
    assert call_count == 2

    # Different private key with identical metadata triggers distinct fingerprint
    dict_key_2 = dict(VALID_SA_INFO_DICT, private_key=_TEST_KEY_PEM_2)
    fp_key_2 = _compute_credential_fingerprint(dict_key_2, subject=None)
    assert fp_key_2 != fp1


def test_sheets_read():
    def handle_request(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "oauth2.googleapis.com/token" in url_str:
            return httpx.Response(200, json={"access_token": "mock_token", "expires_in": 3600})
        if "sheets.googleapis.com/v4/spreadsheets/sheet123/values/Sheet1!A1" in url_str:
            assert request.url.params["valueRenderOption"] == "FORMATTED_VALUE"
            return httpx.Response(
                200,
                json={"range": "Sheet1!A1:C10", "majorDimension": "ROWS", "values": [["Header1", "Header2"], ["Val1", "Val2"]]},
            )
        return httpx.Response(404, text="Not Found")

    mock_client = httpx.Client(transport=httpx.MockTransport(handle_request))
    with GoogleWorkspaceClient(raw_sa_info=VALID_SA_JSON_STR, http_client=mock_client) as client:
        res = client.sheets_read(spreadsheet_id="sheet123", range_name="Sheet1!A1:C10")
        assert res["spreadsheet_id"] == "sheet123"
        assert res["row_count"] == 2
        assert res["cell_count"] == 4
        assert res["values"][0] == ["Header1", "Header2"]
        assert res["value_render_option"] == "FORMATTED_VALUE"


@pytest.mark.parametrize(("render", "value"), [
    ("FORMATTED_VALUE", "$3.00"), ("UNFORMATTED_VALUE", 3), ("FORMULA", "=SUM(A1:A2)"),
])
def test_sheets_read_render_options(render, value):
    def respond(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "fixture-token"})
        assert request.url.path == "/v4/spreadsheets/sheet123/values/Totals!B2"
        assert request.url.params["valueRenderOption"] == render
        return httpx.Response(200, json={"range": "Totals!B2", "values": [[value]]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with GoogleWorkspaceClient(VALID_SA_JSON_STR, http) as client:
            result = client.sheets_read("sheet123", "Totals!B2", value_render_option=render)
    assert result["values"] == [[value]]
    assert result["value_render_option"] == render
    assert result["range"] == "Totals!B2"


def test_sheets_read_rejects_unknown_render_option_before_request():
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected HTTP call"))) as http:
        with GoogleWorkspaceClient(VALID_SA_JSON_STR, http) as client:
            with pytest.raises(ValueError, match="value_render_option"):
                client.sheets_read("sheet123", "A1", value_render_option="RAW")


def test_sheets_info_discovers_tabs_without_cell_data():
    def respond(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "fixture-token"})
        assert request.url.path == "/v4/spreadsheets/sheet123"
        fields = request.url.params["fields"]
        assert "properties(title,locale,timeZone)" in fields
        assert "gridProperties" in fields
        assert "data" not in fields
        assert request.url.params.get("includeGridData") != "true"
        return httpx.Response(200, json={
            "spreadsheetId": "sheet123", "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/sheet123/edit",
            "properties": {"title": "Quarterly workbook", "locale": "en_GB", "timeZone": "Etc/UTC"},
            "sheets": [
                {"properties": {"sheetId": 0, "title": "Overview", "index": 0, "sheetType": "GRID",
                                "hidden": False, "gridProperties": {"rowCount": 1000, "columnCount": 26, "frozenRowCount": 1}}},
                {"properties": {"sheetId": 7, "title": "Вторая вкладка", "index": 1, "sheetType": "OBJECT", "hidden": True}},
            ],
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with GoogleWorkspaceClient(VALID_SA_JSON_STR, http) as client:
            result = client.sheets_info("sheet123")
    assert (result["title"], result["locale"], result["time_zone"]) == ("Quarterly workbook", "en_GB", "Etc/UTC")
    assert result["sheets"][0] == {"sheet_id": 0, "title": "Overview", "index": 0, "sheet_type": "GRID",
                                    "hidden": False, "grid_properties": {"rowCount": 1000, "columnCount": 26, "frozenRowCount": 1}}
    assert result["sheets"][1]["title"] == "Вторая вкладка"
    assert "grid_properties" not in result["sheets"][1]


def test_sheets_append():
    def handle_request(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "oauth2.googleapis.com/token" in url_str:
            return httpx.Response(200, json={"access_token": "mock_token", "expires_in": 3600})
        if "values/Sheet1!A1:append" in url_str:
            return httpx.Response(
                200,
                json={"spreadsheet_id": "sheet123", "tableRange": "Sheet1!A1:B2", "updates": {"updatedRows": 1, "updatedCells": 2}},
            )
        return httpx.Response(404, text="Not Found")

    mock_client = httpx.Client(transport=httpx.MockTransport(handle_request))
    with GoogleWorkspaceClient(raw_sa_info=VALID_SA_JSON_STR, http_client=mock_client) as client:
        res = client.sheets_append(spreadsheet_id="sheet123", range_name="Sheet1!A1", rows=[["NewVal1", "NewVal2"]])
        assert res["updated_rows"] == 1


def test_docs_create_blank():
    def handle_request(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "oauth2.googleapis.com/token" in url_str:
            return httpx.Response(200, json={"access_token": "mock_token", "expires_in": 3600})
        if request.method == "POST" and "docs.googleapis.com/v1/documents" in url_str:
            return httpx.Response(200, json={"documentId": "doc12345", "title": "My New Document"})
        if request.method == "GET" and "drive/v3/files/doc12345" in url_str:
            return httpx.Response(200, json={"parents": ["root_parent_id"]})
        if request.method == "PATCH" and "drive/v3/files/doc12345" in url_str:
            assert "addParents=folder789" in str(request.url.params) or request.url.params.get("addParents") == "folder789"
            assert "removeParents=root_parent_id" in str(request.url.params) or request.url.params.get("removeParents") == "root_parent_id"
            return httpx.Response(200, json={"id": "doc12345"})
        return httpx.Response(404, text="Not Found")

    mock_client = httpx.Client(transport=httpx.MockTransport(handle_request))
    with GoogleWorkspaceClient(raw_sa_info=VALID_SA_JSON_STR, http_client=mock_client) as client:
        res = client.docs_create(title="My New Document", folder_id="folder789")
        assert res["document_id"] == "doc12345"
        assert res["url"] == "https://docs.google.com/document/d/doc12345/edit"
        assert res["folder_id"] == "folder789"


def test_docs_create_from_template():
    def handle_request(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "oauth2.googleapis.com/token" in url_str:
            return httpx.Response(200, json={"access_token": "mock_token", "expires_in": 3600})
        if "drive/v3/files/template999/copy" in url_str:
            return httpx.Response(200, json={"id": "doc_from_template_777", "name": "Copied Doc"})
        return httpx.Response(404, text="Not Found")

    mock_client = httpx.Client(transport=httpx.MockTransport(handle_request))
    with GoogleWorkspaceClient(raw_sa_info=VALID_SA_JSON_STR, http_client=mock_client) as client:
        res = client.docs_create(title="Copied Doc", folder_id="folder_abc", template_id="template999")
        assert res["document_id"] == "doc_from_template_777"
        assert res["url"] == "https://docs.google.com/document/d/doc_from_template_777/edit"


def test_drive_list():
    def handle_request(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "oauth2.googleapis.com/token" in url_str:
            return httpx.Response(200, json={"access_token": "mock_token", "expires_in": 3600})
        if "drive/v3/files" in url_str:
            assert "'folder123' in parents" in request.url.params.get("q", "")
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "id": "file1",
                            "name": "Report.docx",
                            "mimeType": "application/vnd.google-apps.document",
                            "webViewLink": "https://docs.google.com/document/d/file1/edit",
                            "parents": ["folder123"],
                        }
                    ]
                },
            )
        return httpx.Response(404, text="Not Found")

    mock_client = httpx.Client(transport=httpx.MockTransport(handle_request))
    with GoogleWorkspaceClient(raw_sa_info=VALID_SA_JSON_STR, http_client=mock_client) as client:
        res = client.drive_list(folder_id="folder123")
        assert res["count"] == 1
        assert res["files"][0]["name"] == "Report.docx"


def test_drive_list_invalid_id():
    mock_client = httpx.Client()
    with GoogleWorkspaceClient(raw_sa_info=VALID_SA_JSON_STR, http_client=mock_client) as client:
        with pytest.raises(ValueError, match="Invalid folder_id format"):
            client.drive_list(folder_id="folder' or trashed=true or 'a'='a")


def test_drive_read_text_doc_export():
    def handle_request(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "oauth2.googleapis.com/token" in url_str:
            return httpx.Response(200, json={"access_token": "mock_token", "expires_in": 3600})
        if request.url.path == "/drive/v3/files/file_doc" and request.url.params.get("alt") != "media":
            assert {"id", "name", "mimeType", "size", "owners", "version"} <= set(request.url.params["fields"].split(","))
            return httpx.Response(
                200,
                json={"id": "file_doc", "name": "Sample Doc", "mimeType": "application/vnd.google-apps.document", "size": "1024"},
            )
        if "drive/v3/files/file_doc/export?mimeType=text%2Fplain" in url_str:
            return httpx.Response(200, text="Hello Google Docs export plain text!")
        return httpx.Response(404, text="Not Found")

    mock_client = httpx.Client(transport=httpx.MockTransport(handle_request))
    with GoogleWorkspaceClient(raw_sa_info=VALID_SA_JSON_STR, http_client=mock_client) as client:
        res = client.drive_read_text(file_id="file_doc")
        assert res["file_id"] == "file_doc"
        assert res["name"] == "Sample Doc"
        assert "Hello Google Docs" in res["text"]
        assert res["remote_size_bytes"] == 1024
        assert res["truncated"] is False


@pytest.mark.parametrize("operation", ["drive_list", "drive_read_text"])
@pytest.mark.parametrize("shared_drive", [False, True])
def test_file_metadata_preserves_provider_fields_and_absence(operation, shared_drive):
    metadata = {"id": "file_meta", "name": "Working notes", "mimeType": "text/plain",
                "modifiedTime": "2026-01-01T12:30:00Z", "version": "17", "capabilities": {"canEdit": False}}
    if not shared_drive:
        metadata.update(owners=[{"displayName": "Document owner", "emailAddress": "owner@example.invalid"}],
                        lastModifyingUser={"displayName": "Editor", "permissionId": "editor-id"},
                        webViewLink="https://drive.google.com/file/d/file_meta/view")

    def respond(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "fixture-token"})
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, text="A complete short note.")
        fields = request.url.params["fields"]
        for name in ("owners", "lastModifyingUser", "version", "modifiedTime", "webViewLink", "capabilities"):
            assert name in fields
        if request.url.path == "/drive/v3/files":
            return httpx.Response(200, json={"files": [metadata], "nextPageToken": "next-batch"})
        assert request.url.path == "/drive/v3/files/file_meta"
        return httpx.Response(200, json=metadata)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with GoogleWorkspaceClient(VALID_SA_JSON_STR, http) as client:
            if operation == "drive_list":
                page = client.drive_list()
                assert page["next_page_token"] == "next-batch"
                result = page["files"][0]
            else:
                result = client.drive_read_text("file_meta")
    assert result["version"] == "17"
    assert result["modified_time"] == metadata["modifiedTime"]
    assert result["capabilities"] == {"canEdit": False}
    if shared_drive:
        assert "owners" not in result
        assert "last_modifying_user" not in result
        assert result["url"] == "https://drive.google.com/open?id=file_meta"
    else:
        assert result["owners"] == metadata["owners"]
        assert result["last_modifying_user"] == metadata["lastModifyingUser"]
        assert result["url"] == metadata["webViewLink"]


@pytest.mark.parametrize(("max_chars", "truncated"), [(1000, False), (3, True)])
def test_spreadsheet_csv_scope_is_separate_from_character_clipping(max_chars, truncated):
    def respond(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "fixture-token"})
        if request.url.path == "/drive/v3/files/sheet123":
            return httpx.Response(200, json={"id": "sheet123", "name": "Workbook",
                                           "mimeType": "application/vnd.google-apps.spreadsheet"})
        assert request.url.path == "/drive/v3/files/sheet123/export"
        assert request.url.params["mimeType"] == "text/csv"
        return httpx.Response(200, text="Header,Value\nEntry,1\n")

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with GoogleWorkspaceClient(VALID_SA_JSON_STR, http) as client:
            result = client.drive_read_text("sheet123", max_chars=max_chars)
    assert result["export_scope"] == "first_sheet_only"
    assert result["truncated"] is truncated
    assert "sheets_info" in result["export_note"] and "sheets_read" in result["export_note"]


def test_drive_read_text_clamped_max_chars():
    def handle_request(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "oauth2.googleapis.com/token" in url_str:
            return httpx.Response(200, json={"access_token": "mock_token", "expires_in": 3600})
        if "drive/v3/files/file_long" in url_str and "export" not in url_str:
            return httpx.Response(
                200,
                json={"id": "file_long", "name": "Long Doc", "mimeType": "application/vnd.google-apps.document"},
            )
        if "drive/v3/files/file_long/export" in url_str:
            return httpx.Response(200, text="0123456789" * 10)
        return httpx.Response(404, text="Not Found")

    mock_client = httpx.Client(transport=httpx.MockTransport(handle_request))
    with GoogleWorkspaceClient(raw_sa_info=VALID_SA_JSON_STR, http_client=mock_client) as client:
        res = client.drive_read_text(file_id="file_long", max_chars=20)
        assert res["truncated"] is True
        assert len(res["text"].split("\n\n")[0]) == 20


def test_drive_read_text_unsupported_mime():
    def handle_request(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "oauth2.googleapis.com/token" in url_str:
            return httpx.Response(200, json={"access_token": "mock_token", "expires_in": 3600})
        if "drive/v3/files/file_image" in url_str:
            return httpx.Response(
                200,
                json={"id": "file_image", "name": "Photo.png", "mimeType": "image/png"},
            )
        return httpx.Response(404, text="Not Found")

    mock_client = httpx.Client(transport=httpx.MockTransport(handle_request))
    with GoogleWorkspaceClient(raw_sa_info=VALID_SA_JSON_STR, http_client=mock_client) as client:
        with pytest.raises(ValueError, match="Unsupported file type for text export: 'image/png'"):
            client.drive_read_text(file_id="file_image")


def test_plugin_registration_and_settings_save(tmp_path, monkeypatch):
    class MockPluginAPI:
        def __init__(self, state_dir: Path):
            self.state_dir = state_dir
            self.tools = {}
            self.routes = {}
            self.settings_sections = {}
            self.ui_tabs = {}
            self.unloaded = False
            self.logs = []

        def get_state_dir(self) -> str:
            # Matches the PluginAPI contract returning str
            return str(self.state_dir)

        def get_settings(self, keys):
            # Matches the PluginAPI contract taking sequence of keys
            res = {}
            if "GOOGLE_SERVICE_ACCOUNT_JSON" in keys:
                res["GOOGLE_SERVICE_ACCOUNT_JSON"] = VALID_SA_JSON_STR
            return res

        def register_tool(self, name, handler, description, schema, timeout_sec=60):
            self.tools[name] = {"handler": handler, "description": description, "schema": schema}

        def register_route(self, path, handler, methods=("GET",)):
            self.routes[path] = {"handler": handler, "methods": methods}

        def register_settings_section(self, section_id, title, schema):
            self.settings_sections[section_id] = {"title": title, "schema": schema}

        def register_ui_tab(self, tab_id, title, render, icon=None):
            self.ui_tabs[tab_id] = {"title": title, "render": render, "icon": icon}

        def on_unload(self, callback):
            self.unload_callback = callback

        def log(self, level, message):
            self.logs.append((level, message))

    mock_api = MockPluginAPI(tmp_path)
    plugin.register(mock_api)

    # Check registered tools
    expected_tools = {"workspace_auth_status", "sheets_info", "sheets_read", "sheets_append", "docs_create", "drive_list", "drive_read_text"}
    assert expected_tools.issubset(set(mock_api.tools.keys()))
    render_schema = mock_api.tools["sheets_read"]["schema"]["properties"]["value_render_option"]
    assert render_schema["enum"] == ["FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"]
    assert render_schema["default"] == "FORMATTED_VALUE"
    assert mock_api.tools["sheets_info"]["schema"]["required"] == ["spreadsheet_id"]

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def sheets_info(self, **kwargs):
            assert kwargs == {"spreadsheet_id": "sheet123"}
            return {"title": "Workbook", "sheets": [{"sheet_id": 0, "title": "Overview"}]}

        def sheets_read(self, **kwargs):
            assert kwargs == {"spreadsheet_id": "sheet123", "range_name": "Overview!A1", "max_rows": 2,
                              "value_render_option": "FORMULA"}
            return {"values": [["=1+2"]], "value_render_option": "FORMULA"}

    monkeypatch.setattr(plugin, "GoogleWorkspaceClient", FakeClient)
    assert json.loads(mock_api.tools["sheets_info"]["handler"]("sheet123"))["sheets"][0]["sheet_id"] == 0
    assert json.loads(mock_api.tools["sheets_read"]["handler"]("sheet123", "Overview!A1", 2, "FORMULA"))["values"] == [["=1+2"]]

    # Check UI tab render kind
    assert mock_api.ui_tabs["google_workspace"]["render"]["kind"] == "declarative"

    # Check settings section form method
    form_comp = mock_api.settings_sections["google_workspace"]["schema"]["components"][1]
    assert form_comp["type"] == "form"
    assert form_comp["route"] == "settings/save"
    assert form_comp["method"] == "POST"

    # Test settings save route handler
    save_handler = mock_api.routes["settings/save"]["handler"]

    class MockRequest:
        async def json(self):
            return {"DEFAULT_FOLDER_ID": "folder_default_123", "TEMPLATES_JSON": '{"weekly_report": "doc_tmpl_456"}'}

    resp = asyncio.run(save_handler(MockRequest()))
    assert resp.status_code == 200
    saved_file = tmp_path / "settings.json"
    assert saved_file.is_file()
    saved_data = json.loads(saved_file.read_text(encoding="utf-8"))
    assert saved_data["DEFAULT_FOLDER_ID"] == "folder_default_123"

    # Test resolve folder and template ID using the saved settings
    assert plugin._resolve_folder_id(mock_api, None) == "folder_default_123"
    assert plugin._resolve_template_id(mock_api, "weekly_report") == "doc_tmpl_456"
