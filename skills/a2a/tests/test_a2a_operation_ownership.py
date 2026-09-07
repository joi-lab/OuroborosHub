"""Current-message races and real SDK durable recovery (SDK optional in CI)."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import threading
import uuid

import httpx
import pytest
from starlette.testclient import TestClient


def load_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("A2A_TOOLS_REFRESHER", "0")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200, json=(
        {"name": "fixture", "description": "fixture"} if url.endswith("/identity")
        else {"tools": [{"function": {"name": "read_file", "description": "read"}}]} if url.endswith("/tools/schemas")
        else {"ok": True, "status": "pending", "operation_ref": url.rsplit("/", 1)[-1]}
    ), request=httpx.Request("GET", url)))
    path = Path(__file__).resolve().parents[1] / "scripts/a2a_daemon.py"
    spec = importlib.util.spec_from_file_location("a2a_owned_" + uuid.uuid4().hex, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_atomic_first_binding_and_old_message_cannot_reset_current(tmp_path, monkeypatch):
    daemon = load_daemon(tmp_path, monkeypatch)
    barrier = threading.Barrier(2)
    sequence = iter([-101, -102])

    def allocate():
        value = next(sequence)
        barrier.wait(timeout=3)
        return value

    monkeypatch.setattr(daemon, "_allocate_chat_id_sync", allocate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        bindings = list(pool.map(lambda _: daemon._bind_inbound_message_sync("task", "ctx", "one"), range(2)))
    assert bindings[0] == bindings[1]
    first = bindings[0][1]
    daemon._update_task_record("task", state="completed", expected_message_id=first)
    _, second = daemon._bind_inbound_message_sync("task", "ctx", "two")
    assert second != first and daemon._load_task("task")["status"]["state"] == "working"
    daemon._bind_inbound_message_sync("task", "ctx", "one")
    daemon._update_task_record("task", state="completed", expected_message_id=first)
    current = daemon._load_task("task")
    assert current["ouroboros"]["client_message_id"] == second
    assert current["status"]["state"] == "working"
    assert daemon._client_message_id("a:b", "c") != daemon._client_message_id("a", "b:c")
    assert len(daemon._client_message_id("long" * 100, "id" * 100)) <= 128


def test_new_message_cancel_uses_current_binding_and_canceled_replay_stays_canceled(tmp_path, monkeypatch):
    daemon = load_daemon(tmp_path, monkeypatch)
    daemon._A2A_SDK_AVAILABLE = False
    monkeypatch.setattr(daemon, "_allocate_chat_id_sync", lambda: -11)
    _, first = daemon._bind_inbound_message_sync("task", "ctx", "one")
    daemon._update_task_record("task", state="completed", expected_message_id=first)
    _, second = daemon._bind_inbound_message_sync("task", "ctx", "two")
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        return httpx.Response(200, json={"ok": True, "outcome": "cancelled", "status": "cancelled",
                                        "response": "", "operation_ref": f"-11:{second}"},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(daemon.httpx, "post", post)
    with TestClient(daemon._build_app()) as client:
        canceled = client.post("/", json={"jsonrpc": "2.0", "id": "c", "method": "tasks/cancel", "params": {"id": "task"}}).json()
        assert canceled["result"]["status"]["state"] == "canceled"
        assert calls[0]["operation_ref"] == f"-11:{second}"
        replay = client.post("/", json={"jsonrpc": "2.0", "id": "m", "method": "message/send", "params": {
            "message": {"taskId": "task", "contextId": "ctx", "messageId": "two", "parts": [{"kind": "text", "text": "hi"}]},
        }}).json()
        assert replay["result"]["status"]["state"] == "canceled"
        monkeypatch.setattr(daemon.httpx, "post", lambda url, **kwargs: httpx.Response(
            409, json={"ok": False, "error": "different message"}, request=httpx.Request("POST", url)))
        conflict = client.post("/", json={"jsonrpc": "2.0", "id": "bad", "method": "message/send", "params": {
            "message": {"taskId": "task", "contextId": "ctx", "messageId": "two", "parts": [{"kind": "text", "text": "changed"}]},
        }}).json()
        assert conflict["error"]["code"] == -32602
        assert daemon._load_task("task")["status"]["state"] == "canceled"


def test_real_sdk_handler_reads_and_cancels_after_cold_app_restart(tmp_path, monkeypatch):
    pytest.importorskip("a2a", reason="real SDK is installed in the isolated acceptance environment")
    from a2a.server.context import ServerCallContext
    from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus

    first = load_daemon(tmp_path, monkeypatch)
    assert first._A2A_SDK_AVAILABLE, "the real SDK imports must succeed"
    monkeypatch.setattr(first, "_allocate_chat_id_sync", lambda: -91)
    _, message_id = first._bind_inbound_message_sync("sdk-task", "ctx", "message-one")
    task = Task(id="sdk-task", context_id="ctx", status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                history=[Message(message_id="message-one", role=Role.ROLE_USER, parts=[Part(text="hello")])])
    asyncio.run(first._sdk_task_store().save(task, ServerCallContext()))
    # A fresh module and fresh SDK handler have no in-memory task objects.
    restarted = load_daemon(tmp_path, monkeypatch)
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        return httpx.Response(200, json={"ok": True, "outcome": "cancelled", "status": "cancelled"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(restarted.httpx, "post", post)
    with TestClient(restarted.app) as client:
        before = client.post("/", json={"jsonrpc": "2.0", "id": "g", "method": "tasks/get", "params": {"id": "sdk-task"}}).json()
        assert before["result"]["id"] == "sdk-task" and before["result"]["status"]["state"] == "working"
        canceled = client.post("/", json={"jsonrpc": "2.0", "id": "c", "method": "tasks/cancel", "params": {"id": "sdk-task"}}).json()
        assert canceled["result"]["status"]["state"] == "canceled", canceled
        assert calls == [{"operation_ref": f"-91:{message_id}", "reason": "A2A tasks/cancel for sdk-task"}]
    third = load_daemon(tmp_path, monkeypatch)
    with TestClient(third.app) as client:
        after = client.post("/", json={"jsonrpc": "2.0", "id": "g", "method": "tasks/get", "params": {"id": "sdk-task"}}).json()
        assert after["result"]["status"]["state"] == "canceled"


def test_real_sdk_send_persists_a_completed_answer_for_restart(tmp_path, monkeypatch):
    pytest.importorskip("a2a")
    first = load_daemon(tmp_path, monkeypatch)
    assert first._A2A_SDK_AVAILABLE
    monkeypatch.setattr(first, "_allocate_chat_id_sync", lambda: -92)
    monkeypatch.setattr(first, "_dispatch_after_allocate_sync", lambda *a: "real SDK answer")
    with TestClient(first.app) as client:
        sent = client.post("/", json={"jsonrpc": "2.0", "id": "s", "method": "message/send", "params": {
            "message": {"role": "user", "messageId": "message-one", "parts": [{"kind": "text", "text": "hello"}]},
        }}).json()
        assert sent["result"]["status"]["state"] == "completed", sent
        task_id = sent["result"]["id"]
    restarted = load_daemon(tmp_path, monkeypatch)
    with TestClient(restarted.app) as client:
        got = client.post("/", json={"jsonrpc": "2.0", "id": "g", "method": "tasks/get", "params": {"id": task_id}}).json()
        assert got["result"]["status"]["state"] == "completed", got
        assert got["result"]["artifacts"][0]["parts"][0]["text"] == "real SDK answer"
        # A named replay reaches the existing operation owner. The SDK's
        # terminal-task guard still rejects a NEW message on this taskId.
        dispatched = []
        monkeypatch.setattr(restarted, "_dispatch_after_allocate_sync", lambda *args: dispatched.append(args) or "unexpected")
        for message_id, text in [("message-one", "hello"), ("message-two", "next"), ("message-one", "changed")]:
            reply = client.post("/", json={"jsonrpc": "2.0", "id": "r", "method": "message/send", "params": {
                "message": {"role": "user", "taskId": task_id, "messageId": message_id, "parts": [{"kind": "text", "text": text}]},
            }}).json()
            if message_id == "message-one" and text == "hello":
                assert reply["result"]["status"]["state"] == "completed", reply
            else:
                assert "error" in reply, reply
        current = restarted._load_task(task_id)
        assert current["ouroboros"]["client_message_id"] == restarted._client_message_id(task_id, "message-one")
        assert current["status"]["state"] == "completed"
        assert current["artifacts"][0]["parts"][0]["text"] == "real SDK answer"
        stream = client.post("/", json={"jsonrpc": "2.0", "id": "s", "method": "message/stream", "params": {
            "message": {"role": "user", "taskId": task_id, "messageId": "message-one", "parts": [{"kind": "text", "text": "hello"}]},
        }})
        assert "completed" in stream.text and "working" not in stream.text
        assert dispatched == [], "a verified terminal replay returns its Task without starting work"


def test_real_sdk_cold_replay_keeps_blocking_choice_and_unknown_outcome(tmp_path, monkeypatch):
    pytest.importorskip("a2a")
    from a2a.server.context import ServerCallContext
    from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus

    daemon = load_daemon(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon, "_allocate_chat_id_sync", lambda: -93)
    daemon._bind_inbound_message_sync("cold", "ctx", "m")
    asyncio.run(daemon._sdk_task_store().save(Task(
        id="cold", context_id="ctx", status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
        history=[Message(message_id="m", role=Role.ROLE_USER, parts=[Part(text="hello")])],
    ), ServerCallContext()))
    waits = []

    def expire(chat_id, deadline, operation_ref):
        waits.append(operation_ref)
        raise daemon._HostWaitExpired("host outcome remains unknown; work may still be running", operation_ref)

    monkeypatch.setattr(daemon, "_wait_final_after_expiry_sync", expire)
    body = {"jsonrpc": "2.0", "id": "r", "method": "message/send", "params": {
        "message": {"role": "user", "taskId": "cold", "messageId": "m", "parts": [{"kind": "text", "text": "hello"}]},
        "configuration": {"blocking": False},
    }}
    with TestClient(daemon.app) as client:
        immediate = client.post("/", json=body).json()
        assert immediate["result"]["status"]["state"] == "working" and waits == []
        body["params"]["configuration"]["blocking"] = True
        waited = client.post("/", json=body).json()
        assert waited["result"]["status"]["state"] == "working", waited
        assert "unknown" in waited["result"]["status"]["message"]["parts"][0]["text"]
        assert len(waits) == 1
    assert daemon._load_task("cold")["status"]["state"] == "working"


def test_sdk_source_survives_binding_before_the_next_status_event(tmp_path, monkeypatch):
    pytest.importorskip("a2a")
    from a2a.server.context import ServerCallContext
    from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus

    daemon = load_daemon(tmp_path, monkeypatch)
    asyncio.run(daemon._sdk_task_store().save(Task(
        id="accepting", context_id="ctx", status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
        history=[Message(message_id="m", role=Role.ROLE_USER, parts=[Part(text="hello")])],
    ), ServerCallContext()))
    monkeypatch.setattr(daemon, "_allocate_chat_id_sync", lambda: -94)
    daemon._bind_inbound_message_sync("accepting", "ctx", "m")
    assert daemon._load_task("accepting")["sdk_task"]["history"][0]["messageId"] == "m"
    dispatched = []
    monkeypatch.setattr(daemon, "_dispatch_after_allocate_sync", lambda *a: dispatched.append(a))
    with TestClient(daemon.app) as client:
        result = client.post("/", json={"jsonrpc": "2.0", "id": "r", "method": "message/send", "params": {
            "configuration": {"blocking": False},
            "message": {"role": "user", "taskId": "accepting", "messageId": "m", "parts": [{"kind": "text", "text": "hello"}]},
        }}).json()
        assert result["result"]["status"]["state"] == "working" and dispatched == []


@pytest.mark.parametrize("status", [200, 504])
def test_host_cannot_substitute_a_different_operation_ref(tmp_path, monkeypatch, status):
    daemon = load_daemon(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon.httpx, "post", lambda url, **kw: httpx.Response(
        status, json={"ok": True, "status": "completed", "response": "other answer", "operation_ref": "-7:other"},
        request=httpx.Request("POST", url)))
    with pytest.raises(RuntimeError, match="different message"):
        daemon._inject_sync(-7, "hello", "mine")


@pytest.mark.parametrize("sdk", [False, True])
def test_initial_wait_expiry_keeps_host_work_nonterminal_and_cancelable(tmp_path, monkeypatch, sdk):
    if sdk:
        pytest.importorskip("a2a")
    daemon = load_daemon(tmp_path, monkeypatch)
    if not sdk:
        daemon._A2A_SDK_AVAILABLE = False
    monkeypatch.setattr(daemon, "_allocate_chat_id_sync", lambda: -95)

    def expire(chat_id, text, deadline, client_message_id):
        raise daemon._HostWaitExpired("host task may still be running", f"{chat_id}:{client_message_id}")

    monkeypatch.setattr(daemon, "_dispatch_after_allocate_sync", expire)
    monkeypatch.setattr(daemon.httpx, "post", lambda url, **kw: httpx.Response(
        200, json={"ok": True, "outcome": "cancelled", "status": "cancelled"}, request=httpx.Request("POST", url)))
    with TestClient(daemon._build_app()) as client:
        result = client.post("/", json={"jsonrpc": "2.0", "id": "s", "method": "message/send", "params": {
            "message": {"role": "user", "messageId": "m", "parts": [{"kind": "text", "text": "hello"}]},
        }}).json()
        assert result["result"]["status"]["state"] == "working", result
        canceled = client.post("/", json={"jsonrpc": "2.0", "id": "c", "method": "tasks/cancel",
                                        "params": {"id": result["result"]["id"]}}).json()
        assert canceled["result"]["status"]["state"] == "canceled", canceled


@pytest.mark.parametrize("method", ["tasks/get", "message/send", "tasks/cancel"])
def test_late_host_outcome_returns_the_current_message_record(tmp_path, monkeypatch, method):
    """A late M1 read/completion/cancel cannot attach its outcome to current M2."""
    daemon = load_daemon(tmp_path, monkeypatch)
    daemon._A2A_SDK_AVAILABLE = False
    monkeypatch.setattr(daemon, "_allocate_chat_id_sync", lambda: -51)
    _, first = daemon._bind_inbound_message_sync("task", "ctx", "m1")
    started, release = threading.Event(), threading.Event()
    host_calls = []

    def host_reply(url, **kwargs):
        host_calls.append(url)
        if method == "tasks/get":
            assert url.endswith(first)
        elif method == "message/send":
            assert kwargs["json"]["client_message_id"] == first
        else:
            assert kwargs["json"]["operation_ref"] == f"-51:{first}"
        started.set()
        assert release.wait(5)
        return httpx.Response(200, json={
            "ok": True, "operation_ref": f"-51:{first}", "status": "completed",
            "text": "answer to M1", "response": "answer to M1", "outcome": "cancelled",
        }, request=httpx.Request("GET" if method == "tasks/get" else "POST", url))

    monkeypatch.setattr(daemon.httpx, "get" if method == "tasks/get" else "post", host_reply)
    params = {"message": {"taskId": "task", "contextId": "ctx", "messageId": "m1",
                          "parts": [{"kind": "text", "text": "request M1"}]}} if method == "message/send" else {"id": "task"}
    with TestClient(daemon._build_app()) as client, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, "/", json={
            "jsonrpc": "2.0", "id": "race", "method": method, "params": params,
        })
        try:
            assert started.wait(5)
            _, second = daemon._bind_inbound_message_sync("task", "ctx", "m2")
            current = daemon._load_task("task")
            current_bytes = daemon._task_path("task").read_bytes()
        finally:
            release.set()
        response = pending.result(timeout=5).json()
        if method == "tasks/cancel":
            assert response["error"]["code"] == -32002 and "result" not in response
            assert response["error"]["message"] == (
                "The previous host operation was cancelled, but the task now belongs to another message; "
                "the current operation was not cancelled."
            )
        else:
            assert response["result"] == current
        assert current == daemon._load_task("task")
        assert daemon._task_path("task").read_bytes() == current_bytes
        assert current["ouroboros"]["client_message_id"] == second
        assert current["status"]["state"] == "working" and "artifacts" not in current
        assert len(host_calls) == 1
        if method == "tasks/cancel":
            # Refusing the stale cancellation leaves M2 able to finish normally.
            def complete_current(url, **kwargs):
                assert url.endswith("/chat/inject")
                assert kwargs["json"]["client_message_id"] == second
                return httpx.Response(200, json={"ok": True, "status": "completed",
                    "operation_ref": f"-51:{second}", "response": "answer to M2"}, request=httpx.Request("POST", url))
            monkeypatch.setattr(daemon.httpx, "post", complete_current)
            completed = client.post("/", json={"jsonrpc": "2.0", "id": "current", "method": "message/send", "params": {
                "message": {"taskId": "task", "contextId": "ctx", "messageId": "m2",
                            "parts": [{"kind": "text", "text": "request M2"}]},
            }}).json()["result"]
            assert completed["status"]["state"] == "completed"
            assert completed["artifacts"][0]["parts"][0]["text"] == "answer to M2"


@pytest.mark.parametrize("cancel_race", [False, True])
def test_real_sdk_late_producer_preserves_current_message_completion(tmp_path, monkeypatch, caplog, cancel_race):
    """M1 may finish after M2 starts; only M2 owns current artifacts and cleanup."""
    pytest.importorskip("a2a")
    daemon = load_daemon(tmp_path, monkeypatch)
    assert daemon._A2A_SDK_AVAILABLE
    monkeypatch.setattr(daemon, "_allocate_chat_id_sync", lambda: -81)
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    finished = [threading.Event(), threading.Event()]
    cancel_started, cancel_release = threading.Event(), threading.Event()
    artifacts = []
    cancel_refusal = (
        "The previous host operation was cancelled, but the task now belongs to another message; "
        "the current operation was not cancelled."
    )

    def dispatch(chat_id, text, deadline, client_message_id):
        index = 0 if text == "one" else 1
        entered[index].set()
        assert release[index].wait(10)
        return "answer to " + text

    monkeypatch.setattr(daemon, "_dispatch_after_allocate_sync", dispatch)
    def cancel_host(url, **kwargs):
        assert url.endswith("/chat/cancel")
        assert kwargs["json"]["operation_ref"] == f"-81:{daemon._client_message_id(first['id'], 'm1')}"
        cancel_started.set()
        assert cancel_release.wait(10)
        return httpx.Response(200, json={"ok": True, "outcome": "cancelled"},
                              request=httpx.Request("POST", url))

    if cancel_race:
        monkeypatch.setattr(daemon.httpx, "post", cancel_host)
    execute = daemon.OuroborosExecutor.execute
    add_artifact = daemon.TaskUpdater.add_artifact

    async def observed_execute(self, context, event_queue):
        try:
            return await execute(self, context, event_queue)
        finally:
            finished[0 if context.message.message_id == "m1" else 1].set()

    async def observed_artifact(self, parts, **kwargs):
        artifacts.append([part.text for part in parts])
        return await add_artifact(self, parts, **kwargs)

    monkeypatch.setattr(daemon.OuroborosExecutor, "execute", observed_execute)
    monkeypatch.setattr(daemon.TaskUpdater, "add_artifact", observed_artifact)

    def send(client, message_id, text, task_id="", context_id=""):
        message = {"role": "user", "messageId": message_id, "parts": [{"kind": "text", "text": text}]}
        if task_id:
            message.update(taskId=task_id, contextId=context_id)
        return client.post("/", json={"jsonrpc": "2.0", "id": message_id, "method": "message/send",
            "params": {"message": message, "configuration": {"blocking": message_id == "m2"}}}).json()

    with TestClient(daemon.app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        try:
            first = send(client, "m1", "one")["result"]
            assert entered[0].wait(5)
            if cancel_race:
                cancellation = pool.submit(client.post, "/", json={
                    "jsonrpc": "2.0", "id": "cancel", "method": "tasks/cancel", "params": {"id": first["id"]},
                })
                assert cancel_started.wait(5)
            second = pool.submit(send, client, "m2", "two", first["id"], first["contextId"])
            assert entered[1].wait(5)
            if cancel_race:
                cancel_release.set()
                refusal = cancellation.result(timeout=5).json()
                assert "error" in refusal and "result" not in refusal
                assert refusal["error"]["message"] == cancel_refusal
                assert refusal["error"]["code"] == -32603  # SDK 1.1.2 v0.3 mapping; not -32002.
            release[0].set()
            assert finished[0].wait(5)
            assert artifacts == []
            assert not second.done()
            release[1].set()
            result = second.result(timeout=5)["result"]
            assert result["status"]["state"] == "completed"
            assert result["artifacts"][0]["parts"][0]["text"] == "answer to two"
            assert artifacts == [["answer to two"]]
            assert daemon._load_task(first["id"])["artifacts"][0]["parts"][0]["text"] == "answer to two"
        finally:
            cancel_release.set()
            for event in release:
                event.set()
            for event in finished:
                event.wait(5)
    # SDK 1.1.2's v0.3 adapter logs the expected typed refusal as an exception.
    exceptions = [row.exc_info[1] for row in caplog.records if row.exc_info]
    assert len(exceptions) <= int(cancel_race)
    assert all(type(exc) is daemon.TaskNotCancelableError and str(exc) == cancel_refusal for exc in exceptions)
    assert not [row.getMessage() for row in caplog.records
                if "will not be enqueued" in row.getMessage() or "Event dropped" in row.getMessage()
                or "NoTaskQueue" in row.getMessage()]


def _stored_sdk_tasks(tmp_path, monkeypatch):
    pytest.importorskip("a2a", reason="actual SDK controls use the isolated acceptance environment")
    from a2a.server.context import ServerCallContext
    from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus

    daemon = load_daemon(tmp_path, monkeypatch)
    assert daemon._A2A_SDK_AVAILABLE
    store, context, refs = daemon._sdk_task_store(), ServerCallContext(), {}
    for index in range(2):
        task_id = f"stored-{index}"
        client_id = daemon._client_message_id(task_id, "m")
        refs[task_id] = daemon._operation_ref(-61 - index, client_id)
        daemon._update_task_record(task_id, "ctx", binding={
            "chat_id": -61 - index, "client_message_id": client_id, "operation_ref": refs[task_id],
        })
        asyncio.run(store.save(Task(id=task_id, context_id="ctx", status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            history=[Message(message_id="m", role=Role.ROLE_USER, parts=[Part(text="request")])]), context))
    return daemon, store, context, refs


def test_sdk_list_reads_local_snapshots_off_loop_and_get_refreshes_one_task(tmp_path, monkeypatch):
    daemon, store, context, refs = _stored_sdk_tasks(tmp_path, monkeypatch)
    from a2a.types import ListTasksRequest, TaskState

    paths = list(daemon._tasks_dir().glob("*.json"))
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    requests, read_threads = [], []
    read_text = Path.read_text

    def observe_read(path, *args, **kwargs):
        if path in before:
            read_threads.append(threading.get_ident())
        return read_text(path, *args, **kwargs)

    def host_get(url, **kwargs):
        requests.append(url)
        assert url.endswith(refs["stored-0"])
        return httpx.Response(200, json={"ok": True, "operation_ref": refs["stored-0"],
            "status": "completed", "text": "one refreshed answer"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(Path, "read_text", observe_read)
    monkeypatch.setattr(daemon.httpx, "get", host_get)

    async def exercise():
        loop_thread = threading.get_ident()
        first = await store.list(ListTasksRequest(page_size=1), context)
        assert first.total_size == 2 and len(first.tasks) == 1 and first.next_page_token
        assert len(read_threads) == 2 and all(t != loop_thread for t in read_threads)
        second = await store.list(ListTasksRequest(page_size=1, page_token=first.next_page_token), context)
        assert {first.tasks[0].id, second.tasks[0].id} == set(refs)
        assert all(task.status.state == TaskState.TASK_STATE_WORKING for task in [*first.tasks, *second.tasks])
        assert requests == []
        assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths} == before
        refreshed = await store.get("stored-0", context)
        assert refreshed.status.state == TaskState.TASK_STATE_COMPLETED
        assert refreshed.artifacts[0].parts[0].text == "one refreshed answer"
        assert requests == [daemon.HOST_SERVICE_URL + "/chat/operations/" + refs["stored-0"]]

    asyncio.run(exercise())
    assert daemon._load_task("stored-0")["status"]["state"] == "completed"
    assert daemon._task_path("stored-1").read_bytes() == before[daemon._task_path("stored-1")][0]


def test_sdk_local_list_keeps_owner_filter_without_remote_reads(tmp_path, monkeypatch):
    daemon, store, context, _ = _stored_sdk_tasks(tmp_path, monkeypatch)
    from a2a.types import ListTasksRequest

    foreign = daemon._load_task("stored-1")
    foreign["sdk_owner"] = "another-owner"
    daemon._save_task(foreign)
    requests = []
    monkeypatch.setattr(daemon.httpx, "get", lambda *a, **kw: requests.append(a) or None)
    listed = asyncio.run(store.list(ListTasksRequest(), context))
    assert [task.id for task in listed.tasks] == ["stored-0"] and listed.total_size == 1
    assert requests == []
    assert daemon._load_task("stored-1") == foreign


@pytest.mark.parametrize("raw", ["{broken json", "[]", '{"id":"other"}'])
def test_sdk_list_and_get_report_corrupt_state_without_dropping_records(tmp_path, monkeypatch, raw):
    daemon, store, context, _ = _stored_sdk_tasks(tmp_path, monkeypatch)
    from a2a.types import ListTasksRequest

    path = daemon._task_path("bad")
    path.write_text(raw, encoding="utf-8")
    before = {p: p.read_bytes() for p in daemon._tasks_dir().glob("*.json")}
    assert daemon._load_task("missing") is None
    for call in [lambda: daemon._load_task("bad"), lambda: asyncio.run(store.get("bad", context)),
                 lambda: asyncio.run(store.list(ListTasksRequest(), context))]:
        with pytest.raises(daemon._TaskStateCorrupt):
            call()
    requests = []
    monkeypatch.setattr(daemon.httpx, "get", lambda *a, **kw: requests.append(a) or None)
    with TestClient(daemon.app, headers={"A2A-Version": "1.0"}) as client:
        for method, params in [("ListTasks", {}), ("GetTask", {"id": "bad"})]:
            response = client.post("/", json={"jsonrpc": "2.0", "id": method, "method": method, "params": params}).json()
            assert response["id"] == method and response["error"]["code"] == -32603
            assert "task state" in response["error"]["message"] and "result" not in response
    assert requests == []
    assert {p: p.read_bytes() for p in daemon._tasks_dir().glob("*.json")} == before
