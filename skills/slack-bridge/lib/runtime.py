from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import pathlib
import re
from typing import Any

from .host_adapter import HostBindingTerminalError, PresenceHostAdapter
from .provider_context import capture_context
from .slack_api import SlackApiError, SlackClient, chunk_message
from .socket_mode import SocketModeClient
from .store import BridgeStore, InboxItem

log = logging.getLogger(__name__)
_MAX_OUTBOX_ATTEMPTS = 5
# The host Presence endpoint may spend up to 1800 seconds on one turn. Keep
# the inbox lease alive for that full request plus a recovery buffer so a slow
# turn cannot be claimed and submitted a second time by another worker.
_INBOUND_LEASE_SECONDS = 2100.0


def _event_directory_name(item: InboxItem) -> str:
    source = item.event_id or item.envelope_id or str(item.row_id)
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", source)
    return clean[:120] or str(item.row_id)


async def _discover_reporting(host: Any, store: BridgeStore) -> int:
    discover = getattr(host, "discover_delivery_support", None)
    mode = await discover() if discover is not None else 0
    store.set_runtime(
        presence_delivery_version=mode,
        history_reporting_state=getattr(host, "delivery_reporting_status", "unsupported"),
        history_reporting_limitation="" if mode else "Host delivery reporting unavailable; provider sending remains enabled.",
    )
    return mode


def _automatic_origin(item: InboxItem, turn_ref: str) -> dict[str, str]:
    origin = {"kind": "automatic", "source_event_id": item.provider_event_key}
    if turn_ref:
        origin["task_id"] = turn_ref
    return origin


def _delivery_report(item: Any, state: str, *, result: dict[str, Any] | None = None,
                     error: str = "") -> dict[str, Any] | None:
    if item.delivery_reporting_version != 1:
        return None
    result = result or {}
    message = {"provider_message_id": str(result.get("ts") or ""),
               "requested_target": item.target, "target_resolved": bool(item.resolved_channel),
               "chunk_count": item.chunk_count}
    if error:
        message["error"] = error
    return {
        "schema_version": 1, "delivery_id": item.request_id, "part_id": str(item.chunk_index),
        "state": state, "provider": "slack", "account_id": item.provider_account_id,
        "conversation_id": str(result.get("channel") or item.resolved_channel or item.target),
        "thread_id": item.thread_ts, "text": item.text, "format": item.text_format,
        "message": message, "origin": item.origin,
    }


class InboundWorker:
    def __init__(
        self,
        store: BridgeStore,
        slack: SlackClient,
        host: PresenceHostAdapter,
        *,
        staged_root: pathlib.Path,
    ) -> None:
        self.store = store
        self.slack = slack
        self.host = host
        self.staged_root = staged_root

    async def process_once(self) -> bool:
        item = self.store.claim_inbox(lease_seconds=_INBOUND_LEASE_SECONDS)
        if item is None:
            return False
        try:
            if not item.host_reference and item.provider_context is None:
                snapshot = await capture_context(self.slack, item, self.store.workspace_name())
                self.store.set_provider_context(item.row_id, item.lease_token, snapshot)
                item = dataclasses.replace(item, provider_context=snapshot)
            if item.files and not item.staged_files:
                staged = await self.slack.stage_private_files(
                    item.files,
                    destination=self.staged_root / _event_directory_name(item),
                )
                staged_dicts = tuple(file.as_dict() for file in staged)
                self.store.set_staged_files(item.row_id, item.lease_token, staged_dicts)
                item = dataclasses.replace(item, staged_files=staged_dicts)

            reference = item.host_reference
            if not reference:
                await _discover_reporting(self.host, self.store)
                reference = str(await self.host.submit(item)).strip()
                if not reference:
                    raise RuntimeError(
                        "Presence Host adapter returned an empty reference"
                    )
                self.store.set_host_reference(item.row_id, item.lease_token, reference)

            status = await self.host.status(reference)
            delivery_key = hashlib.sha256(reference.encode("utf-8")).hexdigest()
            for index, text in enumerate(status.texts):
                chunks = chunk_message(text)
                if not chunks:
                    continue
                self.store.enqueue_outbox(
                    request_id=f"presence:{delivery_key}:ack:{index}",
                    target=item.channel_id,
                    thread_ts=item.reply_thread_ts,
                    chunks=chunks,
                    origin=_automatic_origin(item, status.turn_ref),
                    delivery_reporting_version=status.delivery_reporting_version,
                )
            if status.state == "failed":
                self.store.fail_inbox(
                    item.row_id,
                    item.lease_token,
                    status.error or "Presence Host turn failed",
                )
                return True
            if status.state != "ready":
                self.store.retry_inbox(
                    item.row_id,
                    item.lease_token,
                    f"Host turn state: {status.state}",
                    delay_seconds=5.0,
                )
                return True

            delivery = await self.host.deliver(reference)
            for index, text in enumerate(delivery.texts):
                chunks = chunk_message(text)
                if not chunks:
                    continue
                self.store.enqueue_outbox(
                    request_id=f"presence:{delivery_key}:final:{index}",
                    target=item.channel_id,
                    thread_ts=item.reply_thread_ts,
                    chunks=chunks,
                    origin=_automatic_origin(item, delivery.turn_ref),
                    delivery_reporting_version=delivery.delivery_reporting_version,
                )
            self.store.complete_inbox(item.row_id, item.lease_token)
            return True
        except asyncio.CancelledError:
            raise
        except HostBindingTerminalError as exc:
            self.store.fail_inbox(item.row_id, item.lease_token, str(exc))
            log.warning(
                "Slack inbound event %s failed terminally: %s", item.row_id, exc
            )
            return True
        except Exception as exc:
            delay = min(60.0, 2.0 ** min(item.attempts, 5))
            self.store.retry_inbox(
                item.row_id,
                item.lease_token,
                str(exc),
                delay_seconds=delay,
            )
            log.warning("Slack inbound event %s will retry: %s", item.row_id, exc)
            return True


class OutboundWorker:
    def __init__(self, store: BridgeStore, slack: SlackClient, host: Any = None) -> None:
        self.store = store
        self.slack = slack
        self.host = host
        self._report_task: asyncio.Task[None] | None = None

    async def _report(self, report: dict[str, Any]) -> None:
        try:
            await self.host.report_delivery(report["payload"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.store.finish_report(report, error=str(exc) or type(exc).__name__)
        else:
            self.store.finish_report(report)

    def _advance_reporting(self) -> bool:
        """At most one callback per existing worker, never awaited by sending."""
        advanced = False
        if self._report_task is not None:
            if not self._report_task.done():
                return False
            try:
                self._report_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log.warning("Slack history report checkpoint failed: %s", type(exc).__name__)
            self._report_task = None
            advanced = True
        if self.host is not None:
            report = self.store.claim_report()
            if report is not None:
                self._report_task = asyncio.create_task(self._report(report), name="slack-delivery-report")
                advanced = True
        return advanced

    async def aclose(self) -> None:
        task, self._report_task = self._report_task, None
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def process_once(self) -> bool:
        # A slow callback keeps its own task while this worker continues sending.
        reported = self._advance_reporting()
        item = self.store.claim_outbox(lease_seconds=60.0)
        if item is None:
            return reported
        item = dataclasses.replace(item, provider_account_id=(
            item.provider_account_id or str(self.store.runtime_value("workspace_id", ""))
        ))
        try:
            channel = item.resolved_channel or await self.slack.resolve_target(item.target)
            self.store.set_resolved_target(item, channel, item.provider_account_id)
            item = dataclasses.replace(item, resolved_channel=channel)
            result = await self.slack.post_message(
                channel=channel,
                text=item.text,
                thread_ts=item.thread_ts,
                text_format=item.text_format,
            )
            actual_channel = str(result.get("channel") or channel)
            if actual_channel != channel:
                self.store.set_resolved_target(item, actual_channel, item.provider_account_id)
                item = dataclasses.replace(item, resolved_channel=actual_channel)
            self.store.complete_outbox(
                item.row_id,
                item.lease_token,
                provider_message_ts=str(result.get("ts") or ""),
                report_payload=_delivery_report(item, "delivered", result=result),
            )
            return True
        except asyncio.CancelledError:
            raise
        except SlackApiError as exc:
            if item.attempts >= _MAX_OUTBOX_ATTEMPTS:
                uncertain = exc.status_code >= 500 or exc.status_code == 408 or exc.error in {
                    "invalid_json", "invalid_response", "internal_error", "fatal_error",
                }
                state = "uncertain" if uncertain else "failed"
                self.store.fail_outbox(item.row_id, item.lease_token, exc.error, state=state,
                                       report_payload=_delivery_report(item, state, error=exc.error))
                log.warning(
                    "Slack outbox item %s failed after %s attempts: %s",
                    item.row_id,
                    item.attempts,
                    exc,
                )
                return True
            delay = exc.retry_after or min(60.0, 2.0 ** min(item.attempts, 5))
            self.store.retry_outbox(
                item.row_id,
                item.lease_token,
                exc.error,
                delay_seconds=delay,
            )
            log.warning("Slack outbox item %s will retry: %s", item.row_id, exc)
            return True
        except Exception as exc:
            if item.attempts >= _MAX_OUTBOX_ATTEMPTS:
                self.store.fail_outbox(item.row_id, item.lease_token, str(exc), state="uncertain",
                                       report_payload=_delivery_report(item, "uncertain", error=type(exc).__name__))
                log.warning(
                    "Slack outbox item %s failed after %s attempts: %s",
                    item.row_id,
                    item.attempts,
                    exc,
                )
                return True
            delay = min(60.0, 2.0 ** min(item.attempts, 5))
            self.store.retry_outbox(
                item.row_id,
                item.lease_token,
                str(exc),
                delay_seconds=delay,
            )
            log.warning("Slack outbox item %s will retry: %s", item.row_id, exc)
            return True


async def _worker_loop(worker: Any, stop: asyncio.Event) -> None:
    try:
        while not stop.is_set():
            worked = await worker.process_once()
            if worked:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
    finally:
        close = getattr(worker, "aclose", None)
        if close is not None:
            await close()


class BridgeRuntime:
    def __init__(
        self,
        *,
        store: BridgeStore,
        slack: SlackClient,
        host: PresenceHostAdapter,
        bot_user_id: str,
        inbound_workers: int = 4,
        outbound_workers: int = 2,
    ) -> None:
        self.store = store
        self.slack = slack
        self.host = host
        self.socket = SocketModeClient(
            slack,
            store,
            bot_user_id=bot_user_id,
        )
        self.inbound_workers = max(1, min(16, int(inbound_workers)))
        self.outbound_workers = max(1, min(8, int(outbound_workers)))
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []

    async def run(self) -> None:
        await _discover_reporting(self.host, self.store)
        inbound_state = "active" if self.host.available else "missing_binding_id"
        self.store.set_runtime(host_adapter_state=inbound_state)
        self._tasks = [asyncio.create_task(self.socket.run(), name="slack-socket-mode")]
        for index in range(self.outbound_workers):
            worker = OutboundWorker(self.store, self.slack, self.host)
            self._tasks.append(
                asyncio.create_task(
                    _worker_loop(worker, self._stop),
                    name=f"slack-outbound-{index}",
                )
            )
        if self.host.available:
            staged_root = self.store.state_dir / "staged"
            for index in range(self.inbound_workers):
                worker = InboundWorker(
                    self.store,
                    self.slack,
                    self.host,
                    staged_root=staged_root,
                )
                self._tasks.append(
                    asyncio.create_task(
                        _worker_loop(worker, self._stop),
                        name=f"slack-inbound-{index}",
                    )
                )
        try:
            await asyncio.gather(*self._tasks)
        finally:
            await self.close()

    async def close(self) -> None:
        self._stop.set()
        await self.socket.close()
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current and not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(
                *(task for task in self._tasks if task is not current),
                return_exceptions=True,
            )
        self._tasks = []
        close_host = getattr(self.host, "aclose", None)
        if close_host is not None:
            await close_host()
        await self.slack.aclose()
        self.store.set_runtime(socket_state="disconnected")
