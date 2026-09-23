"""Complete result files behind the existing task artifact/read_file seam."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import tempfile
from pathlib import Path

# Leave room for the host's child-protocol envelope (512 KiB). Measure the
# escaped envelope, not raw text: JSON strings may double quotes/backslashes.
INLINE_WIRE_BYTES = 384 * 1024


def file_extension(mime):
    return {"text/plain": "txt", "application/json": "json", "text/csv": "csv"}.get(mime) or (mimetypes.guess_extension(mime) or ".bin").lstrip(".")


def _json(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def store_state_bytes(api, data: bytes, *, name="result", extension="bin"):
    """Retain immutable content-addressed bytes using existing skill state."""
    directory = Path(api.get_state_dir()) / "drive_files"
    directory.mkdir(parents=True, exist_ok=True)
    label = "".join(c if c.isalnum() or c in "._-" else "_" for c in Path(name).name)
    label = (label or "result").encode("utf-8")[:96].decode("utf-8", errors="ignore")
    digest = hashlib.sha256(data).hexdigest()
    target = directory / f"{label}-{digest}.{extension}"
    if not target.is_file():
        fd, temporary = tempfile.mkstemp(prefix=".result-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {"path": str(target), "size": len(data), "sha256": digest, "stored": True}


def _actor_source(api, ctx, data, *, tool_name, extension, text_readable=True):
    if ctx is not None and getattr(ctx, "task_id", None) and getattr(ctx, "drive_root", None):
        # The same host source store that materializes long tool outputs. This
        # is a host helper, not a new PluginAPI method or permission surface.
        from ouroboros.artifacts import store_actor_source_bytes, task_artifact_dir_path
        from ouroboros.tool_access_paths import canonical_data_root

        # Child workspaces are disposable. The canonical task-owned store is
        # already readable through artifact_store lineage and survives cleanup.
        root = canonical_data_root(ctx)
        ref = store_actor_source_bytes(
            root, str(ctx.task_id), category="tool_results",
            source_id="google-" + tool_name, data=data, extension=extension,
        )
        path = task_artifact_dir_path(root, str(ctx.task_id)) / ref["path"]
        result = {"path": str(path), "actor_readable": True,
                  "size": ref["size"], "sha256": ref["sha256"], "stored": True}
        if text_readable:
            result.update(source_ref=ref, read={"tool": "read_file", "arguments": {
                **ref["read"]["arguments"], "path": str(path)}})
        else:
            result["read_hint"] = "Binary file: use the existing document/image tools or run_script with this absolute path; read_file is for UTF-8 text."
        return result
    # Direct standalone callers can retain data without importing the host.
    # Do not advertise a task-relative read route when there was no task ctx.
    return {**store_state_bytes(api, data, name=tool_name, extension=extension),
            "actor_readable": False, "read_unavailable_reason": "no task context; use the stored path outside task tools"}


def result_handler(api, name, handler):
    """Keep small result bytes unchanged; retain large output before host IPC."""
    def call(ctx=None, **kwargs):
        text = handler(**kwargs)
        if name in {"drive_download", "drive_export"}:
            payload = json.loads(text)
            if ctx is not None:
                path = Path(payload["path"])
                mime = str(payload.get("mime_type") or "")
                extension = file_extension(mime)
                payload.update(_actor_source(api, ctx, path.read_bytes(), tool_name=name, extension=extension,
                                             text_readable=mime.startswith("text/") or mime == "application/json"))
                return _json(payload)
            payload.update(actor_readable=False, read_unavailable_reason="no task context; use the stored path outside task tools")
            return _json(payload)
        if len(json.dumps({"result": text}, ensure_ascii=False).encode("utf-8")) <= INLINE_WIRE_BYTES:
            return text
        data = text.encode("utf-8")
        payload = json.loads(text)
        result = {"result_format": "json", "result_complete": True, "inline": False,
                  **_actor_source(api, ctx, data, tool_name=name, extension="json")}
        # Completion means complete bytes of THIS tool result, not all pages,
        # tabs or linked documents. Preserve explicit status/coverage facts.
        if isinstance(payload, dict):
            for key in ("ok", "status", "status_code", "truncated", "export_scope", "next_page_token"):
                if key in payload:
                    result[key] = payload[key]
        return _json(result)
    return call
