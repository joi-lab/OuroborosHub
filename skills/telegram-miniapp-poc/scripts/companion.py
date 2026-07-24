"""One supervised lifecycle for sidecar, Quick Tunnel, and Telegram button."""

from __future__ import annotations

import asyncio
import contextlib
import enum
import ipaddress
import json
import os
import random
import signal
import socket
import threading
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
import uvicorn

from cloudflare_tunnel import (
    CLOUDFLARED_VERSION,
    CloudflaredError,
    QuickTunnel,
    ensure_cloudflared,
)
from sidecar import GATEWAY_MARKER_HEADER, TelegramProxySidecar
from platform_support import (
    PlatformSupportError,
    acquire_file_lock,
    path_is_link_or_reparse,
    process_alive,
    release_file_lock,
    require_isolated_process_group as _require_isolated_process_group,
    start_parent_lifeline,
    write_lock_owner,
)
from runtime_status import RuntimeStatus, RuntimeStatusError
from telegram_bridge_mode import (
    BridgeMirrorModeManager,
    BridgeModeConflictError,
    BridgeModeError,
    BridgeModeTransportError,
)
from telegram_menu import (
    TelegramMenuConflictError,
    TelegramMenuError,
    TelegramMenuManager,
    TelegramMenuRejectedError,
    TelegramMenuTransportError,
    snapshot_owner_chat_id,
)


_CONFIG_NAME = "runtime_config.json"
_STATUS_NAME = "status.json"
_CONFIG_SCHEMA = 2
_INITIAL_PARENT_PID = os.getppid()
_TRYCLOUDFLARE_SUFFIX = ".trycloudflare.com"
_DOH_ENDPOINT = "https://1.1.1.1/dns-query"
_DOH_HOST = "cloudflare-dns.com"
_MAX_DOH_RESPONSE_BYTES = 32 * 1024
_MAX_DOH_ANSWERS = 64
_MAX_DOH_ADDRESSES = 4
_MAX_GATEWAY_RESPONSE_BYTES = 64 * 1024
_PUBLIC_OPERATION_TIMEOUT_SEC = 4.0
_DOH_RETRY_INTERVAL_SEC = 2.0
_MAX_DOH_ROUNDS = 8
_RETRY_BASE_SEC = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)
_STABLE_READY_RESET_SEC = 300.0
_CORE_HEALTH_INTERVAL_SEC = 30.0
_PUBLIC_HEALTH_INTERVAL_SEC = 45.0
_MENU_HEALTH_INTERVAL_SEC = 300.0
_OWNER_HEALTH_INTERVAL_SEC = 30.0
_MONITOR_TICK_SEC = 2.0
_SIDECAR_STOP_GRACE_SEC = 0.75
_SHUTDOWN_TELEGRAM_REQUEST_TIMEOUT_SEC = 0.7
_STUCK_CLEANUP_EXIT_CODE = 78
_T = TypeVar("_T")


class CompanionError(RuntimeError):
    pass


class ShutdownRequested(CompanionError):
    pass


class SidecarStoppedError(CompanionError):
    pass


class _PublicProbeError(RuntimeError):
    pass


class PublicProbeOutcome(enum.Enum):
    READY = "ready"
    UNHEALTHY_RESPONSE = "unhealthy_response"
    OBSERVER_UNAVAILABLE = "observer_unavailable"
    TUNNEL_EXITED = "tunnel_exited"


def acquire_singleton(state_dir: Path, *, timeout_sec: float = 8.0) -> int:
    """Hold one non-blocking process lease for the complete companion lifetime."""
    path = state_dir / ".companion.lock"
    try:
        fd = acquire_file_lock(path, timeout_sec=timeout_sec)
        write_lock_owner(fd, os.getpid())
        return fd
    except PlatformSupportError as exc:
        raise CompanionError("Another Mini App companion generation is still shutting down.") from exc


def release_singleton(fd: int | None) -> None:
    release_file_lock(fd)


def _pid_alive(pid: int) -> bool:
    return process_alive(pid)


def require_running(stop_event: asyncio.Event) -> None:
    if stop_event.is_set():
        raise ShutdownRequested("Companion shutdown was requested.")


def require_isolated_process_group() -> None:
    try:
        _require_isolated_process_group()
    except PlatformSupportError as exc:
        raise CompanionError(str(exc)) from exc


def _safe_state_dir(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    try:
        raw.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise CompanionError("Could not create private skill state.") from exc
    if path_is_link_or_reparse(raw) or not raw.is_dir():
        raise CompanionError("Skill state must be a real directory.")
    try:
        return raw.resolve(strict=True)
    except OSError as exc:
        raise CompanionError("Could not resolve private skill state.") from exc


def _state_file(state_dir: Path, name: str) -> Path:
    path = state_dir / name
    try:
        path.parent.resolve(strict=True).relative_to(state_dir)
    except (OSError, ValueError) as exc:
        raise CompanionError("Companion state path escaped private skill state.") from exc
    if path_is_link_or_reparse(path):
        raise CompanionError("Companion state path is an unsafe link.")
    return path


def load_runtime_config(state_dir: Path) -> dict[str, Any]:
    path = _state_file(state_dir, _CONFIG_NAME)
    if path.is_symlink() or not path.is_file():
        raise CompanionError("Runtime configuration is missing or unsafe.")
    try:
        if path.stat().st_size > 16_384:
            raise CompanionError("Runtime configuration is oversized.")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except CompanionError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise CompanionError("Runtime configuration is unreadable.") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "core_port",
        "owner_chat_id",
        "button_text",
        "tunnel",
    }:
        raise CompanionError("Runtime configuration has an unexpected shape.")
    if raw.get("schema") != _CONFIG_SCHEMA or raw.get("tunnel") != "cloudflare_quick":
        raise CompanionError("Runtime configuration requests an unsupported transport.")
    try:
        core_port = int(raw["core_port"])
        owner_chat_id = int(raw["owner_chat_id"])
    except (TypeError, ValueError) as exc:
        raise CompanionError("Runtime configuration contains invalid IDs.") from exc
    button_text = str(raw.get("button_text") or "").strip()
    if not 1 <= core_port <= 65_535 or owner_chat_id < 0:
        raise CompanionError("Runtime configuration contains out-of-range IDs.")
    if not button_text or len(button_text) > 64 or any(c in button_text for c in "\r\n"):
        raise CompanionError("Runtime configuration contains invalid button text.")
    return {
        "core_port": core_port,
        "owner_chat_id": owner_chat_id,
        "button_text": button_text,
    }


async def probe_core(core_port: int, *, timeout_sec: float = 2.0) -> bool:
    async with httpx.AsyncClient(
        timeout=timeout_sec,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        try:
            response = await client.get(
                f"http://127.0.0.1:{core_port}/api/health",
                headers={"Accept": "application/json"},
            )
        except (httpx.HTTPError, OSError):
            return False
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"


async def start_sidecar(sidecar: TelegramProxySidecar) -> tuple[uvicorn.Server, asyncio.Task[None], int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server: uvicorn.Server | None = None
    task: asyncio.Task[None] | None = None
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        port = int(listener.getsockname()[1])
        config = uvicorn.Config(
            sidecar.app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            proxy_headers=False,
            server_header=False,
            date_header=False,
            lifespan="off",
            ws_max_size=8 * 1024 * 1024,
            ws_max_queue=16,
            ws_per_message_deflate=False,
            limit_concurrency=128,
            backlog=128,
            timeout_keep_alive=10,
            timeout_graceful_shutdown=3,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        task = asyncio.create_task(server.serve(sockets=[listener]))
        for _ in range(100):
            if server.started:
                return server, task, port
            if task.done():
                try:
                    await task
                except Exception as exc:
                    raise CompanionError("Authentication sidecar failed during startup.") from exc
                raise CompanionError("Authentication sidecar stopped during startup.")
            await asyncio.sleep(0.05)
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=3)
        raise CompanionError("Authentication sidecar did not start in time.")
    except BaseException:
        if server is not None:
            server.should_exit = True
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        with contextlib.suppress(OSError):
            listener.close()
        raise


def _quick_tunnel_host(public_url: str) -> str:
    try:
        parsed = urlsplit(str(public_url or "").strip())
        port = parsed.port
    except ValueError as exc:
        raise CompanionError("Public tunnel URL is invalid.") from exc
    host = str(parsed.hostname or "").lower()
    label = host[: -len(_TRYCLOUDFLARE_SUFFIX)] if host.endswith(_TRYCLOUDFLARE_SUFFIX) else ""
    if (
        parsed.scheme.lower() != "https"
        or parsed.netloc != host
        or port is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not label
        or "." in label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in label)
    ):
        raise CompanionError("Public tunnel URL is not an exact Cloudflare Quick Tunnel origin.")
    return host


def _is_dns_resolution_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return True
        for value in current.args:
            if isinstance(value, socket.gaierror):
                return True
        current = current.__cause__ or current.__context__
    return False


async def _run_startup_operation(
    operation: Awaitable[_T],
    *,
    stop_event: asyncio.Event | None,
    timeout_sec: float,
) -> _T:
    """Bound one startup operation and make graceful shutdown win every race."""
    operation_task = asyncio.create_task(operation)
    stop_task = asyncio.create_task(stop_event.wait()) if stop_event is not None else None
    waiters: set[asyncio.Task[Any]] = {operation_task}
    if stop_task is not None:
        waiters.add(stop_task)
    try:
        done, _pending = await asyncio.wait(
            waiters,
            timeout=max(0.01, float(timeout_sec)),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task is not None and stop_task in done:
            operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)
            require_running(stop_event)
        if operation_task in done:
            result = await operation_task
            if stop_event is not None:
                require_running(stop_event)
            return result
        operation_task.cancel()
        await asyncio.gather(operation_task, return_exceptions=True)
        raise asyncio.TimeoutError
    finally:
        if stop_task is not None:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)


async def _gateway_response_ready(response: httpx.Response) -> bool:
    if response.status_code != 200 or response.headers.get(GATEWAY_MARKER_HEADER) != "1":
        return False
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > _MAX_GATEWAY_RESPONSE_BYTES:
            return False
    return True


async def _probe_gateway_normally(
    client: httpx.AsyncClient,
    public_url: str,
    *,
    timeout_sec: float,
) -> bool:
    async with client.stream(
        "GET",
        public_url,
        headers={
            "Accept": "text/html",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        },
        timeout=timeout_sec,
    ) as response:
        return await _gateway_response_ready(response)


def _normalise_dns_name(value: Any) -> str:
    return str(value or "").strip().lower().rstrip(".")


def _is_public_unicast_ipv4(address: ipaddress.IPv4Address) -> bool:
    return address.is_global and not (
        address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_private
    )


def _parse_doh_ipv4_response(raw: bytes, host: str) -> tuple[str, ...]:
    if len(raw) > _MAX_DOH_RESPONSE_BYTES:
        raise _PublicProbeError("Cloudflare DNS response was oversized.")
    try:
        payload = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise _PublicProbeError("Cloudflare DNS response was invalid.") from exc
    if not isinstance(payload, dict):
        raise _PublicProbeError("Cloudflare DNS response had an unexpected shape.")
    status = payload.get("Status")
    if type(status) is not int or status != 0 or payload.get("TC") is not False:
        raise _PublicProbeError("Cloudflare DNS did not return a complete successful answer.")
    questions = payload.get("Question")
    if not isinstance(questions, list) or len(questions) != 1:
        raise _PublicProbeError("Cloudflare DNS response did not bind one question.")
    question = questions[0]
    if (
        not isinstance(question, dict)
        or type(question.get("type")) is not int
        or question.get("type") != 1
        or _normalise_dns_name(question.get("name")) != host
    ):
        raise _PublicProbeError("Cloudflare DNS response did not match the tunnel hostname.")
    answers = payload.get("Answer")
    if not isinstance(answers, list) or len(answers) > _MAX_DOH_ANSWERS:
        raise _PublicProbeError("Cloudflare DNS response had an invalid answer set.")
    addresses: list[str] = []
    for answer in answers:
        if (
            not isinstance(answer, dict)
            or type(answer.get("type")) is not int
            or answer.get("type") != 1
        ):
            continue
        try:
            address = ipaddress.IPv4Address(str(answer.get("data") or ""))
        except ipaddress.AddressValueError:
            continue
        if not _is_public_unicast_ipv4(address):
            continue
        value = str(address)
        if value not in addresses:
            addresses.append(value)
            if len(addresses) >= _MAX_DOH_ADDRESSES:
                break
    if not addresses:
        raise _PublicProbeError("Cloudflare DNS returned no usable public IPv4 address.")
    return tuple(addresses)


async def _resolve_public_ipv4_via_doh(host: str, *, timeout_sec: float) -> tuple[str, ...]:
    async with httpx.AsyncClient(
        timeout=timeout_sec,
        follow_redirects=False,
        trust_env=False,
        http2=False,
    ) as client:
        async with client.stream(
            "GET",
            _DOH_ENDPOINT,
            params={"name": host, "type": "A"},
            headers={
                "Accept": "application/dns-json",
                "Accept-Encoding": "identity",
                "Host": _DOH_HOST,
            },
            extensions={"sni_hostname": _DOH_HOST},
            timeout=timeout_sec,
        ) as response:
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if response.status_code != 200 or content_type != "application/dns-json":
                raise _PublicProbeError("Cloudflare DNS-over-HTTPS request failed.")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_DOH_RESPONSE_BYTES:
                    raise _PublicProbeError("Cloudflare DNS response was oversized.")
    return _parse_doh_ipv4_response(bytes(body), host)


async def _probe_gateway_via_ipv4(
    host: str,
    address: str,
    *,
    timeout_sec: float,
) -> bool:
    try:
        parsed_address = ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError as exc:
        raise _PublicProbeError("Cloudflare DNS returned an invalid IPv4 address.") from exc
    if not _is_public_unicast_ipv4(parsed_address):
        raise _PublicProbeError("Cloudflare DNS returned a non-public IPv4 address.")
    async with httpx.AsyncClient(
        timeout=timeout_sec,
        follow_redirects=False,
        trust_env=False,
        http2=False,
    ) as client:
        async with client.stream(
            "GET",
            f"https://{parsed_address}/",
            headers={
                "Accept": "text/html",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
                "Host": host,
            },
            extensions={"sni_hostname": host},
            timeout=timeout_sec,
        ) as response:
            return await _gateway_response_ready(response)


async def probe_public_gateway_once(
    public_url: str,
    tunnel: QuickTunnel,
    *,
    timeout_sec: float = _PUBLIC_OPERATION_TIMEOUT_SEC,
    stop_event: asyncio.Event | None = None,
) -> PublicProbeOutcome:
    if stop_event is not None:
        require_running(stop_event)
    if tunnel.returncode is not None:
        return PublicProbeOutcome.TUNNEL_EXITED
    host = _quick_tunnel_host(public_url)
    async with httpx.AsyncClient(
        timeout=timeout_sec,
        follow_redirects=False,
        trust_env=False,
        http2=False,
    ) as client:
        try:
            ready = await _run_startup_operation(
                _probe_gateway_normally(client, public_url, timeout_sec=timeout_sec),
                stop_event=stop_event,
                timeout_sec=timeout_sec,
            )
        except httpx.HTTPError as exc:
            if not _is_dns_resolution_error(exc):
                return PublicProbeOutcome.OBSERVER_UNAVAILABLE
        except (asyncio.TimeoutError, OSError):
            return PublicProbeOutcome.OBSERVER_UNAVAILABLE
        else:
            return (
                PublicProbeOutcome.READY
                if ready and tunnel.returncode is None
                else PublicProbeOutcome.UNHEALTHY_RESPONSE
            )

    try:
        addresses = await _run_startup_operation(
            _resolve_public_ipv4_via_doh(host, timeout_sec=timeout_sec),
            stop_event=stop_event,
            timeout_sec=timeout_sec,
        )
    except (httpx.HTTPError, asyncio.TimeoutError, OSError, _PublicProbeError):
        return PublicProbeOutcome.OBSERVER_UNAVAILABLE
    saw_response = False
    for address in addresses:
        if tunnel.returncode is not None:
            return PublicProbeOutcome.TUNNEL_EXITED
        try:
            ready = await _run_startup_operation(
                _probe_gateway_via_ipv4(host, address, timeout_sec=timeout_sec),
                stop_event=stop_event,
                timeout_sec=timeout_sec,
            )
        except (httpx.HTTPError, asyncio.TimeoutError, OSError, _PublicProbeError):
            continue
        saw_response = True
        if ready and tunnel.returncode is None:
            return PublicProbeOutcome.READY
    return (
        PublicProbeOutcome.UNHEALTHY_RESPONSE
        if saw_response
        else PublicProbeOutcome.OBSERVER_UNAVAILABLE
    )


async def verify_public_gateway(
    public_url: str,
    tunnel: QuickTunnel,
    *,
    timeout_sec: float = 35.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.01, float(timeout_sec))
    doh_rounds = 0
    while True:
        if stop_event is not None:
            require_running(stop_event)
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise CompanionError("Public tunnel did not reach the authentication sidecar.")
        outcome = await probe_public_gateway_once(
            public_url,
            tunnel,
            timeout_sec=min(_PUBLIC_OPERATION_TIMEOUT_SEC, remaining),
            stop_event=stop_event,
        )
        if outcome is PublicProbeOutcome.READY:
            return
        if outcome is PublicProbeOutcome.TUNNEL_EXITED:
            raise CompanionError(
                f"Cloudflared exited before public verification (exit {tunnel.returncode})."
            )
        if outcome is PublicProbeOutcome.OBSERVER_UNAVAILABLE:
            doh_rounds += 1
            if doh_rounds >= _MAX_DOH_ROUNDS and loop.time() >= deadline:
                raise CompanionError("Public tunnel could not be observed safely.")
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise CompanionError("Public tunnel did not reach the authentication sidecar.")
        if stop_event is None:
            await asyncio.sleep(min(_DOH_RETRY_INTERVAL_SEC, remaining))
        else:
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=min(_DOH_RETRY_INTERVAL_SEC, remaining),
                )
            except asyncio.TimeoutError:
                pass


async def stop_sidecar(
    server: uvicorn.Server | None,
    task: asyncio.Task[None] | None,
    sidecar: TelegramProxySidecar | None,
) -> None:
    if sidecar is not None:
        sidecar.clear_public_url()
    if server is not None:
        server.should_exit = True

    def consume_terminal_result(done_task: asyncio.Task[None]) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            done_task.exception()

    async def finish_cleanup() -> None:
        cleanup_stuck = False
        if task is not None:
            done, _pending = await asyncio.wait(
                {task},
                timeout=_SIDECAR_STOP_GRACE_SEC,
            )
            if task not in done:
                if server is not None:
                    with contextlib.suppress(Exception):
                        server.force_exit = True
                task.cancel()
                done, _pending = await asyncio.wait(
                    {task},
                    timeout=_SIDECAR_STOP_GRACE_SEC,
                )
            if task in done:
                consume_terminal_result(task)
            else:
                task.add_done_callback(consume_terminal_result)
                cleanup_stuck = True
        if sidecar is not None:
            close_task = asyncio.create_task(sidecar.aclose())
            done, _pending = await asyncio.wait(
                {close_task},
                timeout=_SIDECAR_STOP_GRACE_SEC,
            )
            if close_task not in done:
                close_task.cancel()
                done, _pending = await asyncio.wait(
                    {close_task},
                    timeout=_SIDECAR_STOP_GRACE_SEC,
                )
            if close_task in done:
                consume_terminal_result(close_task)
            else:
                close_task.add_done_callback(consume_terminal_result)
                cleanup_stuck = True
        if cleanup_stuck:
            _hard_exit_stuck_cleanup()

    # Keep the bounded cleanup transaction alive across cancellation at any
    # await point.  The caller receives CancelledError only after aclose.
    caller_cancelled = False
    cleanup_task = asyncio.create_task(finish_cleanup())
    cleanup_error: BaseException | None = None
    while not cleanup_task.done():
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as exc:
            if cleanup_task.done():
                cleanup_error = exc
                break
            caller_cancelled = True
        except Exception as exc:
            cleanup_error = exc
            break
    if cleanup_error is None and cleanup_task.done():
        try:
            cleanup_error = cleanup_task.exception()
        except asyncio.CancelledError as exc:
            cleanup_error = exc
    if caller_cancelled:
        raise asyncio.CancelledError
    if cleanup_error is not None:
        raise cleanup_error


def _hard_exit_stuck_cleanup() -> None:
    """End the companion if an in-process cleanup coroutine cannot be reaped."""

    os._exit(_STUCK_CLEANUP_EXIT_CODE)


def _safe_error(exc: BaseException) -> str:
    if isinstance(
        exc,
        (
            CompanionError,
            CloudflaredError,
            TelegramMenuError,
            BridgeModeError,
            RuntimeStatusError,
            PlatformSupportError,
        ),
    ):
        return str(exc)[:300]
    return f"Unexpected companion failure ({type(exc).__name__})."


class _GenerationResult(enum.Enum):
    RECONNECT = "reconnect"
    OWNER_CHANGED = "owner_changed"
    MENU_BLOCKED = "menu_blocked"
    TELEGRAM_BLOCKED = "telegram_blocked"
    MIRROR_BLOCKED = "mirror_blocked"


def _retry_delay(failure_count: int) -> float:
    index = max(0, min(int(failure_count) - 1, len(_RETRY_BASE_SEC) - 1))
    base = _RETRY_BASE_SEC[index]
    return min(60.0, max(0.1, base * random.uniform(0.8, 1.2)))


async def _wait_or_stop(
    delay: float,
    stop_event: asyncio.Event,
    *,
    server_task: asyncio.Task[None] | None = None,
) -> None:
    stop_task = asyncio.create_task(stop_event.wait())
    waiters: set[asyncio.Task[Any]] = {stop_task}
    if server_task is not None:
        waiters.add(server_task)
    try:
        done, _pending = await asyncio.wait(
            waiters,
            timeout=max(0.0, float(delay)),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if server_task is not None and server_task in done:
            try:
                await server_task
            except asyncio.CancelledError as exc:
                raise SidecarStoppedError("Authentication sidecar was cancelled unexpectedly.") from exc
            except Exception as exc:
                raise SidecarStoppedError("Authentication sidecar failed unexpectedly.") from exc
            raise SidecarStoppedError("Authentication sidecar stopped unexpectedly.")
        if stop_task in done and stop_event.is_set():
            raise ShutdownRequested("Companion shutdown was requested.")
    finally:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)


async def _wait_for_owner(
    bridge: BridgeMirrorModeManager,
    token: str,
    state_dir: Path,
    telegram_client: httpx.AsyncClient,
    status: RuntimeStatus,
    stop_event: asyncio.Event,
) -> tuple[int, TelegramMenuManager]:
    failures = 0
    while True:
        require_running(stop_event)
        try:
            owner = bridge.owner_chat_id()
        except BridgeModeConflictError as exc:
            status.transition(
                "blocked",
                _safe_error(exc),
                reason_code="owner_binding_conflict",
            )
            await _wait_or_stop(30.0, stop_event)
            continue
        except BridgeModeTransportError:
            owner = 0
        if owner <= 0:
            status.transition(
                "waiting_owner",
                "Open the private telegram-bridge bot chat once to pin its owner.",
                reason_code="waiting_private_owner",
            )
            await _wait_or_stop(5.0, stop_event)
            continue
        menu = TelegramMenuManager(token, owner, state_dir, client=telegram_client)
        try:
            await _run_startup_operation(
                menu.verify_private_owner(),
                stop_event=stop_event,
                timeout_sec=15.0,
            )
            return owner, menu
        except (TelegramMenuTransportError, asyncio.TimeoutError) as exc:
            failures += 1
            delay = _retry_delay(failures)
            status.transition(
                "degraded",
                _safe_error(exc),
                reason_code="telegram_owner_unobservable",
                attempt=failures,
                next_retry_at_epoch=int(time.time() + delay),
            )
            await _wait_or_stop(delay, stop_event)
        except TelegramMenuRejectedError as exc:
            status.transition(
                "blocked",
                _safe_error(exc),
                reason_code="telegram_owner_rejected",
            )
            await _wait_or_stop(30.0, stop_event)
        except TelegramMenuError as exc:
            status.transition(
                "blocked",
                _safe_error(exc),
                reason_code="telegram_owner_invalid",
            )
            await _wait_or_stop(30.0, stop_event)


async def _wait_for_core_and_mirror(
    bridge: BridgeMirrorModeManager,
    owner: int,
    core_port: int,
    status: RuntimeStatus,
    stop_event: asyncio.Event,
) -> bool:
    failures = 0
    while True:
        require_running(stop_event)
        try:
            if bridge.owner_chat_id() != owner:
                return False
        except BridgeModeConflictError as exc:
            status.transition(
                "blocked",
                _safe_error(exc),
                reason_code="owner_binding_conflict",
            )
            await _wait_or_stop(30.0, stop_event)
            continue
        except BridgeModeTransportError as exc:
            failures += 1
            delay = _retry_delay(failures)
            status.transition(
                "degraded",
                _safe_error(exc),
                reason_code="owner_binding_unobservable",
                attempt=failures,
                next_retry_at_epoch=int(time.time() + delay),
            )
            await _wait_or_stop(delay, stop_event)
            continue
        if not await probe_core(core_port):
            failures += 1
            delay = _retry_delay(failures)
            status.transition(
                "degraded",
                "Local Ouroboros is not ready; the Mini App remains private and will retry.",
                reason_code="core_unavailable",
                attempt=failures,
                next_retry_at_epoch=int(time.time() + delay),
            )
            await _wait_or_stop(delay, stop_event)
            continue
        try:
            await _run_startup_operation(
                bridge.activate(owner),
                stop_event=stop_event,
                timeout_sec=6.0,
            )
            return True
        except BridgeModeConflictError as exc:
            status.transition(
                "blocked",
                _safe_error(exc),
                reason_code="mirror_mode_conflict",
            )
            await _wait_or_stop(30.0, stop_event)
        except (BridgeModeTransportError, asyncio.TimeoutError) as exc:
            failures += 1
            delay = _retry_delay(failures)
            status.transition(
                "degraded",
                _safe_error(exc),
                reason_code="mirror_route_unavailable",
                attempt=failures,
                next_retry_at_epoch=int(time.time() + delay),
            )
            await _wait_or_stop(delay, stop_event)
        except BridgeModeError as exc:
            status.transition(
                "blocked",
                _safe_error(exc),
                reason_code="mirror_mode_state_error",
            )
            await _wait_or_stop(30.0, stop_event)


async def _wait_for_cloudflared(
    state_dir: Path,
    status: RuntimeStatus,
    stop_event: asyncio.Event,
    *,
    server_task: asyncio.Task[None] | None = None,
) -> Path:
    failures = 0
    while True:
        require_running(stop_event)
        try:
            return await _run_startup_operation(
                ensure_cloudflared(state_dir),
                stop_event=stop_event,
                timeout_sec=45.0,
            )
        except (CloudflaredError, asyncio.TimeoutError) as exc:
            failures += 1
            delay = _retry_delay(failures)
            status.transition(
                "reconnecting",
                _safe_error(exc),
                reason_code="cloudflared_unavailable",
                attempt=failures,
                next_retry_at_epoch=int(time.time() + delay),
            )
            await _wait_or_stop(delay, stop_event, server_task=server_task)


async def _wait_for_public_verification(
    public_url: str,
    tunnel: QuickTunnel,
    bridge: BridgeMirrorModeManager,
    owner: int,
    status: RuntimeStatus,
    stop_event: asyncio.Event,
    server_task: asyncio.Task[None],
) -> _GenerationResult | None:
    confirmed_bad = 0
    while True:
        try:
            if bridge.owner_chat_id() != owner:
                return _GenerationResult.OWNER_CHANGED
            if not bridge.safe_for_exposure(owner):
                return _GenerationResult.MIRROR_BLOCKED
        except (BridgeModeConflictError, BridgeModeTransportError, BridgeModeError):
            return _GenerationResult.MIRROR_BLOCKED
        outcome = await probe_public_gateway_once(
            public_url,
            tunnel,
            stop_event=stop_event,
        )
        if outcome is PublicProbeOutcome.READY:
            return None
        if outcome is PublicProbeOutcome.TUNNEL_EXITED:
            return _GenerationResult.RECONNECT
        if outcome is PublicProbeOutcome.UNHEALTHY_RESPONSE:
            confirmed_bad += 1
            status.transition(
                "verifying",
                "The public endpoint responded without the exact Mini App marker.",
                reason_code="public_marker_unhealthy",
                attempt=confirmed_bad,
            )
            if confirmed_bad >= 3:
                return _GenerationResult.RECONNECT
        else:
            status.transition(
                "degraded",
                "This machine cannot currently observe the public tunnel; it will not rotate blindly.",
                reason_code="public_observer_unavailable",
            )
        await _wait_or_stop(2.0, stop_event, server_task=server_task)


async def _sync_menu_until_confirmed(
    public_url: str,
    tunnel: QuickTunnel,
    menu: TelegramMenuManager,
    bridge: BridgeMirrorModeManager,
    owner: int,
    button_text: str,
    status: RuntimeStatus,
    stop_event: asyncio.Event,
    server_task: asyncio.Task[None],
) -> _GenerationResult | None:
    failures = 0
    while True:
        require_running(stop_event)
        if tunnel.returncode is not None:
            return _GenerationResult.RECONNECT
        try:
            if bridge.owner_chat_id() != owner:
                return _GenerationResult.OWNER_CHANGED
            if not bridge.safe_for_exposure(owner):
                await _run_startup_operation(
                    bridge.activate(owner),
                    stop_event=stop_event,
                    timeout_sec=6.0,
                )
            if not bridge.safe_for_exposure(owner):
                return _GenerationResult.MIRROR_BLOCKED
        except BridgeModeConflictError:
            return _GenerationResult.MIRROR_BLOCKED
        except (BridgeModeTransportError, asyncio.TimeoutError) as exc:
            failures += 1
            if failures >= 3:
                return _GenerationResult.MIRROR_BLOCKED
            delay = _retry_delay(failures)
            status.transition(
                "degraded",
                _safe_error(exc),
                reason_code="owner_or_mirror_unobservable",
                attempt=failures,
                next_retry_at_epoch=int(time.time() + delay),
            )
            await _wait_or_stop(delay, stop_event, server_task=server_task)
            continue
        try:
            await _run_startup_operation(
                menu.install(public_url, button_text),
                stop_event=stop_event,
                timeout_sec=15.0,
            )
            if tunnel.returncode is not None:
                return _GenerationResult.RECONNECT
            if bridge.owner_chat_id() != owner:
                return _GenerationResult.OWNER_CHANGED
            if not bridge.safe_for_exposure(owner):
                return _GenerationResult.MIRROR_BLOCKED
            return None
        except BridgeModeConflictError:
            return _GenerationResult.MIRROR_BLOCKED
        except BridgeModeTransportError:
            return _GenerationResult.MIRROR_BLOCKED
        except TelegramMenuConflictError:
            return _GenerationResult.MENU_BLOCKED
        except TelegramMenuRejectedError:
            return _GenerationResult.TELEGRAM_BLOCKED
        except TelegramMenuTransportError as exc:
            failures += 1
            delay = _retry_delay(failures)
            status.transition(
                "syncing_menu",
                _safe_error(exc),
                reason_code="menu_transport_unavailable",
                attempt=failures,
                next_retry_at_epoch=int(time.time() + delay),
            )
            await _wait_or_stop(delay, stop_event, server_task=server_task)
        except asyncio.TimeoutError:
            failures += 1
            delay = _retry_delay(failures)
            status.transition(
                "syncing_menu",
                "Telegram menu operation timed out.",
                reason_code="menu_transport_unavailable",
                attempt=failures,
                next_retry_at_epoch=int(time.time() + delay),
            )
            await _wait_or_stop(delay, stop_event, server_task=server_task)
        except TelegramMenuError:
            return _GenerationResult.MENU_BLOCKED
        except BridgeModeError:
            return _GenerationResult.MIRROR_BLOCKED


async def _monitor_generation(
    public_url: str,
    tunnel: QuickTunnel,
    menu: TelegramMenuManager,
    bridge: BridgeMirrorModeManager,
    owner: int,
    button_text: str,
    core_port: int,
    status: RuntimeStatus,
    stop_event: asyncio.Event,
    server_task: asyncio.Task[None],
) -> tuple[_GenerationResult, bool]:
    loop = asyncio.get_running_loop()
    ready_since = loop.time()
    next_core = loop.time() + _CORE_HEALTH_INTERVAL_SEC
    next_public = loop.time() + _PUBLIC_HEALTH_INTERVAL_SEC
    next_menu = loop.time() + _MENU_HEALTH_INTERVAL_SEC
    next_owner = loop.time() + _OWNER_HEALTH_INTERVAL_SEC
    confirmed_bad = 0
    degraded_reasons: set[str] = set()
    tunnel_wait = asyncio.create_task(tunnel.wait())
    stop_wait = asyncio.create_task(stop_event.wait())
    try:
        while True:
            done, _pending = await asyncio.wait(
                {tunnel_wait, stop_wait, server_task},
                timeout=_MONITOR_TICK_SEC,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_wait in done and stop_event.is_set():
                raise ShutdownRequested("Companion shutdown was requested.")
            if server_task in done:
                try:
                    await server_task
                except asyncio.CancelledError as exc:
                    raise SidecarStoppedError(
                        "Authentication sidecar was cancelled unexpectedly."
                    ) from exc
                except Exception as exc:
                    raise SidecarStoppedError(
                        "Authentication sidecar failed unexpectedly."
                    ) from exc
                raise SidecarStoppedError("Authentication sidecar stopped unexpectedly.")
            if tunnel_wait in done:
                return _GenerationResult.RECONNECT, loop.time() - ready_since >= _STABLE_READY_RESET_SEC
            now = loop.time()
            if now >= next_owner:
                next_owner = now + _OWNER_HEALTH_INTERVAL_SEC
                try:
                    if bridge.owner_chat_id() != owner:
                        return _GenerationResult.OWNER_CHANGED, False
                    if not bridge.safe_for_exposure(owner):
                        await _run_startup_operation(
                            bridge.activate(owner),
                            stop_event=stop_event,
                            timeout_sec=6.0,
                        )
                    if not bridge.safe_for_exposure(owner):
                        return _GenerationResult.MIRROR_BLOCKED, False
                except BridgeModeConflictError:
                    return _GenerationResult.MIRROR_BLOCKED, False
                except (BridgeModeTransportError, asyncio.TimeoutError):
                    return _GenerationResult.MIRROR_BLOCKED, False
                except BridgeModeError:
                    return _GenerationResult.MIRROR_BLOCKED, False
                else:
                    degraded_reasons.discard("owner_or_mirror_unobservable")
            if now >= next_core:
                next_core = now + _CORE_HEALTH_INTERVAL_SEC
                if await probe_core(core_port):
                    degraded_reasons.discard("core_unavailable")
                else:
                    degraded_reasons.add("core_unavailable")
            if now >= next_public:
                next_public = now + _PUBLIC_HEALTH_INTERVAL_SEC
                outcome = await probe_public_gateway_once(
                    public_url,
                    tunnel,
                    stop_event=stop_event,
                )
                if outcome is PublicProbeOutcome.TUNNEL_EXITED:
                    return (
                        _GenerationResult.RECONNECT,
                        loop.time() - ready_since >= _STABLE_READY_RESET_SEC,
                    )
                if outcome is PublicProbeOutcome.READY:
                    confirmed_bad = 0
                    degraded_reasons.discard("public_observer_unavailable")
                    degraded_reasons.discard("public_marker_unhealthy")
                elif outcome is PublicProbeOutcome.UNHEALTHY_RESPONSE:
                    confirmed_bad += 1
                    degraded_reasons.discard("public_observer_unavailable")
                    degraded_reasons.add("public_marker_unhealthy")
                    if confirmed_bad >= 3:
                        return (
                            _GenerationResult.RECONNECT,
                            loop.time() - ready_since >= _STABLE_READY_RESET_SEC,
                        )
                else:
                    degraded_reasons.discard("public_marker_unhealthy")
                    degraded_reasons.add("public_observer_unavailable")
            if now >= next_menu:
                next_menu = now + _MENU_HEALTH_INTERVAL_SEC
                try:
                    await _run_startup_operation(
                        menu.check_owned(public_url, button_text),
                        stop_event=stop_event,
                        timeout_sec=15.0,
                    )
                    degraded_reasons.discard("menu_transport_unavailable")
                except TelegramMenuConflictError:
                    return _GenerationResult.MENU_BLOCKED, False
                except TelegramMenuRejectedError:
                    return _GenerationResult.TELEGRAM_BLOCKED, False
                except (TelegramMenuTransportError, asyncio.TimeoutError):
                    degraded_reasons.add("menu_transport_unavailable")
                except TelegramMenuError:
                    return _GenerationResult.MENU_BLOCKED, False
            if degraded_reasons:
                reason = sorted(degraded_reasons)[0]
                if status.state != "degraded" or status.reason_code != reason:
                    status.transition(
                        "degraded",
                        "Mini App remains exposed through the last verified URL while a health signal recovers.",
                        reason_code=reason,
                    )
            elif status.state != "ready":
                status.transition(
                    "ready",
                    "Existing Ouroboros SPA is available from the private Telegram bot menu.",
                    reason_code="healthy",
                    public_url=public_url,
                )
    finally:
        stop_wait.cancel()
        if not tunnel_wait.done():
            tunnel_wait.cancel()
        await asyncio.gather(stop_wait, tunnel_wait, return_exceptions=True)


async def _wait_blocked_state(
    reason: _GenerationResult,
    menu: TelegramMenuManager,
    bridge: BridgeMirrorModeManager,
    owner: int,
    status: RuntimeStatus,
    stop_event: asyncio.Event,
    server_task: asyncio.Task[None],
) -> _GenerationResult:
    messages = {
        _GenerationResult.MENU_BLOCKED: (
            "Telegram menu changed outside this skill; public exposure is stopped.",
            "menu_drift",
        ),
        _GenerationResult.TELEGRAM_BLOCKED: (
            "Telegram rejected the bot configuration; disable and re-enable after fixing it.",
            "telegram_rejected",
        ),
        _GenerationResult.MIRROR_BLOCKED: (
            "telegram-bridge mirror mode changed externally; public exposure is stopped.",
            "mirror_mode_drift",
        ),
    }
    message, reason_code = messages[reason]
    while True:
        status.transition("blocked", message, reason_code=reason_code)
        await _wait_or_stop(30.0, stop_event, server_task=server_task)
        try:
            if bridge.owner_chat_id() != owner:
                return _GenerationResult.OWNER_CHANGED
            if reason is _GenerationResult.MENU_BLOCKED:
                await _run_startup_operation(
                    menu.check_snapshot_owned(),
                    stop_event=stop_event,
                    timeout_sec=15.0,
                )
                return _GenerationResult.RECONNECT
            if reason is _GenerationResult.MIRROR_BLOCKED:
                await _run_startup_operation(
                    bridge.activate(owner),
                    stop_event=stop_event,
                    timeout_sec=6.0,
                )
                if bridge.safe_for_exposure(owner):
                    return _GenerationResult.RECONNECT
        except (BridgeModeTransportError, TelegramMenuTransportError, asyncio.TimeoutError):
            continue
        except (
            BridgeModeError,
            TelegramMenuError,
        ):
            continue


async def _run_tunnel_generations(
    binary: Path,
    state_dir: Path,
    sidecar_port: int,
    sidecar: TelegramProxySidecar,
    menu: TelegramMenuManager,
    bridge: BridgeMirrorModeManager,
    owner: int,
    button_text: str,
    core_port: int,
    status: RuntimeStatus,
    stop_event: asyncio.Event,
    server_task: asyncio.Task[None],
) -> _GenerationResult:
    failures = 0
    blocked: _GenerationResult | None = None
    while True:
        if blocked is not None:
            blocked_result = await _wait_blocked_state(
                blocked,
                menu,
                bridge,
                owner,
                status,
                stop_event,
                server_task,
            )
            if blocked_result is _GenerationResult.OWNER_CHANGED:
                return blocked_result
            blocked = None
        require_running(stop_event)
        if failures:
            binary = await _wait_for_cloudflared(
                state_dir,
                status,
                stop_event,
                server_task=server_task,
            )
        tunnel = QuickTunnel(binary, state_dir, sidecar_port)
        result = _GenerationResult.RECONNECT
        stable_ready = False
        try:
            status.transition(
                "reconnecting" if failures else "starting",
                "Starting a pinned Cloudflare Quick Tunnel to the authentication sidecar.",
                reason_code="starting_tunnel",
                attempt=failures,
            )
            try:
                await _run_startup_operation(
                    tunnel.start(),
                    stop_event=stop_event,
                    timeout_sec=10.0,
                )
                public_url = await _run_startup_operation(
                    tunnel.wait_url(timeout_sec=30),
                    stop_event=stop_event,
                    timeout_sec=31.0,
                )
            except asyncio.TimeoutError as exc:
                raise CloudflaredError("Tunnel startup operation timed out.") from exc
            sidecar.set_public_url(public_url)
            status.transition(
                "verifying",
                "Verifying the public authentication boundary.",
                reason_code="verifying_public_gateway",
            )
            verification_result = await _wait_for_public_verification(
                public_url,
                tunnel,
                bridge,
                owner,
                status,
                stop_event,
                server_task,
            )
            if verification_result is not None:
                result = verification_result
            else:
                status.transition(
                    "syncing_menu",
                    "Synchronizing the private Telegram Mini App button.",
                    reason_code="syncing_menu",
                )
                sync_result = await _sync_menu_until_confirmed(
                    public_url,
                    tunnel,
                    menu,
                    bridge,
                    owner,
                    button_text,
                    status,
                    stop_event,
                    server_task,
                )
                if sync_result is not None:
                    result = sync_result
                else:
                    status.transition(
                        "ready",
                        "Existing Ouroboros SPA is available from the private Telegram bot menu.",
                        reason_code="healthy",
                        public_url=public_url,
                    )
                    result, stable_ready = await _monitor_generation(
                        public_url,
                        tunnel,
                        menu,
                        bridge,
                        owner,
                        button_text,
                        core_port,
                        status,
                        stop_event,
                        server_task,
                    )
        except (CloudflaredError, CompanionError) as exc:
            if isinstance(exc, (ShutdownRequested, SidecarStoppedError)):
                raise
            result = _GenerationResult.RECONNECT
            status.transition(
                "reconnecting",
                _safe_error(exc),
                reason_code="tunnel_generation_failed",
                attempt=failures + 1,
            )
        finally:
            sidecar.clear_public_url()
            await tunnel.stop()
        if result is _GenerationResult.OWNER_CHANGED:
            return result
        if result in {
            _GenerationResult.MENU_BLOCKED,
            _GenerationResult.TELEGRAM_BLOCKED,
            _GenerationResult.MIRROR_BLOCKED,
        }:
            blocked = result
            continue
        failures = 0 if stable_ready else failures + 1
        delay = _retry_delay(max(1, failures))
        status.transition(
            "reconnecting",
            "The tunnel generation ended; retrying inside the same companion.",
            reason_code="tunnel_reconnect_backoff",
            attempt=failures,
            next_retry_at_epoch=int(time.time() + delay),
        )
        await _wait_or_stop(delay, stop_event, server_task=server_task)


async def _reconcile_owner_change(
    menu: TelegramMenuManager | None,
    bridge: BridgeMirrorModeManager,
    status: RuntimeStatus,
    stop_event: asyncio.Event,
) -> None:
    """Independently finish both compensations before accepting a new owner."""

    menu_done = menu is None
    bridge_done = False
    failures = 0
    while not (menu_done and bridge_done):
        operations: list[tuple[str, Awaitable[bool]]] = []
        if not menu_done and menu is not None:
            operations.append(("menu", menu.restore()))
        if not bridge_done:
            operations.append(("bridge", bridge.restore()))
        results = await asyncio.gather(
            *(
                _run_startup_operation(
                    operation,
                    stop_event=stop_event,
                    timeout_sec=15.0 if name == "menu" else 6.0,
                )
                for name, operation in operations
            ),
            return_exceptions=True,
        )
        errors: list[BaseException] = []
        for (name, _operation), result in zip(operations, results):
            if isinstance(result, BaseException):
                if isinstance(result, ShutdownRequested):
                    raise result
                errors.append(result)
            elif name == "menu":
                menu_done = True
            else:
                bridge_done = True
        if not errors:
            continue
        failures += 1
        transport_only = all(
            isinstance(
                error,
                (TelegramMenuTransportError, BridgeModeTransportError, asyncio.TimeoutError),
            )
            for error in errors
        )
        delay = _retry_delay(failures) if transport_only else 30.0
        status.transition(
            "reconciling",
            _safe_error(errors[0]),
            reason_code=(
                "owner_change_rollback_pending"
                if transport_only
                else "owner_change_rollback_conflict"
            ),
            attempt=failures,
            next_retry_at_epoch=int(time.time() + delay),
        )
        await _wait_or_stop(delay, stop_event)


def _prior_owner_reconciliation(
    token: str,
    current_owner: int,
    state_dir: Path,
    telegram_client: httpx.AsyncClient,
    bridge: BridgeMirrorModeManager,
) -> tuple[bool, TelegramMenuManager | None]:
    menu_owner = snapshot_owner_chat_id(state_dir)
    bridge_owner = bridge.owned_owner_chat_id()
    prior_owners = {
        value
        for value in (menu_owner, bridge_owner)
        if isinstance(value, int) and value > 0
    }
    if len(prior_owners) > 1:
        raise BridgeModeConflictError("Menu and bridge ownership ledgers disagree.")
    if not prior_owners or current_owner in prior_owners:
        return False, None
    prior_owner = next(iter(prior_owners))
    prior_menu = None
    if menu_owner is not None:
        prior_menu = TelegramMenuManager(
            token,
            prior_owner,
            state_dir,
            client=telegram_client,
        )
    return True, prior_menu


async def run() -> int:
    state_dir: Path | None = None
    sidecar: TelegramProxySidecar | None = None
    server: uvicorn.Server | None = None
    server_task: asyncio.Task[None] | None = None
    menu: TelegramMenuManager | None = None
    bridge: BridgeMirrorModeManager | None = None
    telegram_client: httpx.AsyncClient | None = None
    lifeline_cancel: threading.Event | None = None
    lifeline_thread: threading.Thread | None = None
    clean_shutdown = False
    rollback_complete = False
    fatal_error_message = ""
    stop_event = asyncio.Event()
    heartbeat_stop = asyncio.Event()
    heartbeat_task: asyncio.Task[None] | None = None
    status: RuntimeStatus | None = None
    singleton_fd: int | None = None
    sidecar_failures = 0

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop_event.set)

    try:
        require_isolated_process_group()
        state_raw = os.environ.get("OUROBOROS_SKILL_STATE_DIR", "")
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not state_raw:
            raise CompanionError("OUROBOROS_SKILL_STATE_DIR is missing.")
        state_dir = _safe_state_dir(state_raw)
        singleton_fd = acquire_singleton(state_dir)
        status = RuntimeStatus(state_dir, cloudflared_version=CLOUDFLARED_VERSION)
        status.transition(
            "starting",
            "Checking local Ouroboros and Telegram owner.",
            reason_code="initializing",
        )
        heartbeat_task = asyncio.create_task(
            status.heartbeat(heartbeat_stop, failure_event=stop_event)
        )
        if not token:
            raise CompanionError("TELEGRAM_BOT_TOKEN is missing or not granted.")
        parent_pid = _INITIAL_PARENT_PID
        if not _pid_alive(parent_pid):
            raise CompanionError("Ouroboros server parent is unavailable.")
        lifeline_cancel, lifeline_thread = start_parent_lifeline(
            parent_pid,
            loop,
            stop_event,
        )
        config = load_runtime_config(state_dir)

        telegram_client = httpx.AsyncClient(
            timeout=12,
            follow_redirects=False,
            trust_env=False,
        )
        bridge = BridgeMirrorModeManager(state_dir, config["core_port"])
        while True:
            owner, candidate_menu = await _wait_for_owner(
                bridge,
                token,
                state_dir,
                telegram_client,
                status,
                stop_event,
            )
            try:
                reconcile_prior_owner, prior_menu = _prior_owner_reconciliation(
                    token,
                    owner,
                    state_dir,
                    telegram_client,
                    bridge,
                )
            except (TelegramMenuError, BridgeModeError) as exc:
                status.transition(
                    "blocked",
                    _safe_error(exc),
                    reason_code="menu_snapshot_owner_invalid",
                )
                await _wait_or_stop(30.0, stop_event)
                continue
            if reconcile_prior_owner:
                # Keep the previous owner in the outer cleanup slot so a stop
                # during compensation retries the correct Telegram chat.
                menu = prior_menu
                await _reconcile_owner_change(menu, bridge, status, stop_event)
                menu = None
                # Owner settings may have changed again while compensation was
                # in flight. Re-read and re-verify before any new exposure.
                continue
            menu = candidate_menu
            # Complete the potentially slow first download before taking
            # ownership of the text bridge's mirror setting.
            binary = await _wait_for_cloudflared(state_dir, status, stop_event)
            owner_still_current = await _wait_for_core_and_mirror(
                bridge,
                owner,
                config["core_port"],
                status,
                stop_event,
            )
            if not owner_still_current:
                menu = None
                continue
            sidecar = TelegramProxySidecar(
                token,
                owner,
                config["core_port"],
                session_ttl_sec=3600,
            )
            status.set_metrics_provider(sidecar.diagnostics)
            try:
                server, server_task, sidecar_port = await _run_startup_operation(
                    start_sidecar(sidecar),
                    stop_event=stop_event,
                    timeout_sec=6.0,
                )
            except (CompanionError, OSError, asyncio.TimeoutError) as exc:
                if isinstance(exc, ShutdownRequested):
                    raise
                sidecar_failures += 1
                delay = _retry_delay(sidecar_failures)
                status.transition(
                    "reconnecting",
                    _safe_error(exc),
                    reason_code="sidecar_start_failed",
                    attempt=sidecar_failures,
                    next_retry_at_epoch=int(time.time() + delay),
                )
                await stop_sidecar(server, server_task, sidecar)
                status.set_metrics_provider(None)
                sidecar = None
                server = None
                server_task = None
                await _wait_or_stop(delay, stop_event)
                continue
            sidecar_started_at = time.monotonic()
            sidecar_failure: SidecarStoppedError | None = None
            try:
                try:
                    result = await _run_tunnel_generations(
                        binary,
                        state_dir,
                        sidecar_port,
                        sidecar,
                        menu,
                        bridge,
                        owner,
                        config["button_text"],
                        config["core_port"],
                        status,
                        stop_event,
                        server_task,
                    )
                except SidecarStoppedError as exc:
                    sidecar_failure = exc
            finally:
                sidecar.clear_public_url()
                await stop_sidecar(server, server_task, sidecar)
                status.set_metrics_provider(None)
                sidecar = None
                server = None
                server_task = None
            if sidecar_failure is not None:
                if time.monotonic() - sidecar_started_at >= _STABLE_READY_RESET_SEC:
                    sidecar_failures = 0
                sidecar_failures += 1
                delay = _retry_delay(sidecar_failures)
                status.transition(
                    "reconnecting",
                    _safe_error(sidecar_failure),
                    reason_code="sidecar_restarting",
                    attempt=sidecar_failures,
                    next_retry_at_epoch=int(time.time() + delay),
                )
                await _wait_or_stop(delay, stop_event)
                continue
            if result is _GenerationResult.OWNER_CHANGED:
                # Rebind only after the old owner's menu and mirror state are
                # restored. Any conflict remains visible and fail-closed.
                await _reconcile_owner_change(menu, bridge, status, stop_event)
                sidecar_failures = 0
                menu = None
                continue
    except ShutdownRequested:
        heartbeat_failure: BaseException | None = None
        if heartbeat_task is not None and heartbeat_task.done() and not heartbeat_task.cancelled():
            with contextlib.suppress(asyncio.CancelledError):
                heartbeat_failure = heartbeat_task.exception()
        if heartbeat_failure is not None:
            fatal_error_message = _safe_error(heartbeat_failure)
            if status is not None:
                with contextlib.suppress(Exception):
                    status.transition(
                        "error",
                        fatal_error_message,
                        reason_code="status_heartbeat_failed",
                    )
            return 1
        clean_shutdown = True
        return 0
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        fatal_error_message = _safe_error(exc)
        if status is not None and singleton_fd is not None:
            with contextlib.suppress(Exception):
                status.transition(
                    "error",
                    _safe_error(exc),
                    reason_code="fatal_companion_error",
                )
        return 1
    finally:
        if status is not None:
            with contextlib.suppress(Exception):
                status.transition(
                    "stopping",
                    "Stopping public exposure and reconciling owned settings.",
                    reason_code="shutdown",
                )
        if sidecar is not None:
            sidecar.clear_public_url()

        async def restore_menu() -> bool:
            if menu is None:
                return True
            # The host allows five seconds before hard-killing a companion.
            # A restore may issue get/set/get, so bound each request separately
            # and let the complete transaction fit inside the 2.5s envelope.
            await menu.restore(
                request_timeout_sec=_SHUTDOWN_TELEGRAM_REQUEST_TIMEOUT_SEC,
            )
            return True

        async def restore_bridge_mode() -> bool:
            if bridge is None:
                return True
            await bridge.restore()
            return True

        async def close_sidecar() -> None:
            await stop_sidecar(server, server_task, sidecar)

        cleanup_results = await asyncio.gather(
            asyncio.wait_for(restore_menu(), timeout=2.5),
            asyncio.wait_for(restore_bridge_mode(), timeout=2.5),
            asyncio.wait_for(close_sidecar(), timeout=0.75),
            return_exceptions=True,
        )
        rollback_complete = cleanup_results[0] is True and cleanup_results[1] is True
        if telegram_client is not None:
            with contextlib.suppress(Exception):
                await telegram_client.aclose()
        if clean_shutdown and status is not None:
            with contextlib.suppress(Exception):
                if rollback_complete:
                    status.transition(
                        "stopped",
                        "Mini App stopped; prior Telegram button and mirror mode are restored.",
                        reason_code="clean_shutdown",
                    )
                else:
                    status.transition(
                        "rollback_pending",
                        "Mini App stopped; durable rollback state remains for the next enable.",
                        reason_code="rollback_pending",
                    )
        elif fatal_error_message and status is not None:
            with contextlib.suppress(Exception):
                status.transition(
                    "error",
                    fatal_error_message,
                    reason_code=(
                        "fatal_error" if rollback_complete else "fatal_error_rollback_pending"
                    ),
                )
        heartbeat_stop.set()
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if lifeline_cancel is not None:
            lifeline_cancel.set()
        if lifeline_thread is not None:
            lifeline_thread.join(timeout=0.5)
        release_singleton(singleton_fd)


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
