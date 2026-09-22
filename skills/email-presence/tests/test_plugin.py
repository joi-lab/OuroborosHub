import importlib.util
import json
from pathlib import Path
import sys
import types

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


def _settings_app(tmp_path, settings=None):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "email_settings_fixture", root / "plugin.py", submodule_search_locations=[str(root)]
    )
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)

    class API:
        routes = []
        def get_state_dir(self):
            return str(tmp_path)
        def get_settings(self, keys):
            return {key: value for key, value in (settings or {}).items() if key in keys}
        def register_route(self, path, handler, methods):
            self.routes.append(Route("/" + path, handler, methods=list(methods)))
        def register_companion_process(self, *args, **kwargs):
            pass
        def register_tool(self, *args, **kwargs):
            pass
        def register_settings_section(self, *args, **kwargs):
            pass
        def register_ui_tab(self, *args, **kwargs):
            pass

    api = API()
    plugin.register(api)
    return Starlette(routes=api.routes)


def test_settings_hydration_roundtrip_preserves_binding_and_allows_explicit_clear(tmp_path):
    saved = {"binding_id": "a" * 32, "folder": "Тестовая папка", "poll_interval_sec": 75}
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({**saved, "EMAIL_PASSWORD": "must-not-return"}), encoding="utf-8")
    before = path.read_bytes()
    with TestClient(_settings_app(tmp_path)) as client:
        values = client.get("/settings/save").json()
        assert values == saved
        assert path.read_bytes() == before
        response = client.post("/settings/save", json={**values, "folder": "Archive"})
        assert response.status_code == 200
        assert client.get("/settings/save").json() == {**saved, "folder": "Archive"}
        response = client.post("/settings/save", json={"poll_interval_sec": ""})
        assert response.status_code == 200
        assert client.get("/settings/save").json()["poll_interval_sec"] == 75
        response = client.post("/settings/save", json={"binding_id": ""})
        assert response.status_code == 200
        assert client.get("/settings/save").json()["binding_id"] == ""


def test_settings_hydration_defaults_and_blank_poll_interval(tmp_path):
    with TestClient(_settings_app(tmp_path, {"EMAIL_DEFAULT_FOLDER": "Configured", "EMAIL_PASSWORD": "hidden"})) as client:
        response = client.get("/settings/save")
        assert response.status_code == 200
        assert response.json() == {"binding_id": "", "folder": "Configured", "poll_interval_sec": 30}
        assert not (tmp_path / "settings.json").exists()
        assert client.post("/settings/save", json={"poll_interval_sec": " "}).status_code == 200
        assert client.get("/settings/save").json()["poll_interval_sec"] == 30
        assert client.post("/settings/save", json={"poll_interval_sec": 15}).status_code == 200
        assert client.get("/settings/save").json()["poll_interval_sec"] == 15


def test_registered_tools_and_status_share_durable_store(tmp_path):
    root = Path(__file__).resolve().parents[1]
    package = types.ModuleType("test_email_skill")
    package.__path__ = [str(root)]
    sys.modules[package.__name__] = package
    spec = importlib.util.spec_from_file_location("test_email_skill.plugin", root / "plugin.py")
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    class API:
        def __init__(self):
            self.tools = {}
            self.tabs = []
        def get_state_dir(self):
            return str(tmp_path)
        def get_settings(self, keys):
            return {}
        def register_companion_process(self, name):
            assert name == "email_poller"
        def register_tool(self, name, handler, **kw):
            self.tools[name] = handler
        def register_route(self, *a, **kw):
            pass
        def register_settings_section(self, *a, **kw):
            pass
        def register_ui_tab(self, *a, **kw):
            self.tabs.append(kw["render"])
    api = API()
    plugin.register(api)
    expected = {"email_send", "email_search", "email_read", "email_mailbox", "email_draft", "email_receipt", "email_test_connection"}
    assert set(api.tools) == expected
    first = api.tools["email_send"](to="a@example.org", body="Hello", request_id="stable")
    duplicate = api.tools["email_send"](to="a@example.org", body="Hello", request_id="stable")
    assert duplicate["deduplicated"]
    assert first["receipt"]["state"] == "pending"
    assert api.tools["email_receipt"]("stable")["provider_message_id"] == first["receipt"]["provider_message_id"]
    assert api.tabs[0]["components"][0]["method"] == "GET"
