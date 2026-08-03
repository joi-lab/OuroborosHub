"""Outbound peer credentials must never reach an unconfigured peer.

`discover`/`send`/`status` accept an arbitrary caller-supplied URL, so a
process-wide credential was handed to whatever address the model passed.
"""

import importlib.util
import sys
from pathlib import Path


def _load_client(monkeypatch, password="", peer_url=""):
    monkeypatch.setenv("A2A_CLIENT_PASSWORD", password)
    monkeypatch.setenv("A2A_CLIENT_PEER_URL", peer_url)
    module_path = Path(__file__).resolve().parents[1] / "lib" / "client.py"
    spec = importlib.util.spec_from_file_location(f"a2a_client_test_{id(monkeypatch)}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_credential_is_sent_only_to_the_configured_origin(monkeypatch):
    client = _load_client(monkeypatch, password="secret", peer_url="https://peer.example:8443/a2a")

    assert client._auth_for("https://peer.example:8443") == ("ouroboros", "secret")
    # Same host, different port / scheme / host are all DIFFERENT origins.
    assert client._auth_for("https://peer.example:9999") is None
    assert client._auth_for("http://peer.example:8443") is None
    assert client._auth_for("https://attacker.example:8443") is None


def test_no_credential_without_an_explicit_peer_url(monkeypatch):
    """A password alone must not authorize sending it anywhere."""
    client = _load_client(monkeypatch, password="secret", peer_url="")
    assert client._auth_for("https://peer.example") is None


def test_no_credential_without_a_password(monkeypatch):
    client = _load_client(monkeypatch, password="", peer_url="https://peer.example")
    assert client._auth_for("https://peer.example") is None


def test_unusable_target_url_gets_no_credential(monkeypatch):
    client = _load_client(monkeypatch, password="secret", peer_url="https://peer.example")
    for bad in ("", "not a url", "file:///etc/passwd", "ftp://peer.example"):
        assert client._auth_for(bad) is None
