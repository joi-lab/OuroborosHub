"""Native signing regression, including optional real host-child dispatch."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


SKILL_DIR = Path(__file__).resolve().parents[1]


def test_registration_import_does_not_initialize_native_crypto():
    # A fresh interpreter matters: other tests generate RSA keys on collection.
    result = subprocess.run(
        [sys.executable, "-c", (
            "import importlib.util, sys; "
            "spec = importlib.util.spec_from_file_location('workspace_auth', sys.argv[1]); "
            "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
            "assert not any(n == 'cryptography' or n.startswith('cryptography.') for n in sys.modules)"
        ), str(SKILL_DIR / "auth.py")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_signing_through_extension_children(tmp_path):
    """Run with OUROBOROS_SOURCE_DIR to exercise the actual extension loader.

    Only fresh throwaway credentials and MockTransport are used. Loader review
    and grant records below are test fixtures, never installation attestations.
    """
    source = os.environ.get("OUROBOROS_SOURCE_DIR")
    if not source:
        pytest.skip("Set OUROBOROS_SOURCE_DIR to run the host integration test")
    env = dict(os.environ, OUROBOROS_DATA_DIR=str(tmp_path / "drive"),
               OUROBOROS_RUNTIME_MODE="advanced", PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("isolated_signing_probe.py")),
         source, str(SKILL_DIR), str(tmp_path)],
        env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "verified 3 auth and 3 document calls in isolated children" in result.stdout
    assert "verified 3 renewable OAuth auth and 3 document calls in isolated children" in result.stdout
