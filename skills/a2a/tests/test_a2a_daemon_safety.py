import asyncio
import importlib.util
import sys
import pytest
from pathlib import Path


def _load_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HOST_SERVICE_TOKEN", "token")
    # Keep the module-level app build from spawning background tool pollers
    # (they would outlive the test and poke whatever occupies the host port).
    monkeypatch.setenv("A2A_TOOLS_REFRESHER", "0")
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "a2a_daemon.py"
    spec = importlib.util.spec_from_file_location(f"a2a_daemon_test_{id(tmp_path)}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_dispatch_rejects_owner_slash_commands(tmp_path, monkeypatch):
    daemon = _load_daemon(tmp_path, monkeypatch)

    async def run():
        try:
            await daemon._dispatch_to_host("/panic")
        except ValueError as exc:
            assert "slash commands" in str(exc)
        else:
            raise AssertionError("slash command was not rejected")

    asyncio.run(run())


def test_dispatch_adds_transport_metadata_and_timeout(tmp_path, monkeypatch):
    daemon = _load_daemon(tmp_path, monkeypatch)
    calls = []

    class Response:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/chat/allocate-internal"):
            return Response({"chat_id": -123})
        return Response({"response": "ok"})

    monkeypatch.setattr(daemon.httpx, "post", fake_post)

    async def run():
        assert await daemon._dispatch_to_host("hello") == "ok"

    asyncio.run(run())

    inject_url, inject_kwargs = calls[-1]
    assert inject_url.endswith("/chat/inject")
    payload = inject_kwargs["json"]
    assert payload["timeout_sec"] == daemon.A2A_RESPONSE_TIMEOUT_SEC
    assert payload["transport"] == {
        "kind": "a2a",
        "conversation_id": "-123",
        "sender_label": "A2A",
    }


def test_dispatch_applies_backpressure(tmp_path, monkeypatch):
    daemon = _load_daemon(tmp_path, monkeypatch)

    async def run():
        daemon._A2A_SEMAPHORE = asyncio.Semaphore(1)
        await daemon._A2A_SEMAPHORE.acquire()
        try:
            try:
                await daemon._dispatch_to_host("hello")
            except RuntimeError as exc:
                assert "busy" in str(exc)
            else:
                raise AssertionError("busy dispatch was not rejected")
        finally:
            daemon._A2A_SEMAPHORE.release()

    asyncio.run(run())


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8767",
        "http://localhost:8767",
        "http://[::1]:8767",
    ],
)
def test_host_service_loopback_urls_are_allowed(tmp_path, monkeypatch, url):
    monkeypatch.setenv("HOST_SERVICE_URL", url)
    daemon = _load_daemon(tmp_path, monkeypatch)
    assert daemon._is_loopback(daemon._host_service_hostname(url)) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8767@evil.example",
        "http://evil.example:8767",
    ],
)
def test_host_service_url_rejects_non_loopback_and_userinfo(tmp_path, monkeypatch, url):
    monkeypatch.setenv("HOST_SERVICE_URL", url)
    with pytest.raises(RuntimeError):
        _load_daemon(tmp_path, monkeypatch)


def test_task_ids_that_sanitize_alike_do_not_share_a_record(tmp_path, monkeypatch):
    """The id -> filename mapping must be injective.

    Stripping non-alphanumerics mapped "job/a" and "joba" onto one state file, so
    one peer's task could overwrite or return another's record.
    """
    daemon = _load_daemon(tmp_path, monkeypatch)

    assert daemon._task_path("job/a") != daemon._task_path("joba")

    daemon._save_task({"id": "job/a", "status": {"state": "completed"}})
    daemon._save_task({"id": "joba", "status": {"state": "failed"}})

    first = daemon._load_task("job/a")
    second = daemon._load_task("joba")
    assert first["status"]["state"] == "completed"
    assert second["status"]["state"] == "failed"
    # The untouched original id survives inside the record.
    assert first["id"] == "job/a"
    assert second["id"] == "joba"


def test_empty_task_id_maps_deterministically(tmp_path, monkeypatch):
    """A random uuid fallback made _load_task("") unable to find its own write."""
    daemon = _load_daemon(tmp_path, monkeypatch)

    assert daemon._task_path("") == daemon._task_path("")
    daemon._save_task({"id": "", "status": {"state": "completed"}})
    assert daemon._load_task("")["status"]["state"] == "completed"


def test_corrupt_task_record_raises_typed_error(tmp_path, monkeypatch):
    """A present-but-unreadable record is not "task not found"."""
    daemon = _load_daemon(tmp_path, monkeypatch)

    path = daemon._task_path("broken")
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(daemon._TaskStateCorrupt):
        daemon._load_task("broken")

    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(daemon._TaskStateCorrupt):
        daemon._load_task("broken")


def test_malformed_port_setting_does_not_break_import(tmp_path, monkeypatch):
    """A bare int() at module scope crash-looped the companion before `app` existed."""
    monkeypatch.setenv("A2A_PORT", "not-a-port")
    daemon = _load_daemon(tmp_path, monkeypatch)
    assert daemon.A2A_PORT == 18800


def test_out_of_range_port_setting_is_clamped(tmp_path, monkeypatch):
    """An out-of-range port reached uvicorn and prevented startup."""
    monkeypatch.setenv("A2A_PORT", "99999")
    daemon = _load_daemon(tmp_path, monkeypatch)
    assert 1 <= daemon.A2A_PORT <= 65535


def test_non_dict_settings_document_falls_back_to_defaults(tmp_path, monkeypatch):
    """A wrong-shaped settings file must behave like an unreadable one, not like
    "never configured"."""
    (tmp_path / "settings.json").write_text("[1, 2, 3]", encoding="utf-8")
    daemon = _load_daemon(tmp_path, monkeypatch)
    assert daemon._SETTINGS == {}
    assert daemon.A2A_PORT == 18800


def test_response_timeout_is_clamped_below_the_stream_deadline(tmp_path, monkeypatch):
    """A response wait longer than the total deadline blocked for the full socket
    wait and then gave up immediately."""
    monkeypatch.setenv("A2A_RESPONSE_TIMEOUT_SEC", "1740")
    monkeypatch.setenv("A2A_STREAM_DEADLINE_SEC", "60")
    daemon = _load_daemon(tmp_path, monkeypatch)
    assert daemon.A2A_RESPONSE_TIMEOUT_SEC + 15 <= daemon.A2A_STREAM_DEADLINE_SEC


def test_inject_failure_envelope_is_not_an_empty_answer(tmp_path, monkeypatch):
    """HTTP 200 with {"ok": false} must surface the host error, not "" as success."""
    daemon = _load_daemon(tmp_path, monkeypatch)

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": False, "error": "host refused the injection"}

    monkeypatch.setattr(daemon.httpx, "post", lambda url, **kwargs: Response())
    with pytest.raises(RuntimeError) as excinfo:
        daemon._inject_sync(-123, "hello")
    assert "host refused the injection" in str(excinfo.value)


# --- v1.4.0: the host operation binding and honest cancellation (#667) --------


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_message_send_binds_the_host_operation_in_the_durable_record(tmp_path, monkeypatch):
    daemon = _load_daemon(tmp_path, monkeypatch)
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        if url.endswith("/chat/allocate-internal"):
            return _Resp(200, {"chat_id": -123})
        return _Resp(200, {"ok": True, "response": "ok", "operation_ref": f"-123:{kwargs['json']['client_message_id']}"})

    monkeypatch.setattr(daemon.httpx, "post", fake_post)

    def send(message_id):
        return asyncio.run(daemon._dispatch_to_host(
            "hello", task_id="task-1", context_id="ctx-1", message_id=message_id,
        ))

    assert send("msg-1") == "ok"
    first_id = daemon._client_message_id("task-1", "msg-1")
    second_id = daemon._client_message_id("task-1", "msg-2")
    assert calls[-1][1]["client_message_id"] == first_id
    assert daemon._load_task("task-1")["ouroboros"] == {
        "chat_id": -123, "client_message_id": first_id, "operation_ref": f"-123:{first_id}", "message_ids": [first_id],
    }
    # A re-delivered message reuses the bound chat and identity: no second
    # allocation, and the host sees the SAME (chat_id, client_message_id).
    calls.clear()
    assert send("msg-1") == "ok"
    assert [url.rsplit("/", 1)[1] for url, _body in calls] == ["inject"]
    assert calls[0][1]["chat_id"] == -123 and calls[0][1]["client_message_id"] == first_id
    # A NEW message in the same task continues the conversation under its own identity.
    calls.clear()
    assert send("msg-2") == "ok"
    assert calls[-1][1]["chat_id"] == -123 and calls[-1][1]["client_message_id"] == second_id
    assert daemon._load_task("task-1")["ouroboros"]["operation_ref"] == f"-123:{second_id}"


def test_cancel_reports_only_what_the_host_proved(tmp_path, monkeypatch):
    daemon = _load_daemon(tmp_path, monkeypatch)
    with pytest.raises(daemon._HostCancelRefused) as unbound:
        daemon._cancel_host_operation_sync("never-seen")
    assert unbound.value.outcome == "unbound"
    daemon._update_task_record("task-1", "ctx", binding={
        "chat_id": -5, "client_message_id": "a2a:task-1:m", "operation_ref": "-5:a2a:task-1:m",
    })
    answers = iter([
        (409, {"ok": False, "outcome": "cancel_unsupported", "reason": "not_started"}),
        (503, {"ok": False, "outcome": "unresolved", "reason": "cancellation_did_not_settle"}),
        (200, {"ok": True, "outcome": "already_terminal", "status": "completed"}),
        (200, {"ok": True, "outcome": "cancelled", "task_id": "abc", "status": "cancelled"}),
    ])
    posted = []

    def fake_post(url, **kwargs):
        posted.append((url, kwargs["json"]))
        status, payload = next(answers)
        return _Resp(status, payload)

    monkeypatch.setattr(daemon.httpx, "post", fake_post)
    for expected in ("cancel_unsupported", "unresolved", "already_terminal"):
        with pytest.raises(daemon._HostCancelRefused) as refused:
            daemon._cancel_host_operation_sync("task-1")
        assert refused.value.outcome == expected
    assert daemon._cancel_host_operation_sync("task-1")["outcome"] == "cancelled"
    assert all(url.endswith("/chat/cancel") and body["operation_ref"] == "-5:a2a:task-1:m" for url, body in posted)


def _rpc_body(response):
    import json

    return json.loads(response.body)


def test_jsonrpc_tasks_cancel_never_fakes_a_canceled_task(tmp_path, monkeypatch):
    daemon = _load_daemon(tmp_path, monkeypatch)
    missing = _rpc_body(asyncio.run(daemon._jsonrpc_cancel("r1", "never-seen")))
    assert missing["error"]["code"] == -32001
    daemon._update_task_record("task-1", "ctx", binding={
        "chat_id": -5, "client_message_id": "a2a:task-1:m", "operation_ref": "-5:a2a:task-1:m",
    })
    monkeypatch.setattr(daemon.httpx, "post", lambda url, **kw: _Resp(
        409, {"ok": False, "outcome": "cancel_unsupported", "reason": "decision_turn_in_flight"},
    ))
    refused = _rpc_body(asyncio.run(daemon._jsonrpc_cancel("r2", "task-1")))
    assert refused["error"]["code"] == -32002 and "decision_turn_in_flight" in refused["error"]["message"]
    assert daemon._load_task("task-1")["status"]["state"] == "working"
    monkeypatch.setattr(daemon.httpx, "post", lambda url, **kw: _Resp(
        200, {"ok": True, "outcome": "cancelled", "task_id": "abc", "status": "cancelled"},
    ))
    cancelled = _rpc_body(asyncio.run(daemon._jsonrpc_cancel("r3", "task-1")))
    assert cancelled["result"]["status"]["state"] == "canceled"
    assert daemon._load_task("task-1")["status"]["state"] == "canceled"
    terminal = _rpc_body(asyncio.run(daemon._jsonrpc_cancel("r4", "task-1")))
    assert terminal["error"]["code"] == -32002 and "canceled" in terminal["error"]["message"]


def test_sdk_executor_cancel_publishes_canceled_only_on_the_host_outcome(tmp_path, monkeypatch):
    """The executor's ``cancel`` (stubbed SDK surface) raises the SDK's
    TaskNotCancelableError on every refusal and publishes ``canceled`` only for
    the host's ``cancelled`` outcome."""
    from types import SimpleNamespace

    daemon = _load_daemon(tmp_path, monkeypatch)
    published = []

    class _Updater:
        def __init__(self, queue, task_id, context_id):
            self.task_id = task_id

        def new_agent_message(self, parts):
            return {"parts": parts}

        async def cancel(self, message=None):
            published.append((self.task_id, message))

    monkeypatch.setattr(daemon, "TaskUpdater", _Updater, raising=False)
    monkeypatch.setattr(daemon, "Part", lambda text: {"text": text}, raising=False)
    context = SimpleNamespace(task_id="task-1", context_id="ctx-1")
    executor = daemon.OuroborosExecutor()
    with pytest.raises(daemon.TaskNotCancelableError) as unbound:
        asyncio.run(executor.cancel(context, []))
    assert "no host operation" in str(unbound.value)
    assert published == []
    daemon._update_task_record("task-1", "ctx-1", binding={
        "chat_id": -5, "client_message_id": "a2a:task-1:m", "operation_ref": "-5:a2a:task-1:m",
    })
    monkeypatch.setattr(daemon.httpx, "post", lambda url, **kw: _Resp(
        503, {"ok": False, "outcome": "unresolved", "reason": "cancellation_did_not_settle"},
    ))
    with pytest.raises(daemon.TaskNotCancelableError) as unresolved:
        asyncio.run(executor.cancel(context, []))
    assert "unresolved" in str(unresolved.value)
    assert published == []
    monkeypatch.setattr(daemon.httpx, "post", lambda url, **kw: _Resp(
        200, {"ok": True, "outcome": "cancelled", "task_id": "abc", "status": "cancelled"},
    ))
    asyncio.run(executor.cancel(context, []))
    assert published and published[0][0] == "task-1"
    assert daemon._load_task("task-1")["status"]["state"] == "canceled"


def test_fallback_route_dispatches_tasks_cancel(tmp_path, monkeypatch):
    """The no-SDK JSON-RPC route reaches ``tasks/cancel`` (the first probe run
    answered ``method not found`` because the branch sat behind the
    ``message/send`` guard); an unknown method still answers -32601."""
    from starlette.testclient import TestClient

    daemon = _load_daemon(tmp_path, monkeypatch)
    if daemon._A2A_SDK_AVAILABLE:
        pytest.skip("the SDK routes own tasks/cancel when the a2a-sdk is installed")
    client = TestClient(daemon.app)
    missing = client.post("/", json={"jsonrpc": "2.0", "id": "r1", "method": "tasks/cancel",
                                     "params": {"id": "never-seen"}}).json()
    assert missing["error"]["code"] == -32001
    unknown = client.post("/", json={"jsonrpc": "2.0", "id": "r2", "method": "tasks/nope", "params": {}}).json()
    assert unknown["error"]["code"] == -32601
    daemon._update_task_record("task-1", "ctx", binding={
        "chat_id": -5, "client_message_id": "a2a:task-1:m", "operation_ref": "-5:a2a:task-1:m",
    })
    monkeypatch.setattr(daemon.httpx, "post", lambda url, **kw: _Resp(
        200, {"ok": True, "outcome": "cancelled", "task_id": "abc", "status": "cancelled"},
    ))
    cancelled = client.post("/", json={"jsonrpc": "2.0", "id": "r3", "method": "tasks/cancel",
                                       "params": {"id": "task-1"}}).json()
    assert cancelled["result"]["status"]["state"] == "canceled"
