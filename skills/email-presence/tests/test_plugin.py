import importlib.util
from pathlib import Path
import sys
import types


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
