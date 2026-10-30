"""Slack and Telegram reaction facts through a real core's Presence routes (local opt-in).

    OUROBOROS_PRESENCE_CORE_ROOT=/path/to/ouroboros python -m pytest -q -rs tests/test_reaction_core_consumer.py

Without that variable, or with a core lacking the reaction fact, this is reported as skipped,
never as passed. The connectors run unchanged (parser, store, workers, provider client, Host
client); providers are scripted and core's own app answers over ASGI. Only skill-token
authentication and inference are stubbed. Because a partial core deliberately does not advertise
the capabilities yet, ``capable=True`` adds them to the ``/identity`` answer here: this proves the
schema and route seam, not that a core is ready to advertise them, and not the sleeping-author or
final-selection paths. The run is a subprocess whose HOME, data and settings are temporary.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE_ENV = "OUROBOROS_PRESENCE_CORE_ROOT"
TOKEN = "fixture-skill-token"


def _run(tmp_path, mode):
    core = os.environ.get(CORE_ENV, "").strip()
    if not core:
        pytest.skip(f"{CORE_ENV} is not set; the core Presence routes were not exercised")
    if not (Path(core) / "ouroboros" / "place_intake.py").read_text(encoding="utf-8").count("reaction_refusal"):
        pytest.skip("this core has no reaction fact; the routes were not exercised")
    env = {
        "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "home"),
        "OUROBOROS_APP_ROOT": str(tmp_path / "app"), "OUROBOROS_DATA_DIR": str(tmp_path / "data"),
        "OUROBOROS_SETTINGS_PATH": str(tmp_path / "data" / "settings.json"), "OUROBOROS_REPO_DIR": str(tmp_path / "repo"),
        "PYTHONPATH": os.pathsep.join([str(Path(core).resolve()), str(ROOT / "skills" / "slack-bridge"),
                                       str(ROOT / "skills" / "telegram-bot")]),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
    }
    for name in ("home", "app", "data", "repo"):
        (tmp_path / name).mkdir()
    run = subprocess.run([sys.executable, __file__, str(tmp_path), mode], env=env, cwd=tmp_path,
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-4000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_slack_reactions_through_the_real_routes(tmp_path):
    facts = _run(tmp_path, "slack")
    assert facts["refusals"] == {}
    assert facts["statuses"] == {"Ev-m": 200, "Ev-reply": 200, "Ev-r": 200, "Ev-on-reply": 200, "Ev-miss": 200, "Ev-removed": 200}
    # Only the ordinary message woke anybody, under a wake list that matches everything.
    assert facts["woken"] == ["Ev-m", "Ev-reply"]
    assert facts["reasons"] == {"Ev-r": "reaction_context_only", "Ev-on-reply": "reaction_context_only",
                                "Ev-miss": "reaction_context_only", "Ev-removed": "reaction_context_only"}
    assert facts["rows"]["Ev-on-reply"]["thread"] == "1.0"
    rows = facts["rows"]
    assert rows["Ev-r"]["text"] == "" and rows["Ev-r"]["thread"] == "1.0"
    assert "Reaction, not speech" in rows["Ev-r"]["read"] and "eyes" in rows["Ev-r"]["read"]
    assert rows["Ev-miss"]["thread"] == "" and "Reaction, not speech" in rows["Ev-miss"]["read"]
    assert rows["Ev-removed"]["read"].count("wave") == 1
    assert facts["candidates"] == ["U1", "U2"]  # individuals only; Slack has no chat or aggregate actor
    assert facts["inbox"]["Ev-r"] == "delivered" and facts["inbox"]["Ev-miss"] == "delivered"
    assert facts["receipts"] == {"react-ok": "delivered", "react-noop": "delivered", "react-gone": "failed"}
    assert facts["recorded"] == 3
    assert any("added reaction" in said for said in facts["actions"])
    assert any("no change" in said for said in facts["actions"])
    assert any("failed" in said for said in facts["actions"])


def test_telegram_reactions_through_the_real_routes(tmp_path):
    facts = _run(tmp_path, "telegram")
    assert facts["woken"] == ["telegram:9:1"]
    assert facts["reasons"] == {"telegram:9:2": "reaction_context_only", "telegram:9:3": "reaction_context_only",
                                "telegram:9:4": "reaction_context_only", "telegram:9:5": "reaction_context_only"}
    rows = facts["rows"]
    assert "Reaction, not speech" in rows["telegram:9:2"]["read"]
    assert rows["telegram:9:2"]["thread"] == "5"  # the retained message's topic
    assert rows["telegram:9:3"]["thread"] == ""  # unknown forum target: not General's author
    assert "chat" == rows["telegram:9:4"]["author_kind"] and "anonymous" == rows["telegram:9:5"]["author_kind"]
    assert facts["candidates"] == ["55"]  # never the chat or the aggregate
    assert facts["receipts"] == {"set": "delivered", "refused": "failed"}
    assert facts["recorded"] == 2
    assert any("set reaction" in said for said in facts["actions"])
    assert any("failed" in said for said in facts["actions"])


def test_unadvertising_core_receives_no_reaction_from_either_connector(tmp_path):
    facts = _run(tmp_path, "unadvertised")
    assert facts["slack_turns"] == ["Ev-m", "Ev-reply"] and facts["telegram_turns"] == ["telegram:9:1"]
    assert facts["slack_withheld"] == 4 and facts["telegram_withheld"] == 4
    assert facts["reports"] == 0 and facts["slack_applied"] == "applied" and facts["telegram_applied"] == "applied"


def test_declared_reads_pass_the_real_guard_where_writes_do_not(tmp_path):
    facts = _run(tmp_path, "reads")
    for situation in ("advises", "silent", "paused"):
        row = facts[situation]
        assert row["slack_reactions"] == "" and row["slack_thread"] == ""
        assert row["slack_reaction_add"].startswith("⚠️ RESOURCE_CONSTRAINT_BLOCKED")
        # The generic method stays a write in the manifest, even when its caller selects a read.
        assert row["slack_api(effect=write)"].startswith("⚠️ RESOURCE_CONSTRAINT_BLOCKED")
        assert row["slack_api(effect=read)"].startswith("⚠️ RESOURCE_CONSTRAINT_BLOCKED")
    assert facts["requests"] == ["/api/reactions.get", "/api/conversations.replies"]
    assert facts["reactions"]["reactions"] == [{"name": "eyes", "count": 5, "users": ["U1"]}]
    reply = next(row for row in facts["thread"]["messages"] if row["ts"] == "1.5")
    assert reply["thread_ts"] == "1.0" and reply["user"] == "U2"


def test_frozen_list_report_is_not_posted_to_a_core_that_does_not_advertise_it(tmp_path):
    facts = _run(tmp_path, "frozen-held")
    assert facts["posts"] == 0 and facts["recorded"] == 0 and facts["acked"] is False
    assert facts["error"].startswith("Held:") and facts["provider_calls"] == 0 and facts["stored_shape"] == "list"


def test_frozen_list_report_is_recorded_typed_by_a_real_core_and_reads_as_an_action(tmp_path):
    facts = _run(tmp_path, "frozen")
    assert facts["status"] == 200 and facts["recorded"] == 1 and facts["acked"] is True
    assert facts["stored_shape"] == "list" and facts["provider_calls"] == 0
    assert len(facts["actions"]) == 1 and "set reaction 👍" in facts["actions"][0]


def test_telegram_known_outcomes_reach_a_real_core_once_with_one_physical_call_each(tmp_path):
    facts = _run(tmp_path, "telegram-known")
    assert facts["provider_calls"] == {"7": 1, "8": 2}  # the album member is asked once per request
    assert facts["receipts"] == {"late-record": "delivered", "timeout": "uncertain", "album": "delivered"}
    assert facts["statuses"] == [200, 200, 200] and facts["recorded"] == 3
    assert facts["album_receipt"] == {"media_group_id": "album-1", "effective_message_id": "unknown"}
    assert facts["actions"] == ["set reaction 👍, requested on message 7",
                                "outcome unknown, may have landed, do not repeat: set reaction 👍, requested on message 8",
                                "set reaction 👍, requested on message 8"]
    assert facts["unacked"] == 0


def test_slack_padded_row_reaches_the_connected_place_with_one_address(tmp_path):
    facts = _run(tmp_path, "slack-padded")
    assert facts["writes"] == [{"channel": "C1", "timestamp": "1.5", "name": "eyes"}]
    assert facts["state"] == "delivered" and facts["recorded"] == 1
    assert facts["conversation"] == "C1" and facts["thread"] == "1.0"
    assert facts["target"] == {"message_id": "1.5", "resolution": "resolved"}
    assert any("added reaction" in said for said in facts["actions"])


# ---- subprocess -----------------------------------------------------------------------------

def _core(root, connector, provider, account, conversation, *, capable=True):
    from types import SimpleNamespace

    import httpx

    from ouroboros import places as p, presence_admission, presence_context
    from ouroboros.gateway import host_service

    data = root / "data"
    p.connect_place(data, connector=connector, provider=provider, account_id=account, conversation_id=conversation,
                    wakes=["any_message", "from_bot"])
    presence_admission.focus_enabled = lambda _root: True
    presence_admission.focus_allowance = lambda _root: {"status": "available", "remaining_usd": 2, "settled_usd": 1}
    presence_context._own_work_section = lambda *_args: ""

    async def authenticated(_ctx, raw_token, permission=""):
        assert raw_token == TOKEN
        return connector, {}
    host_service._authenticated = authenticated
    woken = []

    def runner(**kwargs):
        woken.append(kwargs["event"].source_event_id)
        return SimpleNamespace(outcome="silent", text="", task_id="focus", work_ref="")

    app = host_service.create_host_service_app(data, presence_runner=runner)
    inner = httpx.ASGITransport(app=app)
    world = SimpleNamespace(data=data, woken=woken, statuses={}, refusals={}, turns=[], reports=0, recorded=0)

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            path = request.url.path
            response = await inner.handle_async_request(request)
            if path in {"/identity", "/presence/turn", "/presence/delivery"}:
                body = await response.aread()
                if path == "/identity" and capable:
                    # The stand-in described in this module's docstring.
                    answer = {**json.loads(body), "presence_reaction_context": 1, "presence_explicit_completion": 1}
                    return httpx.Response(response.status_code, json=answer)
                if path == "/presence/turn":
                    source = json.loads(request.content)["event"]["source_event_id"]
                    world.turns.append(source)
                    world.statuses[source] = response.status_code
                    if response.status_code >= 400:
                        world.refusals[source] = json.loads(body)
                if path == "/presence/delivery":
                    world.reports += 1
                    world.recorded += int(json.loads(body).get("recorded") is True)
                return httpx.Response(response.status_code, content=body, headers=response.headers)
            return response
    world.transport = Transport()
    return world


async def _settle_slack(worker, store):
    """Reports run beside sending and a real route takes real time, so wait for them, within bounds."""
    import asyncio
    import sqlite3

    for _ in range(400):
        await worker.process_once()
        await asyncio.sleep(0.01)
        with sqlite3.connect(store.path) as db:
            busy = db.execute("SELECT COUNT(*) FROM outbox WHERE state IN ('pending','leased') "
                              "OR report_state IN ('pending','reporting')").fetchone()[0]
        if not busy:
            return


def _readback(world):
    from ouroboros import dialogue_store
    from ouroboros.dialogue_provenance import reaction_action_words, row_author, dialogue_text, reaction_fact
    from ouroboros.place_arrivals import account_candidates

    rows, reasons, actions = {}, {}, []
    with dialogue_store.transaction(world.data, write=False) as conn:
        dispatch = {str(row["client_message_id"]): json.loads(row["dispatch_input"] or "{}").get("place_attention")
                    for row in conn.execute("SELECT client_message_id,dispatch_input FROM dialogue WHERE stream='chat' AND direction='in'")}
    for entry in dialogue_store.iter_rows(world.data, "chat"):
        row = entry.row
        transport = row.get("transport") or {}
        if row.get("direction") == "in" and reaction_fact(transport.get("message")):
            source = str(row.get("client_message_id") or "")
            rows[source] = {"text": row.get("text"), "read": dialogue_text(row), "thread": str(transport.get("thread_id") or ""),
                            "author_kind": row_author(row).get("actor_kind", row_author(row).get("kind"))}
            reasons[source] = (dispatch.get(source) or {}).get("reason")
        if row.get("type") == "presence_delivery" and reaction_fact(transport.get("message")):
            actions.append(reaction_action_words(reaction_fact(transport.get("message")), str((transport.get("delivery") or {}).get("state") or "")))
    return rows, reasons, actions, account_candidates


def _slack(root, *, capable=True):
    import asyncio
    import sqlite3

    import httpx

    from lib.events import parse_socket_envelope
    from lib.host_adapter import LoopbackPresenceHostAdapter
    from lib.runtime import InboundWorker, OutboundWorker, _discover_reporting
    from lib.slack_api import SlackClient
    from lib.store import BridgeStore

    world = _core(root, "slack-bridge", "slack", "T1", "", capable=capable)
    store = BridgeStore(root / "slack-state")
    answers = [{"ok": True}, {"ok": False, "error": "already_reacted"}, {"ok": False, "error": "message_not_found"}]

    def provider(request):
        path = request.url.path.rsplit("/", 1)[-1]
        if path == "users.info":
            return httpx.Response(200, json={"ok": True, "user": {"id": request.url.params["user"], "name": "n"}})
        if path == "conversations.replies":
            if "oldest" in request.url.params:
                return httpx.Response(200, json={"ok": False, "error": "thread_not_found"})
            return httpx.Response(200, json={"ok": True, "messages": [{"ts": "1.0", "user": "U1", "text": "hi"}], "has_more": False})
        if path.startswith("reactions."):
            return httpx.Response(200, json=answers.pop(0))
        return httpx.Response(200, json={"ok": True, "channel": {"id": request.url.params["channel"]}})

    def reaction(event_id, ts, *, kind="reaction_added", name="eyes", user="U2"):
        return {"type": "events_api", "envelope_id": event_id, "payload": {"event_id": event_id, "team_id": "T1", "event": {
            "type": kind, "user": user, "reaction": name, "event_ts": "2.0", "item_user": "U1",
            "item": {"type": "message", "channel": "C1", "ts": ts}}}}
    message = {"type": "events_api", "envelope_id": "Ev-m", "payload": {"event_id": "Ev-m", "team_id": "T1", "event": {
        "type": "message", "user": "U1", "channel": "C1", "channel_type": "channel", "ts": "1.0", "text": "hi"}}}
    reply = {"type": "events_api", "envelope_id": "Ev-reply", "payload": {"event_id": "Ev-reply", "team_id": "T1", "event": {
        "type": "message", "user": "U1", "channel": "C1", "channel_type": "channel", "ts": "1.5", "thread_ts": "1.0", "text": "more"}}}
    for payload in (message, reply, reaction("Ev-r", "1.0"), reaction("Ev-on-reply", "1.5"), reaction("Ev-miss", "9.0"),
                    reaction("Ev-removed", "1.0", kind="reaction_removed", name="wave")):
        store.ingest_envelope(payload, parse_socket_envelope(payload, bot_user_id="U_BOT"))

    async def work():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as slack_http, \
                httpx.AsyncClient(transport=world.transport, timeout=60.0) as host_http:
            slack = SlackClient("xoxb-fixture", "xapp-fixture", http_client=slack_http)
            adapter = LoopbackPresenceHostAdapter(binding_id="", host_service_url="http://127.0.0.1:18877",
                                                  skill_token=TOKEN, http_client=host_http)
            inbound = InboundWorker(store, slack, adapter, staged_root=root / "staged")
            for _ in range(8):
                await inbound.process_once()
            await _discover_reporting(adapter, store)
            store.set_runtime(workspace_id="T1")
            for request_id, operation in (("react-ok", "reaction_add"), ("react-noop", "reaction_add"), ("react-gone", "reaction_add")):
                store.enqueue_mutation(request_id=request_id, operation=operation,
                                       payload={"channel": "C1", "ts": "1.0", "name": "eyes"},
                                       origin={"kind": "tool"}, delivery_reporting_version=1)
            outbound = OutboundWorker(store, slack, adapter)
            await _settle_slack(outbound, store)
            await outbound.aclose()
    asyncio.run(work())
    with sqlite3.connect(store.path) as db:
        inbox = {row[0]: row[1] for row in db.execute("SELECT event_id,state FROM inbox")}
    return world, store, inbox


def _telegram(root, *, capable=True):
    import asyncio
    import json as _json

    import httpx

    from telegram_bot.api import TelegramClient
    from telegram_bot.events import parse_telegram_update
    from telegram_bot.host import PresenceHostClient, PresenceHostHTTPError
    from telegram_bot.runtime import TelegramTransportRuntime
    from telegram_bot.tools import make_operation_tool
    from types import SimpleNamespace

    world = _core(root, "telegram-bot", "telegram", "9", "", capable=capable)
    answers = [{"ok": True, "result": True}, {"ok": False, "error_code": 400, "description": "Bad Request: REACTION_INVALID"}]

    class Provider:
        calls = []

        async def post_json(self, url, payload, timeout_sec):
            self.calls.append(url.rsplit("/", 1)[-1])
            return answers.pop(0) if answers else {"ok": True, "result": True}

    class HostTransport:
        async def request_json(self, method, url, *, headers, payload, timeout_sec):
            async with httpx.AsyncClient(transport=world.transport, base_url="http://127.0.0.1:18877", timeout=60.0) as client:
                response = await client.request(method, url.replace("http://127.0.0.1:18877", ""), headers=headers, json=payload)
            body = response.json()
            if response.status_code >= 400:
                raise PresenceHostHTTPError(response.status_code, str(body.get("error")), body)
            return body

    state = root / "telegram-state"
    client = PresenceHostClient(state_dir=state, token_provider=lambda: TOKEN, host_base="http://127.0.0.1:18877",
                                transport=HostTransport())
    runtime = TelegramTransportRuntime(state_dir=state, token_provider=lambda: "fake", logger=None, submitter=client,
                                       client_factory=lambda token: TelegramClient(token, transport=Provider()))
    runtime._client, runtime._client_token, runtime._bot_id = TelegramClient("fake", transport=Provider()), "fake", "9"
    forum = {"id": -100, "type": "supergroup", "title": "Forum", "is_forum": True}
    ann = {"id": 55, "first_name": "Ann"}
    updates = [
        {"update_id": 1, "message": {"message_id": 7, "date": 1, "chat": forum, "from": ann, "text": "hello",
                                     "message_thread_id": 5, "is_topic_message": True}},
        {"update_id": 2, "message_reaction": {"chat": forum, "message_id": 7, "user": ann, "date": 2,
                                              "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}]}},
        {"update_id": 3, "message_reaction": {"chat": forum, "message_id": 999, "user": ann, "date": 3,
                                              "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}]}},
        {"update_id": 4, "message_reaction": {"chat": forum, "message_id": 7, "actor_chat": {"id": -77, "title": "News", "type": "channel"},
                                              "date": 4, "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "❤"}]}},
        {"update_id": 5, "message_reaction_count": {"chat": forum, "message_id": 7, "date": 5,
                                                    "reactions": [{"type": {"type": "emoji", "emoji": "👍"}, "total_count": 3}]}},
    ]

    async def work():
        await client.discover_delivery_support()
        for update in updates:
            runtime.store.commit_update(update["update_id"], parse_telegram_update(update, bot_account_id="9", places=True))
        for _ in range(8):
            await runtime.process_one_inbox()
        operate = make_operation_tool(SimpleNamespace(get_state_dir=lambda: str(state)), lambda: "9")
        for request_id in ("set", "refused"):
            result = operate(method="setMessageReaction", chat_id="-100", message_id=7, topic_id=5,
                             reaction=[{"type": "emoji", "emoji": "👍"}], request_id=request_id)
            assert result["state"] == "queued", result
        for _ in range(4):
            await runtime.process_one_outbox()
        while await runtime.process_one_delivery_report():
            pass
    asyncio.run(work())
    return world, runtime


def _reads(root):
    import asyncio
    from types import SimpleNamespace

    import httpx
    import yaml

    from lib import read_tools
    from lib.slack_api import SlackClient
    from ouroboros import extension_registry_state as registry, place_guard, places as p
    from ouroboros.extension_surface_names import extension_surface_name

    manifest = yaml.safe_load((ROOT / "skills/slack-bridge/SKILL.md").read_text(encoding="utf-8").split("---")[1])
    bundle = SimpleNamespace(generation_digest="g", loaded_skill=SimpleNamespace(
        manifest=SimpleNamespace(raw_extra={"tools": manifest["tools"]})))
    registry._extensions["slack-bridge"] = bundle
    tool = {"skill": "slack-bridge", "extension_generation": "g"}
    data = root / "data"
    place = p.connect_place(data, connector="slack-bridge", provider="slack", account_id="T1", conversation_id="C1",
                            wakes=["any_message"])
    reads = {"channel_id": "C1", "ts": "1.5"}
    calls = {"slack_reactions": ("slack_reactions", reads),
             "slack_thread": ("slack_thread", {"channel_id": "C1", "thread_ts": "1.5"}),
             "slack_reaction_add": ("slack_reaction_add", {"channel": "C1", "ts": "1.5", "name": "eyes"}),
             "slack_api(effect=write)": ("slack_api", {"path": "reactions.add", "effect": "write"}),
             "slack_api(effect=read)": ("slack_api", {"path": "reactions.get", "effect": "read"})}

    def refusals(metadata):
        ctx = SimpleNamespace(drive_root=data, task_id="caller", task_metadata=metadata)
        return {label: place_guard.dispatch_refusal(ctx, extension_surface_name("slack-bridge", short), args, ext_tool=tool)
                for label, (short, args) in calls.items()}

    facts = {"advises": refusals({"initiator": "consciousness", "consciousness_autonomy": "observe"})}
    for state in ("silent", "paused"):  # an ordinary task, which Advises does not narrow
        p.edit_place(data, place.place_id, "set_state", author="owner", state=state)
        facts[state] = refusals({})
    requests = []

    def provider(request):
        requests.append(request.url.path)
        if request.url.path.endswith("reactions.get"):
            return httpx.Response(200, json={"ok": True, "type": "message", "channel": "C1", "message": {
                "ts": "1.5", "text": "r", "reactions": [{"name": "eyes", "count": 5, "users": ["U1"]}]}})
        return httpx.Response(200, json={"ok": True, "has_more": False, "messages": [
            {"ts": "1.0", "thread_ts": "1.0", "user": "U9", "text": "root"},
            {"ts": "1.5", "thread_ts": "1.0", "user": "U2", "text": "r"}]})

    class Api:
        tools = {}

        def get_settings(self, keys):
            return {"SLACK_BOT_TOKEN": "xoxb-fixture", "SLACK_APP_TOKEN": "xapp-fixture"}

        def register_tool(self, name, handler, **metadata):
            self.tools[name] = handler

    async def work():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            read_tools.SlackClient = lambda bot, app: SlackClient(bot, app, http_client=http)
            api = Api()
            read_tools.register_read_tools(api)
            return [json.loads(await api.tools[name](**args)) for name, args in (
                ("slack_reactions", reads), ("slack_thread", {"channel_id": "C1", "thread_ts": "1.5"}))]
    facts["reactions"], facts["thread"] = asyncio.run(work())
    return {**facts, "requests": requests}


def _frozen(root, *, capable=True):
    import asyncio

    import httpx

    from telegram_bot.host import PresenceHostClient, PresenceHostHTTPError
    from telegram_bot.runtime import TelegramTransportRuntime

    world = _core(root, "telegram-bot", "telegram", "9", "", capable=capable)

    class HostTransport:
        status = 0

        async def request_json(self, method, url, *, headers, payload, timeout_sec):
            async with httpx.AsyncClient(transport=world.transport, base_url="http://127.0.0.1:18877", timeout=60.0) as client:
                response = await client.request(method, url.replace("http://127.0.0.1:18877", ""), headers=headers, json=payload)
            body = response.json()
            if url.endswith("/presence/delivery"):
                HostTransport.status = response.status_code
            if response.status_code >= 400:
                raise PresenceHostHTTPError(response.status_code, str(body.get("error")), body)
            return body

    state = root / "telegram-state"
    client = PresenceHostClient(state_dir=state, token_provider=lambda: TOKEN, host_base="http://127.0.0.1:18877",
                                transport=HostTransport())
    provider_calls = []

    class NoTelegram:
        async def operation(self, *args):
            provider_calls.append(args)
            raise AssertionError("a frozen report never reaches Telegram")
    runtime = TelegramTransportRuntime(state_dir=state, token_provider=lambda: "fake", logger=None, submitter=client,
                                       client_factory=lambda _token: NoTelegram())
    report = {"schema_version": 1, "delivery_id": "telegram-operation:old", "part_id": "0", "state": "delivered",
              "provider": "telegram", "account_id": "9", "conversation_id": "-200", "thread_id": "", "text": "", "format": "",
              "message": {"kind": "operation", "method": "setMessageReaction", "target_message_id": 7,
                          "reaction": [{"type": "emoji", "emoji": "👍"}]},
              "origin": {"kind": "tool"}}
    runtime.store.enqueue_outbox(report["delivery_id"], {
        "kind": "operation", "chat_id": "-200", "method": "setMessageReaction", "text": "",
        "parameters": {"chat_id": "-200", "message_id": 7, "reaction": report["message"]["reaction"]},
        "_reporting": {"version": 1, "account_id": "9", "origin": {"kind": "tool"}}, "_operation_result": {"ok": True}})
    runtime.store.claim_outbox(lease_sec=120)
    runtime.store.mark_delivered(report["delivery_id"], provider_receipt={"result": {"ok": True}}, report=report)

    async def work():
        while await runtime.process_one_delivery_report():
            pass
    asyncio.run(work())
    [entry] = runtime.store.delivery_receipt(report["delivery_id"])["history_reports"]
    _rows, _reasons, actions, _candidates = _readback(world)
    return {"status": HostTransport.status, "posts": world.reports, "recorded": world.recorded, "acked": entry["acked"],
            "error": entry["error"],
            "stored_shape": type(entry["payload"]["message"]["reaction"]).__name__, "provider_calls": len(provider_calls),
            "actions": actions}


def _telegram_known(root):
    """A confirmed set whose record fails, an ambiguous answer, and an album member, on the real routes."""
    import asyncio
    import sqlite3

    import httpx

    from telegram_bot.api import TelegramClient
    from telegram_bot.events import parse_telegram_update
    from telegram_bot.host import PresenceHostClient, PresenceHostHTTPError
    from telegram_bot.runtime import TelegramTransportRuntime
    from telegram_bot.tools import make_operation_tool
    from types import SimpleNamespace

    world = _core(root, "telegram-bot", "telegram", "9", "")
    answers = [{"ok": True, "result": True}, {"ok": False, "error_code": 408, "description": "Request Timeout"},
               {"ok": True, "result": True}]
    calls, statuses = [], []

    class Provider:
        async def post_json(self, url, payload, timeout_sec):
            calls.append(payload)
            return answers.pop(0)

    class HostTransport:
        async def request_json(self, method, url, *, headers, payload, timeout_sec):
            async with httpx.AsyncClient(transport=world.transport, base_url="http://127.0.0.1:18877", timeout=60.0) as client:
                response = await client.request(method, url.replace("http://127.0.0.1:18877", ""), headers=headers, json=payload)
            if url.endswith("/presence/delivery"):
                statuses.append(response.status_code)
            body = response.json()
            if response.status_code >= 400:
                raise PresenceHostHTTPError(response.status_code, str(body.get("error")), body)
            return body

    state = root / "telegram-state"
    host = PresenceHostClient(state_dir=state, token_provider=lambda: TOKEN, host_base="http://127.0.0.1:18877",
                              transport=HostTransport())
    runtime = TelegramTransportRuntime(state_dir=state, token_provider=lambda: "fake", logger=None, submitter=host)
    runtime._client, runtime._client_token, runtime._bot_id = TelegramClient("fake", transport=Provider()), "fake", "9"
    store = runtime.store
    group = {"id": -200, "type": "supergroup", "title": "Plain"}
    for update_id, message_id, album in ((1, 7, {}), (2, 8, {"media_group_id": "album-1"})):
        update = {"update_id": update_id, "message": {"message_id": message_id, "date": 1, "chat": group, "text": "words",
                                                      "from": {"id": 55, "first_name": "Ann"}, **album}}
        store.commit_update(update_id, parse_telegram_update(update, bot_account_id="9", places=True))
    operate = make_operation_tool(SimpleNamespace(get_state_dir=lambda: str(state)), lambda: "9")
    for request_id, message_id in (("late-record", 7), ("timeout", 8), ("album", 8)):
        queued = operate(method="setMessageReaction", chat_id="-200", message_id=message_id,
                         reaction=[{"type": "emoji", "emoji": "👍"}], request_id=request_id)
        assert queued["state"] == "queued", queued
    real_checkpoint, failed = store.checkpoint_outbox, []

    def fails_once_on_the_result(delivery_id, payload, **kwargs):
        if "_operation_result" in payload and not failed:
            failed.append(delivery_id)
            raise sqlite3.OperationalError("database is locked")
        return real_checkpoint(delivery_id, payload, **kwargs)
    store.checkpoint_outbox = fails_once_on_the_result

    def everything_due():
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE outbox SET available_at=0 WHERE state='pending'")

    async def work():
        await host.discover_delivery_support()
        for _ in range(5):
            await runtime.process_one_outbox()
            everything_due()
        while await runtime.process_one_delivery_report():
            pass
    asyncio.run(work())
    _rows, _reasons, actions, _candidates = _readback(world)
    keys = ("late-record", "timeout", "album")
    receipts = {key: store.delivery_receipt(f"telegram-operation:{key}") for key in keys}
    per_message = {}
    for call in calls:
        per_message[str(call["message_id"])] = per_message.get(str(call["message_id"]), 0) + 1
    return {"provider_calls": per_message, "receipts": {key: row["state"] for key, row in receipts.items()},
            "statuses": statuses, "recorded": world.recorded, "actions": actions,
            "album_receipt": receipts["album"]["provider_receipt"].get("album"),
            "unacked": sum(not entry["acked"] for row in receipts.values() for entry in row["history_reports"])}


def _slack_padded(root):
    """A row queued with padded arguments before this version, on a place connected to exactly C1."""
    import asyncio
    import sqlite3

    import httpx

    from lib.events import parse_socket_envelope
    from lib.host_adapter import LoopbackPresenceHostAdapter
    from lib.runtime import OutboundWorker, _discover_reporting
    from lib.slack_api import SlackClient
    from lib.store import BridgeStore

    world = _core(root, "slack-bridge", "slack", "T1", "C1")
    store = BridgeStore(root / "slack-state")
    store.set_runtime(workspace_id="T1")
    reply = {"type": "events_api", "envelope_id": "Ev-reply", "payload": {"event_id": "Ev-reply", "team_id": "T1", "event": {
        "type": "message", "user": "U1", "channel": "C1", "channel_type": "channel", "ts": "1.5", "thread_ts": "1.0", "text": "more"}}}
    store.ingest_envelope(reply, parse_socket_envelope(reply, bot_user_id="U_BOT"))
    with sqlite3.connect(store.path) as db:
        db.execute(
            """INSERT INTO outbox(request_id,chunk_index,chunk_count,target,thread_ts,text,ordering_key,kind,operation,
                                  payload_json,origin_json,delivery_reporting_version,created_at,updated_at)
               VALUES('padded',0,1,' C1 ','','',' C1 :','mutation','reaction_add',?,?,1,1,1)""",
            (json.dumps({"channel": " C1 ", "ts": " 1.5 ", "name": " eyes "}), json.dumps({"kind": "tool"})))
    writes, posted = [], []

    def provider(request):
        writes.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    class Recording(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == "/presence/delivery":
                posted.append(json.loads(request.content))
            return await world.transport.handle_async_request(request)

    async def work():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as slack_http, \
                httpx.AsyncClient(transport=Recording(), timeout=60.0) as host_http:
            adapter = LoopbackPresenceHostAdapter(binding_id="", host_service_url="http://127.0.0.1:18877",
                                                  skill_token=TOKEN, http_client=host_http)
            worker = OutboundWorker(store, SlackClient("xoxb-fixture", "xapp-fixture", http_client=slack_http), adapter)
            await _discover_reporting(adapter, store)
            await _settle_slack(worker, store)
            await worker.aclose()
    asyncio.run(work())
    _rows, _reasons, actions, _candidates = _readback(world)
    report = posted[0] if posted else {"message": {}}
    return {"writes": writes, "state": store.delivery_receipt("padded")["parts"][0]["state"], "recorded": world.recorded,
            "conversation": report.get("conversation_id"), "thread": report.get("thread_id"), "actions": actions,
            "target": (report["message"].get("reaction") or {}).get("target")}


def _drive(root: Path, mode: str) -> dict:
    if mode == "reads":
        return _reads(root)
    if mode == "telegram-known":
        return _telegram_known(root)
    if mode == "slack-padded":
        return _slack_padded(root)
    if mode == "frozen":
        return _frozen(root)
    if mode == "frozen-held":
        return _frozen(root, capable=False)
    if mode == "slack":
        world, store, inbox = _slack(root)
        rows, reasons, actions, candidates = _readback(world)
        receipts = {key: store.delivery_receipt(key)["parts"][0]["state"] for key in ("react-ok", "react-noop", "react-gone")}
        return {"statuses": world.statuses, "refusals": world.refusals, "woken": world.woken, "rows": rows, "reasons": reasons, "actions": actions,
                "inbox": inbox, "receipts": receipts, "recorded": world.recorded,
                "candidates": sorted(row["platform_actor_id"] for row in candidates(world.data, _place(world.data, "slack")))}
    if mode == "telegram":
        world, runtime = _telegram(root)
        rows, reasons, actions, candidates = _readback(world)
        receipts = {key: runtime.store.delivery_receipt(f"telegram-operation:{key}")["state"] for key in ("set", "refused")}
        return {"woken": world.woken, "rows": rows, "reasons": reasons, "actions": actions, "receipts": receipts,
                "recorded": world.recorded,
                "candidates": sorted(row["platform_actor_id"] for row in candidates(world.data, _place(world.data, "telegram")))}
    slack_world, slack_store, _ = _slack(root / "s" if (root / "s").mkdir() is None else root, capable=False)
    telegram_world, runtime = _telegram(root / "t" if (root / "t").mkdir() is None else root, capable=False)
    return {
        "slack_turns": slack_world.turns, "telegram_turns": telegram_world.turns,
        "slack_withheld": slack_store.status()["reactions_withheld"],
        "telegram_withheld": runtime.store.status_snapshot()["reactions_withheld"],
        "reports": slack_world.reports + telegram_world.reports,
        "slack_applied": slack_store.delivery_receipt("react-ok")["parts"][0]["provider_result"]["effect"],
        "telegram_applied": runtime.store.delivery_receipt("telegram-operation:set")["provider_receipt"]["effect"],
    }


def _place(data, provider):
    from ouroboros import places as p
    return next(place.place_id for place in p.load_places(data) if place.provider == provider)


if __name__ == "__main__":
    print(json.dumps(_drive(Path(sys.argv[1]), sys.argv[2]), ensure_ascii=False, default=str))
