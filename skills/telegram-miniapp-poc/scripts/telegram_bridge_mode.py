"""Crash-safe, conflict-averse coordination with telegram-bridge mirror mode."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

import httpx

from platform_support import fsync_directory, path_is_link_or_reparse


_LEDGER_NAME = "bridge_mirror_snapshot.json"
_LEDGER_SCHEMA = 2
_TARGET_MODE = "telegram_only"
_DEFAULT_MODE = "all"
_MAX_SETTINGS_BYTES = 64 * 1024


class BridgeModeError(RuntimeError):
    pass


class BridgeModeTransportError(BridgeModeError):
    pass


class BridgeModeConflictError(BridgeModeError):
    pass


class BridgeMirrorModeManager:
    """Own only the mirror-mode value installed through bridge's public route."""

    def __init__(
        self,
        state_dir: str | Path,
        core_port: int,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        if not 1 <= int(core_port) <= 65_535:
            raise BridgeModeError("Ouroboros core port is invalid.")
        self._route = (
            f"http://127.0.0.1:{int(core_port)}"
            "/api/extensions/telegram-bridge/settings/save"
        )
        self._client = client

    @property
    def ledger_path(self) -> Path:
        path = self.state_dir / _LEDGER_NAME
        try:
            path.relative_to(self.state_dir)
        except ValueError as exc:
            raise BridgeModeError("Bridge mode ledger escaped private skill state.") from exc
        if path_is_link_or_reparse(path):
            raise BridgeModeConflictError("Bridge mode ledger is an unsafe link.")
        return path

    @property
    def settings_path(self) -> Path:
        skills_root = self.state_dir.parent
        path = skills_root / "telegram-bridge" / "settings.json"
        for candidate in (skills_root, path.parent, path):
            if path_is_link_or_reparse(candidate):
                raise BridgeModeConflictError("Telegram bridge settings cross an unsafe link.")
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(skills_root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise BridgeModeTransportError("Telegram bridge settings are unavailable.") from exc
        return resolved

    @property
    def enabled_path(self) -> Path:
        skills_root = self.state_dir.parent
        path = skills_root / "telegram-bridge" / "enabled.json"
        for candidate in (skills_root, path.parent, path):
            if path_is_link_or_reparse(candidate):
                raise BridgeModeConflictError("Telegram bridge enablement crosses an unsafe link.")
        try:
            path.resolve().relative_to(skills_root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise BridgeModeTransportError("Telegram bridge enablement is unavailable.") from exc
        return path

    def bridge_enabled(self) -> bool:
        """Return persisted desired enablement; a missing file means disabled."""

        path = self.enabled_path
        if not path.exists():
            return False
        try:
            if path.stat().st_size > 16_384:
                raise BridgeModeConflictError("Telegram bridge enablement is oversized.")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except BridgeModeConflictError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise BridgeModeTransportError("Telegram bridge enablement is unreadable.") from exc
        if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
            raise BridgeModeConflictError("Telegram bridge enablement is invalid.")
        return bool(payload["enabled"])

    def safe_for_exposure(self, owner_chat_id: int | None = None) -> bool:
        """Disabled text bridge is safe; enabled bridge must be telegram-only."""

        if not self.bridge_enabled():
            return True
        if self.current() != _TARGET_MODE:
            return False
        ledger = self._load()
        if ledger is not None and owner_chat_id is not None:
            return ledger["owner_chat_id"] == int(owner_chat_id)
        return True

    def owned_owner_chat_id(self) -> int:
        ledger = self._load()
        return int(ledger["owner_chat_id"]) if ledger is not None else 0

    @staticmethod
    def _mode(payload: dict[str, Any]) -> str:
        value = str(payload.get("TELEGRAM_MIRROR_MODE") or _DEFAULT_MODE).strip().lower()
        if value not in {_DEFAULT_MODE, _TARGET_MODE}:
            raise BridgeModeConflictError("Telegram bridge mirror mode is unsupported.")
        return value

    def _settings_payload(self) -> dict[str, Any]:
        last_error: BaseException | None = None
        for _attempt in range(3):
            try:
                path = self.settings_path
                before = path.stat()
                if before.st_size < 2 or before.st_size > _MAX_SETTINGS_BYTES:
                    raise BridgeModeConflictError("Telegram bridge settings size is invalid.")
                raw = path.read_bytes()
                after = path.stat()
                if (
                    before.st_ino != after.st_ino
                    or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                    or len(raw) != after.st_size
                ):
                    raise BridgeModeTransportError("Telegram bridge settings changed during read.")
                payload = json.loads(raw.decode("utf-8", "strict"))
                if not isinstance(payload, dict):
                    raise BridgeModeConflictError("Telegram bridge settings are invalid.")
                return payload
            except BridgeModeConflictError:
                raise
            except (BridgeModeTransportError, OSError, UnicodeError, ValueError, TypeError) as exc:
                last_error = exc
            if _attempt < 2:
                import time

                time.sleep(0.02)
        raise BridgeModeTransportError("Telegram bridge settings are not stably readable.") from last_error

    def current(self) -> str:
        """Read one stable bridge settings snapshot without ever writing it."""

        return self._mode(self._settings_payload())

    def owner_chat_id(self) -> int:
        payload = self._settings_payload()
        try:
            owner = int(str(payload.get("TELEGRAM_CHAT_ID") or "").strip())
        except (TypeError, ValueError):
            return 0
        return owner if owner > 0 else 0

    def _load(self) -> dict[str, Any] | None:
        path = self.ledger_path
        if not path.exists():
            return None
        if path_is_link_or_reparse(path):
            raise BridgeModeConflictError("Bridge mode ledger is an unsafe link.")
        try:
            if path.stat().st_size > 16_384:
                raise BridgeModeConflictError("Bridge mode ledger is oversized.")
            raw = json.loads(path.read_text(encoding="utf-8"))
        except BridgeModeConflictError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise BridgeModeConflictError("Bridge mode ledger is unreadable.") from exc
        if not isinstance(raw, dict) or raw.get("schema") != _LEDGER_SCHEMA:
            raise BridgeModeConflictError("Bridge mode ledger has an unsupported schema.")
        phase = str(raw.get("phase") or "")
        operation = str(raw.get("operation") or "")
        original = str(raw.get("original") or "")
        before = str(raw.get("from") or "")
        after = str(raw.get("to") or "")
        try:
            owner_chat_id = int(raw.get("owner_chat_id") or 0)
        except (TypeError, ValueError) as exc:
            raise BridgeModeConflictError("Bridge mode ledger has an invalid owner.") from exc
        if (
            phase not in {"mutating", "installed"}
            or operation not in {"activate", "restore"}
            or original not in {_DEFAULT_MODE, _TARGET_MODE}
            or before not in {_DEFAULT_MODE, _TARGET_MODE}
            or after not in {_DEFAULT_MODE, _TARGET_MODE}
            or owner_chat_id <= 0
        ):
            raise BridgeModeConflictError("Bridge mode ledger has an invalid transaction.")
        if phase == "installed" and (
            operation != "activate" or after != _TARGET_MODE or before != original
        ):
            raise BridgeModeConflictError("Bridge mode ledger has an invalid installed state.")
        return {
            "schema": _LEDGER_SCHEMA,
            "phase": phase,
            "operation": operation,
            "original": original,
            "from": before,
            "to": after,
            "owner_chat_id": owner_chat_id,
        }

    def _write(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        path = self.ledger_path
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}")
        try:
            with tmp.open("x", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            with contextlib.suppress(OSError):
                tmp.chmod(0o600)
            os.replace(tmp, path)
            fsync_directory(path.parent)
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise BridgeModeError("Could not persist bridge mode rollback state.") from exc

    def _delete(self) -> None:
        try:
            self.ledger_path.unlink(missing_ok=True)
            fsync_directory(self.state_dir)
        except OSError as exc:
            raise BridgeModeError("Could not remove bridge mode rollback state.") from exc

    async def _set(self, value: str) -> None:
        owned_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=5,
            follow_redirects=False,
            trust_env=False,
        )
        try:
            response = await client.post(
                self._route,
                json={"TELEGRAM_MIRROR_MODE": value},
                headers={"Accept": "application/json"},
            )
        except (httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
            raise BridgeModeTransportError("Telegram bridge settings route is unavailable.") from exc
        finally:
            if owned_client:
                await client.aclose()
        try:
            body = response.json()
        except ValueError as exc:
            raise BridgeModeTransportError("Telegram bridge settings route returned invalid JSON.") from exc
        if response.status_code != 200 or not isinstance(body, dict) or body.get("ok") is not True:
            raise BridgeModeTransportError("Telegram bridge settings route rejected the update.")

    def _installed(self, original: str, owner_chat_id: int) -> dict[str, Any]:
        return {
            "schema": _LEDGER_SCHEMA,
            "phase": "installed",
            "operation": "activate",
            "original": original,
            "from": original,
            "to": _TARGET_MODE,
            "owner_chat_id": int(owner_chat_id),
        }

    def _reconcile(
        self,
        ledger: dict[str, Any],
        observed: str,
        *,
        restoring: bool,
    ) -> dict[str, Any] | None:
        if ledger["phase"] == "installed":
            if observed == _TARGET_MODE:
                return ledger
            if restoring and observed == ledger["original"]:
                self._delete()
                return None
            raise BridgeModeConflictError("Telegram bridge mode changed outside this skill.")
        if observed == ledger["to"]:
            if ledger["operation"] == "activate":
                stable = self._installed(ledger["original"], ledger["owner_chat_id"])
                self._write(stable)
                return stable
            self._delete()
            return None
        if observed == ledger["from"]:
            if ledger["operation"] == "activate":
                self._delete()
                return None
            stable = self._installed(ledger["original"], ledger["owner_chat_id"])
            self._write(stable)
            return stable
        raise BridgeModeConflictError("Telegram bridge mode drifted during an interrupted update.")

    async def activate(self, owner_chat_id: int | None = None) -> bool:
        if not self.bridge_enabled():
            return False
        owner = int(owner_chat_id or self.owner_chat_id())
        if owner <= 0:
            raise BridgeModeConflictError("Telegram bridge owner binding is unavailable.")
        observed = self.current()
        ledger = self._load()
        ledger = self._reconcile(ledger, observed, restoring=False) if ledger else None
        if ledger is not None:
            if ledger["owner_chat_id"] != owner:
                raise BridgeModeConflictError("Bridge mode ownership belongs to a prior owner.")
            return False
        if observed == _TARGET_MODE:
            return False
        transaction = {
            "schema": _LEDGER_SCHEMA,
            "phase": "mutating",
            "operation": "activate",
            "original": observed,
            "from": observed,
            "to": _TARGET_MODE,
            "owner_chat_id": owner,
        }
        self._write(transaction)
        await self._set(_TARGET_MODE)
        if self.current() != _TARGET_MODE:
            raise BridgeModeTransportError("Telegram bridge did not confirm telegram_only mode.")
        self._write(self._installed(observed, owner))
        return True

    async def restore(self) -> bool:
        ledger = self._load()
        if ledger is None:
            return False
        observed = self.current()
        ledger = self._reconcile(ledger, observed, restoring=True)
        if ledger is None:
            return False
        original = ledger["original"]
        transaction = {
            "schema": _LEDGER_SCHEMA,
            "phase": "mutating",
            "operation": "restore",
            "original": original,
            "from": _TARGET_MODE,
            "to": original,
            "owner_chat_id": ledger["owner_chat_id"],
        }
        self._write(transaction)
        await self._set(original)
        if self.current() != original:
            raise BridgeModeTransportError("Telegram bridge did not confirm restored mirror mode.")
        self._delete()
        return True


__all__ = [
    "BridgeMirrorModeManager",
    "BridgeModeConflictError",
    "BridgeModeError",
    "BridgeModeTransportError",
]
