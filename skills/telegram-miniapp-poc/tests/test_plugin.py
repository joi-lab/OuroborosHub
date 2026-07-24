from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest


PLUGIN_PATH = Path(__file__).parents[1] / "plugin.py"
SPEC = importlib.util.spec_from_file_location("telegram_miniapp_poc_plugin", PLUGIN_PATH)
assert SPEC and SPEC.loader
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


class FakeAPI:
    def __init__(self, root: Path, *, port: int = 8765) -> None:
        self.data_dir = root / "data"
        self.state_dir = root / "skill-state"
        self.state_dir.mkdir(parents=True)
        bridge = self.data_dir / "state" / "skills" / "telegram-bridge"
        bridge.mkdir(parents=True)
        (bridge / "settings.json").write_text(
            json.dumps({"TELEGRAM_CHAT_ID": "12345"}),
            encoding="utf-8",
        )
        self.port = port
        self.routes: list[tuple[str, tuple[str, ...], Any]] = []
        self.companions: list[str] = []
        self.sections: list[tuple[str, str, dict[str, Any]]] = []
        self.logs: list[tuple[str, str]] = []

    @staticmethod
    def _assert_tool_name(name: str) -> None:
        candidate = str(name or "").strip()
        if not candidate or len(candidate) > 64 or not candidate.replace("_", "").isalnum():
            raise ValueError(f"tool name must be alnum/underscore only: {candidate!r}")

    def get_runtime_info(self) -> dict[str, Any]:
        return {
            "data_dir": str(self.data_dir),
            "state_dir": str(self.state_dir),
            "server_port": self.port,
        }

    def get_state_dir(self) -> str:
        return str(self.state_dir)

    def register_route(self, name: str, handler: Any, methods: tuple[str, ...]) -> None:
        self._assert_tool_name(name)
        self.routes.append((name, methods, handler))

    def register_settings_section(self, section_id: str, title: str, schema: dict[str, Any]) -> None:
        self._assert_tool_name(section_id)
        self.sections.append((section_id, title, schema))

    def register_companion_process(self, name: str) -> None:
        self._assert_tool_name(name)
        self.companions.append(name)

    def log(self, level: str, message: str) -> None:
        self.logs.append((level, message))


def response_json(response: Any) -> dict[str, Any]:
    return json.loads(response.body.decode("utf-8"))


def test_register_writes_nonsecret_runtime_config_and_companion(tmp_path: Path) -> None:
    api = FakeAPI(tmp_path, port=9012)
    plugin.register(api)
    config_path = api.state_dir / plugin._CONFIG_NAME
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config == {
        "schema": 2,
        "core_port": 9012,
        "owner_chat_id": 12345,
        "button_text": "Ouroboros",
        "tunnel": "cloudflare_quick",
    }
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert api.companions == ["miniapp_gateway"]
    assert [(name, methods) for name, methods, _handler in api.routes] == [
        ("status", ("POST",))
    ]
    assert api.sections[0][0] == "telegram_miniapp_poc"


def test_bridge_binding_waits_for_private_owner_and_rejects_symlink(tmp_path: Path) -> None:
    api = FakeAPI(tmp_path)
    settings = api.data_dir / "state" / "skills" / "telegram-bridge" / "settings.json"
    settings.write_text(json.dumps({"TELEGRAM_CHAT_ID": "-100123"}), encoding="utf-8")
    plugin.register(api)
    config = json.loads((api.state_dir / plugin._CONFIG_NAME).read_text(encoding="utf-8"))
    assert config["owner_chat_id"] == 0

    target = tmp_path / "other.json"
    target.write_text(json.dumps({"TELEGRAM_CHAT_ID": "999"}), encoding="utf-8")
    settings.unlink()
    settings.symlink_to(target)
    with pytest.raises(plugin.ConfigurationError, match="symlink"):
        plugin.register(api)


def test_invalid_core_port_fails_before_companion(tmp_path: Path) -> None:
    api = FakeAPI(tmp_path, port=0)
    with pytest.raises(plugin.ConfigurationError, match="port"):
        plugin.register(api)
    assert api.companions == []


@pytest.mark.parametrize(
    ("system", "machine"),
    [
        ("Darwin", "arm64"),
        ("Darwin", "x86_64"),
        ("Linux", "aarch64"),
        ("Linux", "amd64"),
        ("Windows", "AMD64"),
    ],
)
def test_supported_platform_matrix(
    monkeypatch: pytest.MonkeyPatch, system: str, machine: str
) -> None:
    monkeypatch.setattr(plugin.platform, "system", lambda: system)
    monkeypatch.setattr(plugin.platform, "machine", lambda: machine)
    plugin._require_supported_platform()


def test_windows_status_uses_heartbeat_without_destructive_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeAPI(tmp_path)
    plugin.register(api)
    monkeypatch.setattr(plugin.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        plugin.os,
        "kill",
        lambda *_args: pytest.fail("Windows status must never call os.kill(pid, 0)"),
    )
    (api.state_dir / plugin._STATUS_NAME).write_text(
        json.dumps(
            {
                "state": "ready",
                "message": "ok",
                "public_url": "https://abc.trycloudflare.com/",
                "pid": 4242,
                "updated_at_epoch": int(time.time()),
            }
        ),
        encoding="utf-8",
    )
    assert plugin._read_status(api)["state"] == "ready"


def test_status_route_returns_only_bounded_diagnostics(tmp_path: Path) -> None:
    api = FakeAPI(tmp_path)
    plugin.register(api)
    extra_key = "TELEGRAM_BOT_TOKEN"
    sentinel = "must-not-leak"
    (api.state_dir / plugin._STATUS_NAME).write_text(
        json.dumps(
            {
                "state": "ready",
                "message": "Existing SPA is available",
                "public_url": "https://abc.trycloudflare.com/",
                "cloudflared_version": "2026.7.2",
                "pid": os.getpid(),
                "updated_at_epoch": int(time.time()),
                "reason_code": "healthy",
                "attempt": 0,
                "last_ready_at_epoch": int(time.time()),
                "next_retry_at_epoch": 0,
                extra_key: sentinel,
                "init_data": sentinel,
            }
        ),
        encoding="utf-8",
    )
    handler = api.routes[0][2]
    response = asyncio.run(handler(None))
    body = response_json(response)
    assert body["ok"] is True
    assert body["state"] == "ready"
    assert body["message"] == "Existing SPA is available"
    assert body["public_url"] == "https://abc.trycloudflare.com/"
    assert body["cloudflared_version"] == "2026.7.2"
    assert body["reason_code"] == "healthy"
    assert sentinel not in response.body.decode("utf-8")


def test_status_hides_dead_companion_url(tmp_path: Path) -> None:
    api = FakeAPI(tmp_path)
    plugin.register(api)
    (api.state_dir / plugin._STATUS_NAME).write_text(
        json.dumps(
            {
                "state": "ready",
                "message": "old ready state",
                "public_url": "https://dead.trycloudflare.com/",
                "pid": 999_999_999,
                "updated_at_epoch": int(time.time()),
            }
        ),
        encoding="utf-8",
    )
    body = response_json(asyncio.run(api.routes[0][2](None)))
    assert body["state"] == "stale"
    assert body["reason_code"] == "heartbeat_stale"
    assert "public_url" not in body


def test_registration_status_becomes_stale_without_companion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    monkeypatch.setattr(plugin.time, "time", lambda: now)
    api = FakeAPI(tmp_path)
    plugin.register(api)
    assert plugin._read_status(api)["state"] == "starting"
    monkeypatch.setattr(plugin.time, "time", lambda: now + 46)
    stale = plugin._read_status(api)
    assert stale["state"] == "stale"
    assert stale["reason_code"] == "heartbeat_stale"
