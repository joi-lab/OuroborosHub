"""Registration surface for the automatic Telegram Mini App gateway."""

from __future__ import annotations

import json
import os
import platform
import stat
import time
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse


_CONFIG_SCHEMA = 2
_CONFIG_NAME = "runtime_config.json"
_STATUS_NAME = "status.json"


class ConfigurationError(RuntimeError):
    pass


def _bounded_text(value: Any, maximum: int = 200) -> str:
    return str(value or "").strip().replace("\r", " ").replace("\n", " ")[:maximum]


def _unsafe_link(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(reparse and attributes & reparse)


def _bridge_chat_id(api: Any) -> int:
    info = api.get_runtime_info()
    data_dir_raw = str(info.get("data_dir") or "").strip()
    if not data_dir_raw:
        raise ConfigurationError("Ouroboros data directory is unavailable.")
    data_dir_raw_path = Path(data_dir_raw).expanduser()
    if _unsafe_link(data_dir_raw_path):
        raise ConfigurationError("Ouroboros data directory is an unsafe link.")
    data_dir = data_dir_raw_path.resolve()
    state_root = data_dir / "state"
    skills_root = state_root / "skills"
    bridge_root = skills_root / "telegram-bridge"
    unresolved = bridge_root / "settings.json"
    if any(_unsafe_link(path) for path in (state_root, skills_root, bridge_root, unresolved)):
        raise ConfigurationError("telegram-bridge owner binding crosses a symlink.")
    if not unresolved.is_file():
        return 0
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(data_dir)
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (ValueError, OSError, TypeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        chat_id = int(_bounded_text(payload.get("TELEGRAM_CHAT_ID"), 32))
    except (TypeError, ValueError):
        return 0
    # Bot API private user chats have positive IDs.  Groups/channels are negative;
    # the companion additionally verifies getChat(type=private) before exposure.
    return chat_id if chat_id > 0 else 0


def _require_supported_platform() -> None:
    system = platform.system()
    machine = platform.machine().lower()
    arch = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "amd64",
        "amd64": "amd64",
    }.get(machine)
    supported = (
        (system == "Darwin" and arch in {"arm64", "amd64"})
        or (system == "Linux" and arch in {"arm64", "amd64"})
        or (system == "Windows" and arch == "amd64")
    )
    if not supported:
        raise ConfigurationError(
            f"Telegram Mini App does not have a pinned cloudflared asset for "
            f"{system or 'unknown'}/{machine or 'unknown'}."
        )


def _state_path(api: Any, name: str) -> Path:
    raw_root = Path(api.get_state_dir()).expanduser()
    raw_root.mkdir(parents=True, exist_ok=True)
    if _unsafe_link(raw_root):
        raise ConfigurationError("Skill state directory is an unsafe link.")
    root = raw_root.resolve(strict=True)
    path = root / name
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ConfigurationError("Skill state path escaped its private directory.") from exc
    if _unsafe_link(path):
        raise ConfigurationError("Skill state file is an unsafe link.")
    return path


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if len(encoded.encode("utf-8")) > 16_384:
        raise ConfigurationError("Runtime configuration is unexpectedly large.")
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(encoded, encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ConfigurationError("Could not persist Mini App runtime configuration.") from exc


def _process_alive_for_status(pid: int) -> bool:
    if pid <= 1:
        return False
    if platform.system() == "Windows":
        # Fresh heartbeat is the safe authority. os.kill(pid, 0) terminates a
        # process on Windows CPython instead of probing it.
        return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False


def _seed_starting_status(api: Any) -> None:
    """Give registration-without-spawn a heartbeat-bounded visible state."""

    path = _state_path(api, _STATUS_NAME)
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError, TypeError):
        current = None
    if isinstance(current, dict):
        try:
            age = int(time.time()) - int(current.get("updated_at_epoch") or 0)
            pid = int(current.get("pid") or 0)
        except (TypeError, ValueError):
            age = 999
            pid = 0
        if -60 <= age <= 45 and _process_alive_for_status(pid):
            return
    _atomic_json(
        path,
        {
            "schema": 2,
            "state": "starting",
            "reason_code": "registered",
            "message": "Waiting for the Mini App companion to publish its first heartbeat.",
            "cloudflared_version": "2026.7.2",
            "pid": 0,
            "updated_at_epoch": int(time.time()),
        },
    )


def _write_runtime_config(api: Any) -> dict[str, Any]:
    info = api.get_runtime_info()
    try:
        core_port = int(info.get("server_port") or 0)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("Ouroboros server port is invalid.") from exc
    if not 1 <= core_port <= 65_535:
        raise ConfigurationError("Ouroboros server port is unavailable.")
    payload = {
        "schema": _CONFIG_SCHEMA,
        "core_port": core_port,
        "owner_chat_id": _bridge_chat_id(api),
        "button_text": "Ouroboros",
        "tunnel": "cloudflare_quick",
    }
    _atomic_json(_state_path(api, _CONFIG_NAME), payload)
    return payload


def _read_status(api: Any) -> dict[str, Any]:
    try:
        path = _state_path(api, _STATUS_NAME)
    except ConfigurationError:
        return {"state": "error", "message": "Companion status path is unsafe."}
    if not path.is_file() or path.is_symlink():
        return {
            "state": "starting",
            "message": "The companion has not published status yet.",
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"state": "error", "message": "Companion status is unreadable."}
    if not isinstance(value, dict):
        return {"state": "error", "message": "Companion status is invalid."}
    # Return only the documented, bounded public diagnostics.  In particular,
    # never echo environment, headers, initData, cookies, or Telegram API text.
    result = {
        "state": _bounded_text(value.get("state"), 32) or "unknown",
        "message": _bounded_text(value.get("message"), 300),
    }
    try:
        pid = int(value.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    stale_status = False
    terminal = {"stopped", "rollback_pending", "error"}
    if result["state"] not in terminal:
        registration_pending = (
            pid == 0
            and result["state"] == "starting"
            and _bounded_text(value.get("reason_code"), 64) == "registered"
        )
        alive = registration_pending or _process_alive_for_status(pid)
        try:
            updated_at = int(value.get("updated_at_epoch") or 0)
        except (TypeError, ValueError):
            updated_at = 0
        age = int(time.time()) - updated_at
        heartbeat_fresh = updated_at > 0 and -60 <= age <= 45
        if not alive or not heartbeat_fresh:
            stale_status = True
            result = {
                "state": "stale",
                "message": (
                    "The companion heartbeat is stale or its process is no longer running."
                ),
                "reason_code": "heartbeat_stale",
            }
    public_url = _bounded_text(value.get("public_url"), 2048)
    if (
        result["state"] == "ready"
        and public_url.startswith("https://")
        and public_url.endswith(".trycloudflare.com/")
    ):
        result["public_url"] = public_url
    version = _bounded_text(value.get("cloudflared_version"), 64)
    if version:
        result["cloudflared_version"] = version
    for key, maximum in (
        ("instance_id", 64),
        ("platform", 96),
    ):
        bounded = _bounded_text(value.get(key), maximum)
        if bounded:
            result[key] = bounded
    if not stale_status:
        reason_code = _bounded_text(value.get("reason_code"), 64)
        if reason_code:
            result["reason_code"] = reason_code
    for key in ("updated_at_epoch", "last_ready_at_epoch", "attempt", "next_retry_at_epoch"):
        try:
            result[key] = max(0, int(value.get(key) or 0))
        except (TypeError, ValueError):
            pass
    security = value.get("security")
    if isinstance(security, dict):
        allowed = {
            "auth_success",
            "auth_rejected",
            "auth_rate_limited",
            "auth_busy",
            "active_sessions",
            "active_websockets",
        }
        safe_security: dict[str, int] = {}
        for key in sorted(allowed):
            try:
                safe_security[key] = max(0, min(int(security.get(key) or 0), 2_147_483_647))
            except (TypeError, ValueError):
                continue
        if safe_security:
            result["security"] = safe_security
    return result


def _make_status(api: Any):
    async def status(_request: Any) -> JSONResponse:
        return JSONResponse({"ok": True, **_read_status(api)})

    return status


_SETTINGS_SCHEMA = {
    "components": [
        {
            "type": "markdown",
            "text": (
                "Automatic owner-only Telegram Mini App PoC. Enabling this skill starts a "
                "temporary Cloudflare Quick Tunnel to a skill-owned authentication sidecar, "
                "then points the existing private bot menu button at the unchanged Ouroboros "
                "SPA. No Cloudflare account, domain, app-store login, or manual URL is needed.\n\n"
                "The authenticated Mini App has the same owner authority as the local UI, "
                "including Settings, Skills, and Files. Quick Tunnels are public dev/test "
                "transport with no SLA; the sidecar, not URL secrecy, is the security boundary. "
                "Disabling the skill stops the tunnel and best-effort restores the prior button."
            ),
        },
        {
            "type": "action",
            "id": "status",
            "route": "status",
            "method": "POST",
            "submit_label": "Refresh Mini App status",
            "busy_label": "Checking...",
            "fields": [],
        },
    ]
}


def register(api: Any) -> None:
    _require_supported_platform()
    _write_runtime_config(api)
    _seed_starting_status(api)
    api.register_route("status", handler=_make_status(api), methods=("POST",))
    api.register_settings_section(
        "telegram_miniapp_poc",
        title="Telegram Mini App PoC",
        schema=_SETTINGS_SCHEMA,
    )
    api.register_companion_process("miniapp_gateway")
    api.log(
        "info",
        "telegram-miniapp-poc registered automatic owner-only gateway",
    )


__all__ = ["ConfigurationError", "register"]
