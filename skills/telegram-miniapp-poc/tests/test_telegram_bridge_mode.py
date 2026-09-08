from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest


SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import telegram_bridge_mode as bridge_mode  # noqa: E402


def _layout(tmp_path: Path, *, enabled: bool, mode: str = "all") -> tuple[Path, Path]:
    skills = tmp_path / "data" / "state" / "skills"
    state = skills / "telegram-miniapp-poc"
    bridge = skills / "telegram-bridge"
    state.mkdir(parents=True)
    bridge.mkdir()
    (bridge / "settings.json").write_text(
        json.dumps({"TELEGRAM_CHAT_ID": "12345", "TELEGRAM_MIRROR_MODE": mode}),
        encoding="utf-8",
    )
    (bridge / "enabled.json").write_text(json.dumps({"enabled": enabled}), encoding="utf-8")
    return state, bridge / "settings.json"


class _Response:
    status_code = 200

    @staticmethod
    def json() -> dict[str, bool]:
        return {"ok": True}


class _Client:
    def __init__(self, settings: Path, *, lose_reply: bool = False) -> None:
        self.settings = settings
        self.lose_reply = lose_reply
        self.calls: list[str] = []

    async def post(self, _url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _Response:
        assert headers == {"Accept": "application/json"}
        value = str(json["TELEGRAM_MIRROR_MODE"])
        payload = __import__("json").loads(self.settings.read_text(encoding="utf-8"))
        payload["TELEGRAM_MIRROR_MODE"] = value
        self.settings.write_text(__import__("json").dumps(payload), encoding="utf-8")
        self.calls.append(value)
        if self.lose_reply:
            self.lose_reply = False
            request = httpx.Request("POST", "http://127.0.0.1/")
            raise httpx.ConnectError("lost reply", request=request)
        return _Response()


def test_disabled_bridge_needs_no_live_route(tmp_path: Path) -> None:
    state, settings = _layout(tmp_path, enabled=False)
    client = _Client(settings)
    manager = bridge_mode.BridgeMirrorModeManager(state, 8765, client=client)
    assert asyncio.run(manager.activate()) is False
    assert manager.safe_for_exposure()
    assert client.calls == []
    assert not manager.ledger_path.exists()


def test_enabled_bridge_activate_and_restore_are_transactional(tmp_path: Path) -> None:
    state, settings = _layout(tmp_path, enabled=True)
    client = _Client(settings)
    manager = bridge_mode.BridgeMirrorModeManager(state, 8765, client=client)
    assert asyncio.run(manager.activate(12345)) is True
    assert manager.current() == "telegram_only"
    assert manager.safe_for_exposure(12345)
    assert not manager.safe_for_exposure(54321)
    assert manager.owned_owner_chat_id() == 12345
    ledger = json.loads(manager.ledger_path.read_text(encoding="utf-8"))
    assert ledger["owner_chat_id"] == 12345
    assert manager.ledger_path.is_file()
    with pytest.raises(bridge_mode.BridgeModeConflictError, match="prior owner"):
        asyncio.run(manager.activate(54321))
    assert asyncio.run(manager.restore()) is True
    assert manager.current() == "all"
    assert client.calls == ["telegram_only", "all"]
    assert not manager.ledger_path.exists()
    assert manager.owned_owner_chat_id() == 0


def test_lost_route_reply_reconciles_without_second_write(tmp_path: Path) -> None:
    state, settings = _layout(tmp_path, enabled=True)
    client = _Client(settings, lose_reply=True)
    manager = bridge_mode.BridgeMirrorModeManager(state, 8765, client=client)
    with pytest.raises(bridge_mode.BridgeModeTransportError):
        asyncio.run(manager.activate())
    assert manager.current() == "telegram_only"
    assert asyncio.run(manager.activate()) is False
    assert client.calls == ["telegram_only"]
    assert json.loads(manager.ledger_path.read_text(encoding="utf-8"))["phase"] == "installed"


def test_external_mode_restore_is_never_overwritten(tmp_path: Path) -> None:
    state, settings = _layout(tmp_path, enabled=True)
    client = _Client(settings)
    manager = bridge_mode.BridgeMirrorModeManager(state, 8765, client=client)
    assert asyncio.run(manager.activate()) is True
    payload = json.loads(settings.read_text(encoding="utf-8"))
    payload["TELEGRAM_MIRROR_MODE"] = "all"
    settings.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(bridge_mode.BridgeModeConflictError, match="outside this skill"):
        asyncio.run(manager.activate())
    assert client.calls == ["telegram_only"]
    assert manager.current() == "all"


def test_unsafe_settings_link_fails_closed(tmp_path: Path) -> None:
    state, settings = _layout(tmp_path, enabled=True)
    target = tmp_path / "outside.json"
    target.write_text(settings.read_text(encoding="utf-8"), encoding="utf-8")
    settings.unlink()
    settings.symlink_to(target)
    manager = bridge_mode.BridgeMirrorModeManager(state, 8765, client=_Client(target))
    with pytest.raises(bridge_mode.BridgeModeConflictError, match="unsafe link"):
        manager.owner_chat_id()
