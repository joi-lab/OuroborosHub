"""Unit tests for the v1.3.0 resilient dispatch pipeline (no a2a-sdk needed).

Covers the two stream defects observed on a live install:
- internal timeouts surfacing as JSON-RPC -32603 "timed out" (allocate 5s
  hardcoded; inject wait capped with no recovery) — now: allocate retry with a
  configurable timeout, and a wait expiry (host 504 / socket timeout) falls back
  to polling the durable chat log instead of failing;
- progress notes: `_fetch_progress_notes_sync` filters the gateway progress log
  by our negative chat_id, dedups across polls, and strips narration prefixes.
"""

import importlib.util
import os
import pathlib
import time

_DAEMON = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "a2a_daemon.py"


def _load_daemon(env: dict | None = None):
    for key in ("A2A_AGENT_NAME", "A2A_AGENT_DESCRIPTION", "HOST_SERVICE_TOKEN"):
        os.environ.pop(key, None)
    os.environ["OUROBOROS_SKILL_STATE_DIR"] = "/tmp/a2a_test_state_dir_nonexistent"
    os.environ["A2A_TOOLS_REFRESHER"] = "0"
    for k, v in (env or {}).items():
        os.environ[k] = v
    spec = importlib.util.spec_from_file_location("a2ad_stream_under_test", _DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.time.sleep = lambda *_a, **_k: None  # neutralize retry backoff / poll sleeps
    return mod


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _install_http(mod, *, post_script, get_script=None):
    """post_script/get_script: lists of (matcher, response-or-exception); each
    request consumes the FIRST matching entry (or the last one repeats)."""
    calls = {"post": [], "get": []}
    post_seq = list(post_script)

    def fake_post(url, **_kw):
        calls["post"].append(url)
        for i, (frag, resp) in enumerate(post_seq):
            if frag in url:
                post_seq.pop(i)
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected POST {url} (script exhausted)")

    get_seq = list(get_script or [])

    def fake_get(url, **_kw):
        calls["get"].append(url)
        for i, (frag, resp) in enumerate(get_seq):
            if frag in url:
                get_seq.pop(i)
                if isinstance(resp, Exception):
                    raise resp
                return resp
        # gateway polls may repeat: repeat the LAST configured matching response
        for frag, resp in reversed(list(get_script or [])):
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected GET {url}")

    mod.httpx.post = fake_post
    mod.httpx.get = fake_get
    return calls


def test_allocate_retries_once_then_succeeds():
    mod = _load_daemon()
    calls = _install_http(
        mod,
        post_script=[
            ("allocate-internal", RuntimeError("connect timeout")),
            ("allocate-internal", _FakeResponse(200, {"chat_id": -7})),
        ],
    )
    assert mod._allocate_chat_id_sync() == -7
    assert len(calls["post"]) == 2


def test_allocate_two_failures_is_hard_error():
    mod = _load_daemon()
    _install_http(
        mod,
        post_script=[
            ("allocate-internal", RuntimeError("boom1")),
            ("allocate-internal", RuntimeError("boom2")),
        ],
    )
    try:
        mod._allocate_chat_id_sync()
    except RuntimeError as exc:
        assert "allocation failed" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_inject_success_returns_text():
    mod = _load_daemon()
    _install_http(
        mod,
        post_script=[("chat/inject", _FakeResponse(200, {"ok": True, "response": "final answer"}))],
    )
    assert mod._inject_sync(-7, "hello") == "final answer"


def test_inject_504_raises_wait_expired_not_hard_error():
    mod = _load_daemon()
    _install_http(
        mod,
        post_script=[("chat/inject", _FakeResponse(504, {"ok": False, "error": "timed out waiting for response"}))],
    )
    try:
        mod._inject_sync(-7, "hello")
    except mod._HostWaitExpired:
        pass
    else:
        raise AssertionError("expected _HostWaitExpired")


def test_inject_socket_timeout_raises_wait_expired():
    mod = _load_daemon()
    _install_http(
        mod,
        post_script=[("chat/inject", mod.httpx.ReadTimeout("timed out"))],
    )
    try:
        mod._inject_sync(-7, "hello")
    except mod._HostWaitExpired:
        pass
    else:
        raise AssertionError("expected _HostWaitExpired")


def test_inject_429_stays_a_hard_error():
    mod = _load_daemon()
    _install_http(
        mod,
        post_script=[("chat/inject", _FakeResponse(429, {"ok": False, "error": "rate limit exceeded"}))],
    )
    try:
        mod._inject_sync(-7, "hello")
    except mod._HostWaitExpired:
        raise AssertionError("429 must NOT be treated as a recoverable wait expiry")
    except RuntimeError:
        pass


def test_wait_expiry_falls_back_to_chat_log_and_recovers_answer():
    """THE showstopper scenario: wait expires, task finishes later, the answer is
    recovered from the durable chat log instead of dying with -32603."""
    mod = _load_daemon()
    empty = _FakeResponse(200, {"entries": []})
    ready = _FakeResponse(200, {"entries": [
        {"chat_id": -7, "direction": "in", "text": "hello", "ts": "t1"},
        {"chat_id": -9, "direction": "out", "text": "someone else's answer", "ts": "t2"},
        {"chat_id": -7, "direction": "out", "text": "late but real answer", "ts": "t3"},
    ]})
    _install_http(
        mod,
        post_script=[("chat/inject", _FakeResponse(504, {"ok": False}))],
        get_script=[("api/logs/chat", empty), ("api/logs/chat", ready)],
    )
    out = mod._dispatch_after_allocate_sync(-7, "hello", time.monotonic() + 60)
    assert out == "late but real answer"


def test_fallback_ignores_inbound_row_and_foreign_chats():
    mod = _load_daemon()
    only_noise = _FakeResponse(200, {"entries": [
        {"chat_id": -7, "direction": "in", "text": "our own question", "ts": "t1"},
        {"chat_id": -9, "direction": "out", "text": "foreign", "ts": "t2"},
    ]})
    _install_http(mod, post_script=[], get_script=[("api/logs/chat", only_noise)])
    assert mod._fetch_final_answer_sync(-7) is None


def test_fallback_deadline_exhaustion_is_honest_error():
    mod = _load_daemon()
    _install_http(
        mod,
        post_script=[("chat/inject", _FakeResponse(504, {"ok": False}))],
        get_script=[("api/logs/chat", _FakeResponse(200, {"entries": []}))],
    )
    try:
        mod._dispatch_after_allocate_sync(-7, "hello", time.monotonic() - 1)
    except RuntimeError as exc:
        assert "A2A_STREAM_DEADLINE_SEC" in str(exc)
        assert "may still be running" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_progress_notes_filter_dedup_and_strip():
    mod = _load_daemon()
    rows = _FakeResponse(200, {"entries": [
        {"chat_id": -7, "text": "💬 Reading the repository", "ts": "t1", "_line": 1},
        {"chat_id": -9, "text": "💬 foreign chat note", "ts": "t1", "_line": 2},
        {"chat_id": -7, "text": "", "ts": "t2", "_line": 3},
        {"chat_id": -7, "text": "💬 Running tests", "ts": "t3", "_line": 4},
    ]})
    _install_http(mod, post_script=[], get_script=[("api/logs/progress", rows)])
    seen: set = set()
    first = mod._fetch_progress_notes_sync(-7, seen)
    assert first == ["Reading the repository", "Running tests"]
    # second poll with the same rows: everything already seen
    second = mod._fetch_progress_notes_sync(-7, seen)
    assert second == []


def test_progress_notes_gateway_down_is_silent():
    mod = _load_daemon()
    _install_http(mod, post_script=[], get_script=[("api/logs/progress", RuntimeError("conn refused"))])
    assert mod._fetch_progress_notes_sync(-7, set()) == []


def test_response_timeout_cap_raised_and_allocate_knob_exists():
    mod = _load_daemon(env={"A2A_RESPONSE_TIMEOUT_SEC": "99999", "A2A_ALLOCATE_TIMEOUT_SEC": "5"})
    assert mod.A2A_RESPONSE_TIMEOUT_SEC == 1740  # clamped to the host-safe max, not 600
    assert mod.A2A_ALLOCATE_TIMEOUT_SEC == 5
    os.environ.pop("A2A_RESPONSE_TIMEOUT_SEC", None)
    os.environ.pop("A2A_ALLOCATE_TIMEOUT_SEC", None)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"ALL_OK ({len(fns)} tests)")


if __name__ == "__main__":
    _run_all()
