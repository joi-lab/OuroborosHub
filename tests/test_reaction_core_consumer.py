"""Slack and Telegram reaction facts through a real core's Presence routes (local opt-in).

    OUROBOROS_PRESENCE_CORE_ROOT=/path/to/ouroboros python -m pytest -q -rs tests/test_reaction_core_consumer.py

Without that variable, or with a core lacking the reaction fact, this is reported as skipped,
never as passed. The connectors run unchanged (parser, store, workers, provider client, Host
client); providers are scripted and core's own app answers over ASGI. Only skill-token
authentication and inference are stubbed. The connectors read ``/identity`` as one of three Hosts,
whatever a core advertises today. ``OLD_HOST`` strips both capabilities. ``EXPLICIT_ONLY_HOST``
strips the reaction context and leaves core's own answer on explicit completion, so its positive
case is real. ``CAPABLE_HOST`` adds both as a labelled stand-in: it proves the schema and route
seam, not that a core is ready to advertise them, and not the sleeping-author or final-selection
paths. The worker tests load the skill through core's real loader in separate processes and call it
through the real dispatch; only the LLM safety check and what ``urllib`` would reach (Telegram, and
the Host answering the polling process) are stubbed. Every run is a subprocess whose HOME, data and
settings are temporary.
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
# What each Host's /identity says about reactions, over core's own answer.
OLD_HOST = {"drop": ("presence_reaction_context", "presence_explicit_completion"), "add": {}}
EXPLICIT_ONLY_HOST = {"drop": ("presence_reaction_context",), "add": {}}
CAPABLE_HOST = {"drop": (), "add": {"presence_reaction_context": 1, "presence_explicit_completion": 1}}  # a stand-in, not proof


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
        (tmp_path / name).mkdir(exist_ok=True)
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


def test_old_core_receives_no_reaction_from_either_connector(tmp_path):
    facts = _run(tmp_path, "old-host")
    assert {"presence_reaction_context", "presence_explicit_completion"}.isdisjoint(facts["identity"])
    assert facts["slack_turns"] == ["Ev-m", "Ev-reply"] and facts["telegram_turns"] == ["telegram:9:1"]
    assert facts["slack_withheld"] == 4 and facts["telegram_withheld"] == 4
    assert facts["reports"] == 0 and facts["recorded"] == 0 and facts["actions"] == []
    assert facts["slack_applied"] == "applied" and facts["telegram_applied"] == "applied"


def test_explicit_completion_only_core_records_own_reactions_and_still_receives_none(tmp_path):
    facts = _run(tmp_path, "explicit-host")
    if facts["core_identity"].get("presence_explicit_completion") != 1:
        pytest.skip("this core does not advertise explicit completion; that Host was not exercised")
    assert facts["identity"].get("presence_explicit_completion") == 1 and "presence_reaction_context" not in facts["identity"]
    # Nothing incoming: without the reaction context each stays in custody, as on an old Host.
    assert facts["slack_turns"] == ["Ev-m", "Ev-reply"] and facts["telegram_turns"] == ["telegram:9:1"]
    assert facts["slack_withheld"] == 4 and facts["telegram_withheld"] == 4
    # Outgoing: three Slack and two Telegram receipts, each posted once and recorded as an action.
    assert facts["reports"] == 5 and facts["recorded"] == 5
    assert facts["actions"] == ["added reaction eyes, requested on message 1.0",
                                "no change: reaction eyes was already present, requested on message 1.0",
                                "failed: added reaction eyes, requested on message 1.0",
                                "set reaction 👍, requested on message 7", "failed: set reaction 👍, requested on message 7"]
    assert facts["slack_applied"] == "applied" and facts["telegram_applied"] == "applied"


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
    assert "presence_explicit_completion" not in facts["identity"]
    assert facts["posts"] == 0 and facts["recorded"] == 0 and facts["acked"] is False
    assert facts["error"].startswith("Held:") and facts["provider_calls"] == 0 and facts["stored_shape"] == "list"


def test_frozen_list_report_is_recorded_by_a_core_advertising_only_explicit_completion(tmp_path):
    facts = _run(tmp_path, "frozen-explicit")
    if facts["core_identity"].get("presence_explicit_completion") != 1:
        pytest.skip("this core does not advertise explicit completion; that Host was not exercised")
    assert facts["identity"].get("presence_explicit_completion") == 1 and "presence_reaction_context" not in facts["identity"]
    assert facts["status"] == 200 and facts["posts"] == 1 and facts["recorded"] == 1 and facts["acked"] is True
    assert facts["stored_shape"] == "list" and facts["provider_calls"] == 0
    assert len(facts["actions"]) == 1 and "set reaction 👍" in facts["actions"][0]


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


def _refused(answer, code):
    return answer.get("ok") is False and code in answer["error"]


def _assert_unconfirmed(facts):
    answers = facts["answers"]
    assert facts["run_task"] is False and facts["requests"] == []
    # Retained or not, nothing can be read as this bot's, and the refusal says why rather than "not retained here".
    for label in ("plain", "plain_unretained", "forum_missing", "unobserved"):
        assert _refused(answers[label], "forum_topic_unknown"), (label, answers[label])
        assert "identity is unconfirmed" in answers[label]["error"]
        assert "not retained here" not in answers[label]["error"] and "not observed yet" not in answers[label]["error"]
    # What the caller supplies, and a private chat, stay queued with nothing claimed as proven.
    for label in ("forum_right", "forum_wrong", "private_plain", "private_topic_missing", "private_topic_right"):
        assert answers[label]["state"] == "queued", (label, answers[label])
        assert facts["targets"][label] == "unresolved", label


def test_pooled_worker_answers_from_the_bot_the_server_process_confirmed(tmp_path):
    server = _run(tmp_path, "server:token-a")
    assert server["run_task"] is True and server["confirmed"] == ["9", ""] and server["offset"] == 7
    facts = _run(tmp_path, "worker:token-a")
    # The process split itself: core loaded it here and never started its supervised task.
    assert facts["server_process"] is False and facts["run_task"] is False and facts["live"] is True
    assert facts["confirmed"] == ["", "Telegram bot not running"]
    assert facts["status"]["connect"] is None and facts["status"]["connect_unavailable"] == "Telegram bot not running"
    answers, guards = facts["answers"], facts["guards"]
    assert set(guards.values()) == {""}  # the place guard admitted every call on its active place
    assert set(facts["dispatched"].values()) == {True}  # and core's dispatch reached the handler each time
    for label in ("plain", "plain_unretained", "forum_right", "forum_general", "private_plain", "private_topic_right"):
        assert answers[label]["state"] == "queued", (label, answers[label])
        assert facts["targets"][label] == "resolved", label
    assert facts["queued_topics"]["forum_right"] == "5" and facts["queued_topics"]["plain"] is None
    for label in ("forum_wrong", "forum_missing", "private_topic_missing", "plain_wrong"):
        assert _refused(answers[label], "forum_topic_mismatch"), (label, answers[label])
    assert "topic_id=5" in answers["forum_missing"]["error"] and "topic_id=9" in answers["private_topic_missing"]["error"]
    assert _refused(answers["forum_unretained"], "forum_topic_unknown") and "is a forum" in answers["forum_unretained"]["error"]
    assert _refused(answers["unobserved"], "forum_topic_unknown") and "not observed yet" in answers["unobserved"]["error"]
    assert facts["requests"] == []  # no provider, Host or other request from the worker


def test_pooled_worker_does_not_trust_a_changed_never_confirmed_or_refused_token(tmp_path):
    _run(tmp_path, "server:token-a")
    # The settings moved on and no server process has asked Telegram about this token.
    _assert_unconfirmed(_run(tmp_path, "worker:token-new"))
    # The server asked and Telegram refused it; the bot of the earlier token still does not answer for it.
    assert _run(tmp_path, "server:token-refused")["confirmed"][0] == ""
    _assert_unconfirmed(_run(tmp_path, "worker:token-refused"))
    # The token Telegram confirmed before, until Telegram refuses that one too (revoked).
    confirmed_again = _run(tmp_path, "worker:token-a")
    assert confirmed_again["targets"]["plain"] == "resolved"
    assert _refused(confirmed_again["answers"]["forum_wrong"], "forum_topic_mismatch")
    assert _run(tmp_path, "server:token-a:revoked")["confirmed"][0] == ""
    _assert_unconfirmed(_run(tmp_path, "worker:token-a"))
    # A different bot's token is confirmed as that bot, whose records do not include the first bot's messages.
    assert _run(tmp_path, "server:token-b")["confirmed"] == ["77", ""]
    other = _run(tmp_path, "worker:token-b")
    assert _refused(other["answers"]["plain"], "forum_topic_unknown") and "not observed yet" in other["answers"]["plain"]["error"]
    assert "identity is unconfirmed" not in other["answers"]["plain"]["error"]


# ---- subprocess -----------------------------------------------------------------------------

def _core(root, connector, provider, account, conversation, *, host=CAPABLE_HOST):
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
    world = SimpleNamespace(data=data, woken=woken, statuses={}, refusals={}, turns=[], reports=0, recorded=0,
                            core_identity={}, identity={})

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            path = request.url.path
            response = await inner.handle_async_request(request)
            if path in {"/identity", "/presence/turn", "/presence/delivery"}:
                body = await response.aread()
                if path == "/identity":
                    # The Host described in this module's docstring, over core's own answer.
                    world.core_identity = json.loads(body)
                    world.identity = {**{key: value for key, value in world.core_identity.items() if key not in host["drop"]},
                                      **host["add"]}
                    return httpx.Response(response.status_code, json=world.identity)
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


def _slack(root, *, host=CAPABLE_HOST):
    import asyncio
    import sqlite3

    import httpx

    from lib.events import parse_socket_envelope
    from lib.host_adapter import LoopbackPresenceHostAdapter
    from lib.runtime import InboundWorker, OutboundWorker, _discover_reporting
    from lib.slack_api import SlackClient
    from lib.store import BridgeStore

    world = _core(root, "slack-bridge", "slack", "T1", "", host=host)
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


def _telegram(root, *, host=CAPABLE_HOST):
    import asyncio
    import json as _json

    import httpx

    from telegram_bot.api import TelegramClient
    from telegram_bot.events import parse_telegram_update
    from telegram_bot.host import PresenceHostClient, PresenceHostHTTPError
    from telegram_bot.runtime import TelegramTransportRuntime
    from telegram_bot.tools import make_operation_tool
    from types import SimpleNamespace

    world = _core(root, "telegram-bot", "telegram", "9", "", host=host)
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


def _frozen(root, *, host=CAPABLE_HOST):
    import asyncio

    import httpx

    from telegram_bot.host import PresenceHostClient, PresenceHostHTTPError
    from telegram_bot.runtime import TelegramTransportRuntime

    world = _core(root, "telegram-bot", "telegram", "9", "", host=host)

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
            "actions": actions, "core_identity": world.core_identity, "identity": world.identity}


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


# Telegram's answers behind the real client: getMe per token, then these messages for bot 9.
_BOTS = {"token-a": {"id": 9, "is_bot": True, "username": "presence_bot"},
         "token-b": {"id": 77, "is_bot": True, "username": "other_bot"}}
_GROUP = {"id": -200, "type": "supergroup", "title": "Plain"}
_FORUM = {"id": -100, "type": "supergroup", "title": "Forum", "is_forum": True}
_DM_TOPICS = {"id": 55, "type": "private", "first_name": "Ann"}
_DM_PLAIN = {"id": 56, "type": "private", "first_name": "Bob"}


def _message(update_id, chat, message_id, topic=None):
    return {"update_id": update_id, "message": {
        "message_id": message_id, "date": 1, "chat": chat, "from": {"id": 55, "is_bot": False, "first_name": "Ann"},
        "text": "words", **({"message_thread_id": topic, "is_topic_message": True} if topic else {})}}


_UPDATES = [_message(1, _GROUP, 7), _message(2, _FORUM, 7, topic=5), _message(3, _FORUM, 8), _message(4, _DM_TOPICS, 3, topic=9),
            _message(5, _DM_PLAIN, 4), {"update_id": 6, "my_chat_member": {
                "chat": _GROUP, "from": {"id": 55, "is_bot": False}, "date": 1,
                "old_chat_member": {"status": "left", "user": {"id": 9, "is_bot": True}},
                "new_chat_member": {"status": "member", "user": {"id": 9, "is_bot": True}}}}]
_WORKER_CALLS = {
    "plain": ("-200", 7, None), "plain_wrong": ("-200", 7, 3), "plain_unretained": ("-200", 999, None),
    "unobserved": ("-300", 2, None),
    "forum_right": ("-100", 7, 5), "forum_wrong": ("-100", 7, 6), "forum_missing": ("-100", 7, None),
    "forum_general": ("-100", 8, None), "forum_unretained": ("-100", 999, None),
    "private_topic_right": ("55", 3, 9), "private_topic_missing": ("55", 3, None), "private_plain": ("56", 4, None),
}


def _scripted_network(requests, *, bots, updates=()):
    """Everything the skill's urllib clients would send; ``requests`` lists what they asked for."""
    import io
    import time
    import urllib.error
    import urllib.request

    def urlopen(request, timeout=None):
        url = request.full_url
        requests.append(url)
        if "/bot" in url:
            token, method = url.split("/bot", 1)[1].rsplit("/", 1)
            bot = bots.get(token)
            if bot is None:
                body = json.dumps({"ok": False, "error_code": 401, "description": "Unauthorized"}).encode()
                raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(body))
            if method == "getMe":
                result = bot
            elif method == "getUpdates":
                offset = json.loads(request.data)["offset"]
                time.sleep(0.02)
                result = [update for update in updates if update["update_id"] >= offset]
            else:
                result = True
            return io.BytesIO(json.dumps({"ok": True, "result": result}).encode())
        if url.endswith("/identity"):
            return io.BytesIO(json.dumps({"presence_delivery_version": 1, "places_version": 1}).encode())
        body = json.dumps({"ok": False, "error": "Connect this place in Consciousness > Places.",
                           "code": "place_not_connected", "field": "place", "disposition": "blocked"}).encode()
        raise urllib.error.HTTPError(url, 409, "Conflict", {}, io.BytesIO(body))
    urllib.request.urlopen = urlopen


def _load_skill(root, token):
    """This Hub's skill, reviewed, enabled and granted as a real install is, loaded by core's own reload_all."""
    import shutil

    from ouroboros import extension_loader
    from ouroboros.config import load_settings
    from ouroboros.skill_loader import SkillReviewState, find_skill, save_enabled, save_review_state, save_skill_grants

    os.environ["OUROBOROS_RUNTIME_MODE"] = "advanced"
    os.environ["OUROBOROS_HOST_SERVICE_PORT"] = "1"  # never the live Host's port, whatever the fakes miss
    data, skills = root / "data", root / "skills"
    Path(os.environ["OUROBOROS_SETTINGS_PATH"]).write_text(json.dumps({"TELEGRAM_PUBLIC_BOT_TOKEN": token}), encoding="utf-8")
    if not skills.exists():
        shutil.copytree(ROOT / "skills" / "telegram-bot", skills / "telegram-bot",
                        ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc", ".DS_Store"))
        loaded = find_skill(data, "telegram-bot", repo_path=str(skills))
        save_enabled(data, "telegram-bot", True)
        save_review_state(data, "telegram-bot", SkillReviewState(status="pass", content_hash=loaded.content_hash))
        save_skill_grants(data, "telegram-bot", ["TELEGRAM_PUBLIC_BOT_TOKEN"], content_hash=loaded.content_hash,
                          requested_keys=["TELEGRAM_PUBLIC_BOT_TOKEN"], granted_permissions=["presence"],
                          requested_permissions=["presence"])
    errors = extension_loader.reload_all(data, load_settings, repo_path=str(skills))
    assert errors.get("telegram-bot") is None, errors
    return next(module for module in list(sys.modules.values()) if hasattr(module, "_make_telegram_send"))


def _server_phase(root, token, revoked):
    """The server process: core starts the skill's supervised task, which polls until the updates are committed."""
    import asyncio
    import threading
    import time

    from ouroboros import extension_loader
    from ouroboros.event_bus import get_global_event_bus
    from ouroboros.extension_companion import init_server_process_pid

    requests = []
    # A revoked token is the same string Telegram no longer answers for.
    bots = {} if revoked else _BOTS
    _scripted_network(requests, bots=bots, updates=_UPDATES if token == "token-a" else ())
    init_server_process_pid()
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    get_global_event_bus().set_loop(loop)
    plugin = _load_skill(root, token)
    runtime = plugin._RUNTIME

    def settled():
        if token not in bots:
            return runtime.status_snapshot()["runtime_state"] == "provider_error"
        return runtime.confirmed_bot()[0] and (token != "token-a" or runtime.store.telegram_offset() > len(_UPDATES))

    deadline = time.monotonic() + 60
    while not settled():
        assert time.monotonic() < deadline, requests
        time.sleep(0.02)
    facts = {"run_task": runtime._run_task is not None, "confirmed": list(runtime.confirmed_bot()),
             "offset": runtime.store.telegram_offset()}
    extension_loader.unload_extension("telegram-bot")
    return facts


def _worker_phase(root, token):
    """A pooled worker: core's real reload_all, no supervised task, and each call through the real dispatch."""
    import ast
    import sqlite3
    from types import SimpleNamespace

    from ouroboros import extension_loader, place_guard, places as p, safety
    from ouroboros.extension_companion import is_server_process
    from ouroboros.extension_surface_names import extension_surface_name
    from ouroboros.tools.extension_dispatch import _dispatch_extension_tool_result

    requests = []
    _scripted_network(requests, bots={})
    safety.check_safety = lambda *_args, **_kwargs: (True, "")
    data = root / "data"
    plugin = _load_skill(root, token)
    runtime = plugin._RUNTIME
    connected = {place.conversation_id for place in p.load_places(data)}
    for chat in {call[0] for call in _WORKER_CALLS.values()} - connected:
        for account in ("9", "77"):
            place = p.connect_place(data, connector="telegram-bot", provider="telegram", account_id=account, conversation_id=chat,
                                    wakes=["any_message"])
            p.edit_place(data, place.place_id, "set_state", author="owner", state="active")
    name = extension_surface_name("telegram-bot", "telegram_operation")
    ext_tool = extension_loader.get_tool(name)
    ctx = SimpleNamespace(drive_root=data, task_id="pooled-task", task_metadata={}, messages=None)
    guards, answers, dispatched, targets, topics = {}, {}, {}, {}, {}
    for label, (chat, message, topic) in _WORKER_CALLS.items():
        request_id = f"{label}-{os.getpid()}"
        args = {"method": "setMessageReaction", "chat_id": chat, "message_id": message, "request_id": request_id,
                "reaction": [{"type": "emoji", "emoji": "👍"}], **({"topic_id": topic} if topic else {})}
        guards[label] = place_guard.dispatch_refusal(ctx, name, args, ext_tool=ext_tool)
        result = _dispatch_extension_tool_result(ctx, name, ext_tool, args)
        dispatched[label], answers[label] = result.meta.get("physical_dispatch"), ast.literal_eval(result.text)
        with sqlite3.connect(runtime.store.path) as db:
            row = db.execute("SELECT payload_json FROM outbox WHERE delivery_id=?", (f"telegram-operation:{request_id}",)).fetchone()
        if row:
            payload = json.loads(row[0])
            targets[label], topics[label] = payload["_reaction_target"]["resolution"], payload.get("topic_id")
    return {"server_process": is_server_process(), "run_task": runtime._run_task is not None,
            "live": extension_loader.is_extension_live("telegram-bot", data, repo_path=str(root / "skills")),
            "confirmed": list(runtime.confirmed_bot()), "status": plugin._status_payload(), "guards": guards,
            "answers": answers, "dispatched": dispatched, "targets": targets, "queued_topics": topics, "requests": requests}


def _drive(root: Path, mode: str) -> dict:
    if mode.startswith("server:"):
        _phase, token, *revoked = mode.split(":")
        return _server_phase(root, token, bool(revoked))
    if mode.startswith("worker:"):
        return _worker_phase(root, mode.split(":")[1])
    if mode == "reads":
        return _reads(root)
    if mode == "telegram-known":
        return _telegram_known(root)
    if mode == "slack-padded":
        return _slack_padded(root)
    if mode == "frozen":
        return _frozen(root)
    if mode == "frozen-held":
        return _frozen(root, host=OLD_HOST)
    if mode == "frozen-explicit":
        return _frozen(root, host=EXPLICIT_ONLY_HOST)
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
    host = OLD_HOST if mode == "old-host" else EXPLICIT_ONLY_HOST
    slack_world, slack_store, _ = _slack(root / "s" if (root / "s").mkdir() is None else root, host=host)
    telegram_world, runtime = _telegram(root / "t" if (root / "t").mkdir() is None else root, host=host)
    return {
        "core_identity": telegram_world.core_identity, "identity": telegram_world.identity,
        "slack_turns": slack_world.turns, "telegram_turns": telegram_world.turns,
        "slack_withheld": slack_store.status()["reactions_withheld"],
        "telegram_withheld": runtime.store.status_snapshot()["reactions_withheld"],
        "reports": slack_world.reports + telegram_world.reports,
        "recorded": slack_world.recorded + telegram_world.recorded,
        "actions": _readback(slack_world)[2] + _readback(telegram_world)[2],
        "slack_applied": slack_store.delivery_receipt("react-ok")["parts"][0]["provider_result"]["effect"],
        "telegram_applied": runtime.store.delivery_receipt("telegram-operation:set")["provider_receipt"]["effect"],
    }


def _place(data, provider):
    from ouroboros import places as p
    return next(place.place_id for place in p.load_places(data) if place.provider == provider)


if __name__ == "__main__":
    print(json.dumps(_drive(Path(sys.argv[1]), sys.argv[2]), ensure_ascii=False, default=str))
