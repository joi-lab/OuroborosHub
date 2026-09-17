"""Offline subprocess fixture for test_isolated_signing; not a skill entrypoint."""

import json
import os
from pathlib import Path
import shutil
import sys


def main():
    repo, source_skill, temp_root = map(Path, sys.argv[1:])
    os.environ["OUROBOROS_DATA_DIR"] = str(temp_root / "drive")
    os.environ["OUROBOROS_RUNTIME_MODE"] = "advanced"
    sys.path.insert(0, str(repo))
    import cryptography
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from ouroboros import extension_loader
    from ouroboros.extension_process_runner import dispatch_extension_tool_subprocess
    from ouroboros.skill_loader import (
        SkillReviewState, find_skill, save_enabled, save_review_state, save_skill_grants,
    )
    from ouroboros.tools.registry import ToolContext
    from supervisor import queue, state
    from tests._extension_loader_shared import _mark_isolated_deps_installed

    drive = temp_root / "drive"
    drive.mkdir(exist_ok=True)
    state.init(drive)
    queue.init(drive)
    skills = temp_root / "skills"
    skill = skills / "google-workspace"
    skill.mkdir(parents=True)
    for name in ("SKILL.md", "auth.py", "client.py"):
        shutil.copyfile(source_skill / name, skill / name)
    shutil.copyfile(source_skill / "plugin.py", skill / "original_plugin.py")
    # Mock only network I/O; the original plugin, JWT signer, client, manifest,
    # native dependency, registration and process dispatch are all exercised.
    (skill / "plugin.py").write_text('''
import base64
from urllib.parse import parse_qs
import httpx
from . import original_plugin, auth

_Client = httpx.Client

def _respond(request):
    if str(request.url) == "https://oauth2.googleapis.com/token":
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        assertion = parse_qs(request.content.decode())["assertion"][0]
        head, body, signature = assertion.split(".")
        # Verify the actual signature with the same disposable public key.
        key = serialization.load_pem_private_key(_credential["private_key"].encode(), None)
        key.public_key().verify(base64.urlsafe_b64decode(signature + "=="),
                                (head + "." + body).encode(), padding.PKCS1v15(), hashes.SHA256())
        return httpx.Response(200, json={"access_token": "fixture-token", "expires_in": 3600})
    if request.url.path == "/drive/v3/files/fixture_doc":
        return httpx.Response(200, json={"id": "fixture_doc", "name": "Fixture",
                                       "mimeType": "application/vnd.google-apps.document"})
    if request.url.path == "/drive/v3/files/fixture_doc/export":
        return httpx.Response(200, text="Disposable connector test content")
    raise AssertionError("Unexpected network request: " + str(request.url))

def _client(*args, **kwargs):
    kwargs["transport"] = httpx.MockTransport(_respond)
    return _Client(*args, **kwargs)

def register(api):
    global _credential
    _credential = auth.parse_service_account_info(api.get_settings(["GOOGLE_SERVICE_ACCOUNT_JSON"])["GOOGLE_SERVICE_ACCOUNT_JSON"])
    httpx.Client = _client
    original_plugin.register(api)
''', encoding="utf-8")
    site = skill / ".ouroboros_env" / "python" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    site.mkdir(parents=True)
    shutil.copytree(Path(cryptography.__file__).parent, site / "cryptography")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    credential = {"type": "service_account", "client_email": "fixture@example.invalid",
                  "token_uri": "https://oauth2.googleapis.com/token",
                  "private_key": key.private_bytes(serialization.Encoding.PEM,
                      serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()}
    settings = {"GOOGLE_SERVICE_ACCOUNT_JSON": json.dumps(credential)}
    (drive / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    loaded = find_skill(drive, "google-workspace", repo_path=str(skills))
    assert loaded is not None
    save_enabled(drive, loaded.name, True)
    save_review_state(drive, loaded.name, SkillReviewState(status="pass", content_hash=loaded.content_hash))
    permissions = list(loaded.manifest.permissions)
    keys = list(loaded.manifest.env_from_settings)
    save_skill_grants(drive, loaded.name, keys, content_hash=loaded.content_hash,
                      requested_keys=keys, granted_permissions=permissions, requested_permissions=permissions)
    _mark_isolated_deps_installed(drive, loaded)
    loaded = find_skill(drive, loaded.name, repo_path=str(skills))
    error = extension_loader.load_extension(loaded, lambda: settings, drive_root=drive)
    assert error is None, error
    try:
        ctx = ToolContext(repo_dir=repo, drive_root=drive)
        for _ in range(3):
            for name, args in (("workspace_auth_status", {}), ("drive_read_text", {"file_id": "fixture_doc"})):
                tool = extension_loader.get_tool(extension_loader.extension_surface_name(loaded.name, name))
                assert tool["out_of_process"]
                result = json.loads(dispatch_extension_tool_subprocess(tool, ctx, args))
                if name == "workspace_auth_status":
                    assert result["status"] == "ready"
                else:
                    assert result["text"] == "Disposable connector test content"
        print("verified 3 auth and 3 document calls in isolated children")
    finally:
        extension_loader.unload_extension(loaded.name)


if __name__ == "__main__":
    main()
