"""Behavior tests for the a2a agent-card builder (_agent_card).

Mocks the Host Service (/identity and /tools/schemas) so we exercise the real
card-building logic without a live host: identity success/failure, operator env
override precedence, tool-fetch retry exhaustion + WARNING, non-empty tools, and
the genuinely-empty-tools honest fallback (no "General" collapse). Also asserts
the A2A v0.3 dict-card transport fields survive.

Run with an interpreter that has httpx + starlette (the skill's runtime deps):
    python3 -m pytest tests/test_a2a_agent_card.py
or standalone:
    python3 tests/test_a2a_agent_card.py
"""
from __future__ import annotations

import importlib.util
import logging
import os
import pathlib

_DAEMON = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "a2a_daemon.py"


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _load_daemon(env: dict | None = None):
    """(Re)load the daemon module with a controlled environment."""
    for key in ("A2A_AGENT_NAME", "A2A_AGENT_DESCRIPTION", "HOST_SERVICE_TOKEN"):
        os.environ.pop(key, None)
    os.environ["OUROBOROS_SKILL_STATE_DIR"] = "/tmp/a2a_test_state_dir_nonexistent"
    for k, v in (env or {}).items():
        os.environ[k] = v
    spec = importlib.util.spec_from_file_location("a2ad_under_test", _DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.time.sleep = lambda *_a, **_k: None  # neutralize retry backoff
    return mod


def _install_get(mod, *, identity, tools):
    """identity: (status, payload) or Exception. tools: (status, payload) or Exception.
    Returns a counter dict tracking how many times /tools/schemas was hit."""
    calls = {"identity": 0, "tools": 0}

    def fake_get(url, **_kw):
        if url.endswith("/identity"):
            calls["identity"] += 1
            if isinstance(identity, Exception):
                raise identity
            return _FakeResponse(*identity)
        if url.endswith("/tools/schemas"):
            calls["tools"] += 1
            if isinstance(tools, Exception):
                raise tools
            return _FakeResponse(*tools)
        raise AssertionError(f"unexpected url {url}")

    mod.httpx.get = fake_get
    return calls


def _capture_warnings(mod):
    records: list[str] = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = _H(level=logging.WARNING)
    mod.logger.addHandler(h)
    mod.logger.setLevel(logging.WARNING)
    return records


def test_identity_success_and_nonempty_tools():
    mod = _load_daemon()
    _install_get(
        mod,
        identity=(200, {"ok": True, "name": "Wanderer", "description": "a self-authoring agent"}),
        tools=(200, {"tools": [
            {"function": {"name": "read_file", "description": "Read a file."}},
            {"function": {"name": "web_search", "description": "Search the web."}},
        ]}),
    )
    card = mod._agent_card()
    assert card["name"] == "Wanderer"
    assert card["description"] == "a self-authoring agent"
    ids = [s["id"] for s in card["skills"]]
    assert ids == ["read_file", "web_search"]
    # v0.3 transport fields preserved
    assert card["protocolVersion"] == "0.3.0"
    assert card["preferredTransport"] == "JSONRPC"
    assert card["additionalInterfaces"] and card["additionalInterfaces"][0]["transport"] == "JSONRPC"
    assert card["version"] == "1.1.1"


def test_env_override_beats_identity():
    mod = _load_daemon(env={"A2A_AGENT_NAME": "OpName", "A2A_AGENT_DESCRIPTION": "OpDesc"})
    _install_get(
        mod,
        identity=(200, {"ok": True, "name": "IdentityName", "description": "IdentityDesc"}),
        tools=(200, {"tools": []}),
    )
    card = mod._agent_card()
    assert card["name"] == "OpName"
    assert card["description"] == "OpDesc"


def test_identity_failure_falls_back_to_defaults():
    mod = _load_daemon()
    recs = _capture_warnings(mod)
    _install_get(
        mod,
        identity=RuntimeError("connection refused"),
        tools=(200, {"tools": [{"function": {"name": "read_file", "description": "Read."}}]}),
    )
    card = mod._agent_card()
    assert card["name"] == "Ouroboros"
    assert card["description"] == "Ouroboros A2A peer"
    assert [s["id"] for s in card["skills"]] == ["read_file"]
    assert any("/identity" in r for r in recs), recs


def test_tools_retry_exhaustion_warns_and_falls_back():
    mod = _load_daemon()
    recs = _capture_warnings(mod)
    calls = _install_get(
        mod,
        identity=(200, {"ok": True, "name": "Wanderer", "description": "real desc"}),
        tools=RuntimeError("host chat-agent not ready"),
    )
    card = mod._agent_card()
    # retried the configured number of times
    assert calls["tools"] == mod._TOOLS_FETCH_ATTEMPTS
    # honest identity fallback, NOT a "General" stub
    assert len(card["skills"]) == 1
    only = card["skills"][0]
    assert only["id"] == "ouroboros"
    assert only["name"] == "Wanderer"
    assert only["description"] == "real desc"
    assert only["name"] != "General"
    assert any("/tools/schemas" in r for r in recs), recs


def test_empty_tools_uses_honest_identity_entry():
    mod = _load_daemon()
    _install_get(
        mod,
        identity=(200, {"ok": True, "name": "Wanderer", "description": "real desc"}),
        tools=(200, {"tools": []}),
    )
    card = mod._agent_card()
    assert len(card["skills"]) == 1
    assert card["skills"][0]["id"] == "ouroboros"
    assert card["skills"][0]["description"] == "real desc"
    # never the contentless collapse
    assert all(s["name"] != "General" for s in card["skills"])


def _install_get_tool_sequence(mod, *, identity, tool_payloads):
    """Like _install_get but /tools/schemas returns a SEQUENCE of payloads across
    successive calls (the last payload repeats once exhausted), so we can model a
    host that is empty for the first few polls and then becomes ready."""
    calls = {"identity": 0, "tools": 0}
    seq = list(tool_payloads)

    def fake_get(url, **_kw):
        if url.endswith("/identity"):
            calls["identity"] += 1
            return _FakeResponse(*identity)
        if url.endswith("/tools/schemas"):
            idx = min(calls["tools"], len(seq) - 1)
            calls["tools"] += 1
            return _FakeResponse(*seq[idx])
        raise AssertionError(f"unexpected url {url}")

    mod.httpx.get = fake_get
    return calls


def test_empty_200_is_retried_then_populates():
    """An empty 200 (host chat-agent not ready) must be retried, not accepted as
    final. Host is empty for the first 3 polls, then returns the real tools."""
    mod = _load_daemon()
    empty = (200, {"tools": []})
    ready = (200, {"tools": [
        {"function": {"name": "read_file", "description": "Read a file."}},
        {"function": {"name": "web_search", "description": "Search the web."}},
    ]})
    calls = _install_get_tool_sequence(
        mod,
        identity=(200, {"ok": True, "name": "Wanderer", "description": "real desc"}),
        tool_payloads=[empty, empty, empty, ready],
    )
    card = mod._agent_card()
    # It kept polling past the empty 200s and picked up the real list.
    assert calls["tools"] == 4
    assert [s["id"] for s in card["skills"]] == ["read_file", "web_search"]
    # Not the identity-only collapse.
    assert all(s["id"] != "ouroboros" for s in card["skills"])


def test_last_good_cache_prevents_regression_to_empty():
    """Once the card has populated, a later empty fetch must NOT regress it back
    to the identity-only entry — the last known-good tool list is served."""
    mod = _load_daemon()
    ready = (200, {"tools": [{"function": {"name": "read_file", "description": "Read."}}]})
    empty = (200, {"tools": []})
    # First build populates the cache.
    _install_get_tool_sequence(
        mod,
        identity=(200, {"ok": True, "name": "Wanderer", "description": "real desc"}),
        tool_payloads=[ready],
    )
    first = mod._agent_card()
    assert [s["id"] for s in first["skills"]] == ["read_file"]
    # Now every subsequent fetch is empty — the card must still show the cached tools.
    recs = _capture_warnings(mod)
    _install_get_tool_sequence(
        mod,
        identity=(200, {"ok": True, "name": "Wanderer", "description": "real desc"}),
        tool_payloads=[empty],
    )
    second = mod._agent_card()
    assert [s["id"] for s in second["skills"]] == ["read_file"], second["skills"]
    assert any("last known-good" in r for r in recs), recs


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"ALL_OK ({len(fns)} tests)")


if __name__ == "__main__":
    _run_all()
