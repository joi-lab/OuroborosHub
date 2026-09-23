"""Complete large provider results survive IPC through existing file readers."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    from .. import results
except ImportError:
    import results


def api_for(path):
    return SimpleNamespace(get_state_dir=lambda: str(path))


def test_small_registered_result_keeps_exact_bytes_without_files(tmp_path):
    text = '{"text":"Привет 🧪","value":null,"ok":false}'
    handler = results.result_handler(api_for(tmp_path), "workspace_request", lambda: text)
    assert handler() == text
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("content", ["字🧪" * 150000, '\\"\n' * 150000])
def test_full_oversized_json_stored_before_escaped_ipc_cap(tmp_path, content):
    original = {"status_code": 200, "data": {"nested": [content, {"last": "sentinel"}]}}
    text = json.dumps(original, ensure_ascii=False, separators=(",", ":"))
    result = json.loads(results.result_handler(api_for(tmp_path), "workspace_request", lambda: text)())
    assert len(json.dumps({"result": json.dumps(result)}, ensure_ascii=False).encode()) < 512 * 1024
    raw = Path(result["path"]).read_bytes()
    assert raw == text.encode("utf-8")
    assert json.loads(raw) == original
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["size"] == len(raw)
    assert result["result_complete"] is True
    assert result["result_format"] == "json"
    assert result["actor_readable"] is False
    assert "read" not in result
    assert result["status_code"] == 200


def test_wire_escaping_not_plain_text_length_selects_staging(tmp_path):
    text = json.dumps({"text": '"' * 110000}, separators=(",", ":"))
    assert len(text.encode()) < results.INLINE_WIRE_BYTES
    assert len(json.dumps({"result": text}, ensure_ascii=False).encode()) > results.INLINE_WIRE_BYTES
    result = json.loads(results.result_handler(api_for(tmp_path), "docs_read", lambda: text)())
    assert result["stored"]
    assert Path(result["path"]).read_text(encoding="utf-8") == text


def test_content_addresses_preserve_versions_and_concurrent_exports(tmp_path):
    api = api_for(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        stored = list(pool.map(lambda value: results.store_state_bytes(api, value, name="Shared name", extension="txt"),
                               [b"old", b"new", b"old", b"new"]))
    assert stored[0]["path"] == stored[2]["path"]
    assert stored[1]["path"] == stored[3]["path"]
    assert stored[0]["path"] != stored[1]["path"]
    assert [Path(x["path"]).read_bytes() for x in stored] == [b"old", b"new", b"old", b"new"]
    other_format = results.store_state_bytes(api, b"old", name="Shared name", extension="json")
    assert other_format["path"] != stored[0]["path"]
    assert not list((tmp_path / "drive_files").glob(".result-*"))


def test_large_declared_failure_and_partial_provider_coverage_stay_visible(tmp_path):
    payload = {"ok": False, "status": "error", "status_code": 403, "data": "x" * 500000,
               "truncated": True, "export_scope": "first_sheet_only", "next_page_token": "next-page"}
    response = json.loads(results.result_handler(api_for(tmp_path), "workspace_request", lambda: json.dumps(payload))())
    for key in ("ok", "status", "status_code", "truncated", "export_scope", "next_page_token"):
        assert response[key] == payload[key]
    assert json.loads(Path(response["path"]).read_text(encoding="utf-8")) == payload
