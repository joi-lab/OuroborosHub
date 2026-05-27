import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path


def _load_plugin(tmp_path):
    root = Path(__file__).resolve().parents[1]
    package = types.ModuleType("telegram_bridge_test")
    package.__path__ = [str(root)]
    sys.modules["telegram_bridge_test"] = package
    spec = importlib.util.spec_from_file_location("telegram_bridge_test.plugin", root / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeApi:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)

    def get_state_dir(self):
        return str(self.state_dir)

    def get_settings(self, keys):
        return {"TELEGRAM_BOT_TOKEN": "token"}

    def get_skill_token(self):
        return types.SimpleNamespace(use_in_request=lambda: "skill-token")

    def log(self, level, message, **fields):
        pass  # no-op for tests


class FakeTelegramClient:
    instances = []

    def __init__(self, token):
        self.token = token
        self.sent = []
        FakeTelegramClient.instances.append(self)

    async def call(self, method, **kwargs):
        return {"ok": True, "result": {}}

    async def get_updates(self, offset):
        return list(self.updates)

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))

    async def send_message_with_inline_keyboard(self, chat_id, text, keyboard):
        self.sent.append((chat_id, text))

    async def answer_callback_query(self, callback_query_id, *, text=""):
        self.sent.append(("cb_answer", callback_query_id, text))


def test_slash_messages_are_not_injected(tmp_path, monkeypatch):
    plugin = _load_plugin(tmp_path)
    (tmp_path / "settings.json").write_text(json.dumps({"TELEGRAM_MAX_UPDATES_PER_POLL": 20}), encoding="utf-8")
    FakeTelegramClient.updates = [
        {"update_id": 1, "message": {"chat": {"id": 42}, "from": {"id": 7}, "text": "/panic"}}
    ]
    monkeypatch.setattr(plugin, "TelegramClient", FakeTelegramClient)
    injected = []
    monkeypatch.setattr(plugin, "_inject", lambda api, payload: injected.append(payload))

    async def stop_sleep(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(plugin.asyncio, "sleep", stop_sleep)
    poller = plugin._make_poller(FakeApi(tmp_path))

    try:
        asyncio.run(poller())
    except asyncio.CancelledError:
        pass

    assert injected == []
    assert FakeTelegramClient.instances[-1].sent


def test_poller_caps_update_batch_and_adds_transport(tmp_path, monkeypatch):
    plugin = _load_plugin(tmp_path)
    (tmp_path / "settings.json").write_text(json.dumps({"TELEGRAM_MAX_UPDATES_PER_POLL": 2}), encoding="utf-8")
    FakeTelegramClient.updates = [
        {"update_id": 1, "message": {"chat": {"id": 42}, "from": {"id": 7, "username": "alice"}, "text": "one"}},
        {"update_id": 2, "message": {"chat": {"id": 43}, "from": {"id": 8}, "text": "two"}},
        {"update_id": 3, "message": {"chat": {"id": 44}, "from": {"id": 9}, "text": "three"}},
    ]
    monkeypatch.setattr(plugin, "TelegramClient", FakeTelegramClient)
    injected = []

    async def fake_inject(api, payload):
        injected.append(payload)

    monkeypatch.setattr(plugin, "_inject", fake_inject)

    async def stop_sleep(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(plugin.asyncio, "sleep", stop_sleep)
    poller = plugin._make_poller(FakeApi(tmp_path))

    try:
        asyncio.run(poller())
    except asyncio.CancelledError:
        pass

    assert len(injected) == 2
    assert injected[0]["transport"] == {
        "kind": "telegram",
        "conversation_id": "42",
        "sender_label": "Telegram (alice)",
    }
    assert "telegram_chat_id" not in injected[0]


def test_legacy_env_chat_id_filters_updates(tmp_path, monkeypatch):
    plugin = _load_plugin(tmp_path)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    FakeTelegramClient.updates = [
        {"update_id": 1, "message": {"chat": {"id": 99}, "from": {"id": 7}, "text": "blocked"}},
        {"update_id": 2, "message": {"chat": {"id": 42}, "from": {"id": 8}, "text": "allowed"}},
    ]
    monkeypatch.setattr(plugin, "TelegramClient", FakeTelegramClient)
    injected = []

    async def fake_inject(api, payload):
        injected.append(payload)

    monkeypatch.setattr(plugin, "_inject", fake_inject)

    async def stop_sleep(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(plugin.asyncio, "sleep", stop_sleep)
    try:
        asyncio.run(plugin._make_poller(FakeApi(tmp_path))())
    except asyncio.CancelledError:
        pass

    assert [item["chat_id"] for item in injected] == [42]


def test_manifest_declares_route_permission():
    manifest = Path(__file__).resolve().parents[1] / "SKILL.md"
    text = manifest.read_text(encoding="utf-8")
    assert "route" in text.split("permissions:", 1)[1].split("]", 1)[0]


def test_markdown_to_telegram_html_placeholder_ordering(tmp_path):
    plugin = _load_plugin(tmp_path)
    from telegram_bridge_test.lib.telegram_api import markdown_to_telegram_html
    # Generate a string with more than 10 backtick blocks
    text = " ".join(f"`block{i}`" for i in range(12))
    result = markdown_to_telegram_html(text)
    # Ensure all placeholders replaced correctly without trailing "0" or "1" on any block
    for i in range(12):
        assert f"<code>block{i}</code>" in result
    assert "CODEPLACEHOLDER" not in result
