"""Audio (music) generation extension — in-process widget + agent tool.

v0.5.0: Adds kv metadata (format/MIME/size) to the widget result.

Generates a short music clip from a text prompt via OpenRouter's
``google/lyria-3-pro-preview`` model. Every generation writes the audio to
the skill's private state directory and both the inline ``<audio>`` player
and the **Download audio** button stream the file through the extension route
``GET /api/extensions/music_gen/download?clip_id=audio_<hex>.<ext>``.

A small kv panel shows ``detected`` format, ``mime`` type, and ``file_size_bytes``
so the user can see what was generated.

Security model matches ``image_gen``:

- Single-host allowlist (``openrouter.ai``). Cross-host redirects refused.
- ``OPENROUTER_API_KEY`` via canonical ``PluginAPI.get_settings`` only.
- Download route: path traversal via strict regex +
  ``Path.resolve().relative_to(state_dir.resolve())``.

Format handling
---------------

Lyria 3 Pro Preview ignores the ``audio.format`` hint and may deliver:
- real WAV (starts with ``RIFF...WAVE``), or
- MP3 bitstream (starts with ``ID3`` or MPEG frame sync), or
- OGG container, or
- raw PCM16/PCM24 without a header.

We inspect the first 16 bytes and pick the correct MIME/container.
For raw PCM: guess bit depth, wrap in a minimal RIFF/WAVE header.

Streaming: SSE chunks with base64 fragments in ``delta.audio.data``.
Fragments decoded per-chunk into a bytearray, re-encoded once at end.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response


# ---------------------------------------------------------------------------
# Security / network constants
# ---------------------------------------------------------------------------

_ALLOWED_HOST = "openrouter.ai"
_API_BASE = f"https://{_ALLOWED_HOST}/api/v1"
_TIMEOUT_SEC = 180  # wall-clock deadline for the whole SSE read
_USER_AGENT = "Ouroboros-MusicGen/0.5"
_MAX_PROMPT_LEN = 4096
_MAX_AUDIO_BYTES = 20 * 1024 * 1024  # 20 MB decoded cap
_MODEL = "google/lyria-3-pro-preview"
_SSE_PREFIX = "data: "

_PCM_SAMPLE_RATE = 48000
_PCM_CHANNELS = 2

_MIME_BY_EXTENSION: Dict[str, str] = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
}

_CLIP_ID_RE = re.compile(r"^audio_[a-f0-9]{12}\.(?:wav|mp3|ogg)$")


class _StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse cross-host redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        target = urllib.parse.urlparse(newurl).hostname
        if target != _ALLOWED_HOST:
            raise urllib.error.URLError(
                f"music_gen: cross-host redirect refused: {target!r} not in allowlist"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_StrictRedirectHandler())


def _normalize_prompt(prompt: Any) -> str:
    text = str(prompt or "").strip()
    if not text:
        return ""
    if len(text) > _MAX_PROMPT_LEN:
        return text[:_MAX_PROMPT_LEN]
    return text


def _b64decode_padded(chunk: str) -> bytes:
    """Decode a base64 fragment, tolerating missing trailing padding."""
    padding = "=" * (-len(chunk) % 4)
    return base64.b64decode(chunk + padding)


def _detect_audio_format(head: bytes) -> Tuple[str, str]:
    """Classify raw bytes by file-signature header."""
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ("wav", "wav")
    if head[:3] == b"ID3":
        return ("mp3", "mp3")
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return ("mp3", "mp3")
    if head[:4] == b"OggS":
        return ("ogg", "ogg")
    return ("pcm", "wav")


def _guess_pcm_bits_per_sample(n_bytes: int) -> int:
    """Pick 16 or 24 bits/sample to keep PCM frame count consistent."""
    for bd in (16, 24, 32):
        frame_bytes = _PCM_CHANNELS * (bd // 8)
        if frame_bytes > 0 and n_bytes % frame_bytes == 0:
            return bd
    return 16


def _wrap_pcm_as_wav(pcm: bytes, bits_per_sample: int) -> bytes:
    """Prepend a minimal RIFF/WAVE header to raw interleaved PCM."""
    byte_rate = _PCM_SAMPLE_RATE * _PCM_CHANNELS * (bits_per_sample // 8)
    block_align = _PCM_CHANNELS * (bits_per_sample // 8)
    data_size = len(pcm)
    fmt_chunk = struct.pack(
        "<4sIHHIIHH",
        b"fmt ",
        16,
        1,
        _PCM_CHANNELS,
        _PCM_SAMPLE_RATE,
        byte_rate,
        block_align,
        bits_per_sample,
    )
    data_chunk_header = struct.pack("<4sI", b"data", data_size)
    riff_size = 4 + len(fmt_chunk) + len(data_chunk_header) + data_size
    riff_header = struct.pack("<4sI4s", b"RIFF", riff_size, b"WAVE")
    return riff_header + fmt_chunk + data_chunk_header + pcm


def _generate_audio_bytes(
    api_key: str, prompt: str, *, audio_format: str = "wav"
) -> Dict[str, Any]:
    """Call OpenRouter SSE audio endpoint. Never raises."""
    if not api_key:
        return {
            "error": (
                "OPENROUTER_API_KEY is not granted. Configure the key in "
                "Ouroboros Settings and approve a key grant for music_gen "
                "on the Skills tab."
            )
        }

    cleaned_prompt = _normalize_prompt(prompt)
    if not cleaned_prompt:
        return {"error": "prompt is empty"}

    url = f"{_API_BASE}/chat/completions"
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != _ALLOWED_HOST:
        return {"error": f"refusing host {parsed.netloc!r}"}

    body = json.dumps(
        {
            "model": _MODEL,
            "messages": [{"role": "user", "content": cleaned_prompt}],
            "modalities": ["text", "audio"],
            "audio": {"format": audio_format},
            "stream": True,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": _USER_AGENT,
        },
        method="POST",
    )

    deadline = time.monotonic() + _TIMEOUT_SEC
    audio_buf = bytearray()
    transcript_parts: list[str] = []

    try:
        response = _OPENER.open(request, timeout=_TIMEOUT_SEC)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        return {"error": f"upstream HTTP {exc.code}: {exc.reason} {detail}".strip()}
    except urllib.error.URLError as exc:
        return {"error": f"network: {exc.reason!r}"}
    except TimeoutError:
        return {"error": f"upstream timed out opening connection after {_TIMEOUT_SEC}s"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

    try:
        for raw_line in response:
            if time.monotonic() > deadline:
                return {"error": f"generation timed out after {_TIMEOUT_SEC}s"}
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line or line.startswith(":") or not line.startswith(_SSE_PREFIX):
                continue
            data = line[len(_SSE_PREFIX):].strip()
            if not data or data == "[DONE]":
                if data == "[DONE]":
                    break
                continue
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if not isinstance(chunk, dict):
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
            if not isinstance(delta, dict):
                continue
            audio = delta.get("audio")
            if not isinstance(audio, dict):
                continue
            b64_chunk = audio.get("data")
            if isinstance(b64_chunk, str) and b64_chunk:
                try:
                    decoded = _b64decode_padded(b64_chunk)
                except (base64.binascii.Error, ValueError):
                    continue
                if len(audio_buf) + len(decoded) > _MAX_AUDIO_BYTES:
                    return {"error": f"audio exceeds {_MAX_AUDIO_BYTES // (1024 * 1024)} MB decoded cap"}
                audio_buf.extend(decoded)
            transcript = audio.get("transcript")
            if isinstance(transcript, str) and transcript:
                transcript_parts.append(transcript)
    finally:
        try:
            response.close()
        except Exception:
            pass

    if not audio_buf:
        return {"error": "no audio bytes returned by upstream"}

    head = bytes(audio_buf[:16])
    detected, ext = _detect_audio_format(head)

    mime_by_kind = {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "ogg": "audio/ogg",
        "pcm": "audio/wav",
    }

    if detected == "pcm":
        bits = _guess_pcm_bits_per_sample(len(audio_buf))
        wrapped = _wrap_pcm_as_wav(bytes(audio_buf), bits)
        return {
            "audio_bytes": wrapped,
            "mime": mime_by_kind["pcm"],
            "extension": ext,
            "detected": f"pcm{bits}",
            "model": _MODEL,
            "text": " ".join(transcript_parts).strip()[:500],
        }

    return {
        "audio_bytes": bytes(audio_buf),
        "mime": mime_by_kind[detected],
        "extension": ext,
        "detected": detected,
        "model": _MODEL,
        "text": " ".join(transcript_parts).strip()[:500],
    }


def _persist_clip(
    state_dir: Path, result: Dict[str, Any], prompt: str
) -> Tuple[Path, str]:
    """Write audio bytes to state_dir. Returns (path, clip_id). Raises OSError."""
    digest = hashlib.sha256(
        (str(prompt) + "|" + str(time.time_ns())).encode("utf-8")
    ).hexdigest()[:12]
    ext = str(result["extension"])
    if ext not in _MIME_BY_EXTENSION:
        raise OSError(f"music_gen: unsupported extension {ext!r}")
    clip_id = f"audio_{digest}.{ext}"
    out_path = state_dir / clip_id
    out_path.write_bytes(result["audio_bytes"])
    return out_path, clip_id


# ---------------------------------------------------------------------------
# UI declaration (declarative v1)  — v0.5.0 adds kv metadata row
# ---------------------------------------------------------------------------

_UI_RENDER: Dict[str, Any] = {
    "kind": "declarative",
    "schema_version": 1,
    "components": [
        {
            "type": "form",
            "title": "Generate a music clip",
            "target": "result",
            "route": "generate",
            "method": "POST",
            "job": True,
            "status_route": "status",
            "interval_ms": 2000,
            "max_ticks": 180,
            "submit_label": "Generate",
            "fields": [
                {
                    "name": "prompt",
                    "label": "Prompt",
                    "type": "textarea",
                    "required": True,
                },
            ],
        },
        {
            "type": "status",
            "target": "result",
            "idle": "Enter a music prompt and press Generate. Typical latency: 30–60 seconds.",
            "loading": "Generating music — this usually takes 30–60 seconds…",
            "success": "Done.",
            "error": "Generation failed.",
        },
        {
            "type": "markdown",
            "target": "result",
            "path": "error",
        },
        {
            "type": "audio",
            "target": "result",
            "path": "clip_url",
            "label": "Generated audio",
        },
        {
            "type": "file",
            "target": "result",
            "path": "clip_url",
            "label": "Download audio",
        },
        # v0.5.0: show format/MIME/size metadata after a successful generation
        {
            "type": "kv",
            "target": "result",
            "fields": [
                {"path": "detected", "label": "Format"},
                {"path": "mime", "label": "MIME"},
                {"path": "file_size_bytes", "label": "File size (bytes)"},
            ],
        },
    ],
}


# ---------------------------------------------------------------------------
# PluginAPI entry point
# ---------------------------------------------------------------------------


def register(api: Any) -> None:
    """PluginAPI v1 entry point. Called exactly once per load."""
    jobs: Dict[str, Dict[str, Any]] = {}
    tasks: Dict[str, asyncio.Task[Any]] = {}
    loop = asyncio.get_running_loop()
    _MAX_JOBS = 25

    def _prune_jobs() -> None:
        terminal = [job_id for job_id, job in jobs.items() if job.get("status") in {"done", "error"}]
        while len(jobs) > _MAX_JOBS and terminal:
            jobs.pop(terminal.pop(0), None)

    def _cleanup_jobs() -> None:
        for job_id, task in list(tasks.items()):
            if not task.done():
                loop.call_soon_threadsafe(task.cancel)
                jobs[job_id] = {
                    "status": "error",
                    "error": "generation cancelled because extension unloaded",
                }
        tasks.clear()

    def _resolve_api_key() -> str:
        try:
            settings = api.get_settings(["OPENROUTER_API_KEY"]) or {}
        except Exception as exc:
            api.log("warning", f"music_gen: get_settings raised: {exc}")
            return ""
        value = settings.get("OPENROUTER_API_KEY")
        return str(value or "").strip()

    def _resolve_state_dir() -> Optional[Path]:
        try:
            raw = api.get_state_dir()
        except Exception as exc:
            api.log("warning", f"music_gen: get_state_dir raised: {exc}")
            return None
        if not raw:
            return None
        state_dir = Path(str(raw))
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            api.log("warning", f"music_gen: mkdir state dir failed: {exc}")
            return None
        return state_dir

    def _build_clip_url(clip_id: str) -> str:
        return (
            f"/api/extensions/music_gen/download"
            f"?clip_id={urllib.parse.quote(clip_id, safe='')}"
        )

    def _generate_and_persist(api_key: str, state_dir: Path, prompt: Any) -> Dict[str, Any]:
        result = _generate_audio_bytes(api_key, prompt)
        if "error" in result:
            return result
        try:
            out_path, clip_id = _persist_clip(state_dir, result, prompt)
        except OSError as exc:
            return {"error": f"music_gen: failed to write clip: {exc}"}
        return {
            "clip_url": _build_clip_url(clip_id),
            "clip_id": clip_id,
            "file_size_bytes": out_path.stat().st_size,
            "mime": result["mime"],
            "detected": result["detected"],
            "model": result["model"],
            "text": result.get("text", ""),
        }

    async def _run_generate_job(job_id: str, api_key: str, state_dir: Path, prompt: Any) -> None:
        jobs[job_id]["status"] = "running"
        try:
            result = await asyncio.to_thread(_generate_and_persist, api_key, state_dir, prompt)
            if "error" in result:
                jobs[job_id] = {"status": "error", "error": result["error"]}
            else:
                jobs[job_id] = {"status": "done", "result": result}
            _prune_jobs()
        except Exception as exc:
            jobs[job_id] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            _prune_jobs()

    async def _route_generate(request: Request) -> JSONResponse:
        """POST /api/extensions/music_gen/generate"""
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        prompt = payload.get("prompt", "")

        if not _normalize_prompt(prompt):
            return JSONResponse({"error": "prompt is empty"}, status_code=400)
        if len(tasks) >= _MAX_JOBS:
            return JSONResponse({"error": "too many active generation jobs; wait for one to finish"}, status_code=429)

        api_key = _resolve_api_key()
        state_dir = _resolve_state_dir()
        if state_dir is None:
            return JSONResponse(
                {"error": "music_gen: state dir unavailable, cannot persist audio"},
                status_code=500,
            )

        job_id = f"job_{uuid.uuid4().hex[:12]}"
        jobs[job_id] = {"status": "queued", "message": "Music generation queued."}
        task = asyncio.create_task(_run_generate_job(job_id, api_key, state_dir, prompt))
        tasks[job_id] = task
        task.add_done_callback(lambda _task, _job_id=job_id: tasks.pop(_job_id, None))
        return JSONResponse({"job_id": job_id, "status": "queued", "message": "Music generation started."})

    def _route_status(request: Request) -> JSONResponse:
        """GET /api/extensions/music_gen/status?job_id=..."""
        job_id = (request.query_params.get("job_id") or "").strip()
        job = jobs.get(job_id)
        if not job:
            return JSONResponse({"status": "error", "error": "job not found"}, status_code=404)
        return JSONResponse(job)

    def _route_download(request: Request) -> Response:
        """GET /api/extensions/music_gen/download?clip_id=..."""
        clip_id = (request.query_params.get("clip_id") or "").strip()
        if not clip_id or not _CLIP_ID_RE.match(clip_id):
            return JSONResponse({"error": "invalid clip_id"}, status_code=400)
        state_dir = _resolve_state_dir()
        if state_dir is None:
            return JSONResponse({"error": "music_gen: state dir unavailable"}, status_code=500)
        candidate = state_dir / clip_id
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(state_dir.resolve())
        except (ValueError, OSError):
            return JSONResponse({"error": "clip not found"}, status_code=404)
        if not resolved.is_file():
            return JSONResponse({"error": "clip not found"}, status_code=404)
        ext = clip_id.rsplit(".", 1)[-1]
        media_type = _MIME_BY_EXTENSION.get(ext, "application/octet-stream")
        return FileResponse(str(resolved), media_type=media_type, filename=clip_id)

    def _tool_generate(*, prompt: str = "") -> str:
        """Agent-callable music generation tool."""
        api_key = _resolve_api_key()
        state_dir = _resolve_state_dir()
        if state_dir is None:
            return json.dumps(
                {"error": "music_gen: state dir unavailable, cannot persist audio"},
                ensure_ascii=False,
            )

        result = _generate_audio_bytes(api_key, prompt)
        if "error" in result:
            return json.dumps(result, ensure_ascii=False)

        try:
            out_path, clip_id = _persist_clip(state_dir, result, prompt)
        except OSError as exc:
            return json.dumps(
                {"error": f"music_gen: failed to write clip: {exc}"},
                ensure_ascii=False,
            )

        return json.dumps(
            {
                "file_path": str(out_path),
                "clip_id": clip_id,
                "clip_url": _build_clip_url(clip_id),
                "bytes": out_path.stat().st_size,
                "mime": result["mime"],
                "detected": result["detected"],
                "model": result["model"],
                "prompt_used": str(prompt)[:200],
            },
            ensure_ascii=False,
        )

    api.register_tool(
        "generate",
        _tool_generate,
        description=(
            "Generate a music clip from a text prompt using Google's "
            "Lyria model via OpenRouter (google/lyria-3-pro-preview). "
            "Writes audio to the skill's private state directory and "
            "returns JSON with 'file_path', 'clip_id', 'clip_url', "
            "'bytes', 'mime', 'detected', 'model', 'prompt_used' on "
            "success, or 'error'."
        ),
        schema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Music description to synthesize.",
                },
            },
            "required": ["prompt"],
        },
        timeout_sec=_TIMEOUT_SEC + 15,
    )
    api.on_unload(_cleanup_jobs)
    api.register_route("generate", _route_generate, methods=("POST",))
    api.register_route("status", _route_status, methods=("GET",))
    api.register_route("download", _route_download, methods=("GET",))
    api.register_ui_tab(
        "music_gen",
        "Music generator",
        icon="music",
        render=_UI_RENDER,
    )
    api.log("info", "music_gen: v0.5.0 registered (routes: generate, download; tool; ui_tab)")


__all__ = ["register"]
