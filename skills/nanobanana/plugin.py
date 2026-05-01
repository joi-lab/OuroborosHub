"""Image generation extension — in-process widget + agent tool.

v0.2.0: Adds disk persistence and a proper download route.

Registers four PluginAPI v1 surfaces:

- ``register_route("generate", methods=("POST",))`` — accepts JSON
  ``{"prompt": "...", "model": "..."}``, calls OpenRouter's chat
  completions, persists the image to the skill state dir, and returns
  ``{"image_url": "/api/extensions/nanobanana/media?image_id=...",
     "download_url": "/api/extensions/nanobanana/download?image_id=...",
     "image_id": "...", "file_size_bytes": ..., "model": "...", "text": "..."}``.

- ``register_route("media", methods=("GET",))`` — streams the image
  inline (Content-Disposition: inline) for the ``<img>`` tag in the widget.

- ``register_route("download", methods=("GET",))`` — streams the image
  as an attachment (Content-Disposition: attachment) for the Download
  button, so the file saves with a proper filename.

- ``register_tool("generate", ...)`` — the same call exposed to the
  agent dispatcher. Returns a JSON string so the tool-output contract
  stays a single string.

- ``register_ui_tab("widget", ...)`` — declarative render schema v1.

Security model:
- Single-host allowlist (``openrouter.ai``). Cross-host redirects refused.
- ``OPENROUTER_API_KEY`` via canonical ``PluginAPI.get_settings`` only.
- Download/media routes: path traversal via strict regex +
  ``path.resolve().relative_to(state_dir.resolve())``.
- Inline vs attachment is controlled by Content-Disposition, not different data.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse


# ---------------------------------------------------------------------------
# Security / network constants
# ---------------------------------------------------------------------------

_ALLOWED_HOST = "openrouter.ai"
_API_BASE = f"https://{_ALLOWED_HOST}/api/v1"
_TIMEOUT_SEC = 60
_USER_AGENT = "Ouroboros-ImageGen/0.2"
_MAX_PROMPT_LEN = 4096

_DEFAULT_MODEL = "google/gemini-3.1-flash-image-preview"
_ALLOWED_MODELS = {
    "google/gemini-3.1-flash-image-preview",
    "google/gemini-3.1-flash-image-preview",
    "google/gemini-3-pro-image-preview",
}

# Strict filename guard for media/download routes.
# Matches: img_<12 hex chars>.<png|jpeg|jpg|webp>
_IMAGE_ID_RE = re.compile(r"^img_[a-f0-9]{12}\.(?:png|jpeg|jpg|webp)$")

# Allowed MIME types for generated images.
_MIME_BY_EXT: Dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "webp": "image/webp",
}


class _StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse cross-host redirects to protect the Bearer token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        target = urllib.parse.urlparse(newurl).hostname
        if target != _ALLOWED_HOST:
            raise urllib.error.URLError(
                f"nanobanana: cross-host redirect refused: {target!r} not in allowlist"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_StrictRedirectHandler())


# ---------------------------------------------------------------------------
# Helpers — input normalization
# ---------------------------------------------------------------------------


def _normalize_prompt(prompt: Any) -> str:
    text = str(prompt or "").strip()
    if not text:
        return ""
    if len(text) > _MAX_PROMPT_LEN:
        return text[:_MAX_PROMPT_LEN]
    return text


def _normalize_model(model: Any) -> str:
    candidate = str(model or "").strip() or _DEFAULT_MODEL
    if candidate not in _ALLOWED_MODELS:
        return _DEFAULT_MODEL
    return candidate


def _parse_data_url(data_url: str) -> Tuple[Optional[str], Optional[bytes]]:
    """Parse a ``data:image/...;base64,...`` URL into ``(mime, bytes)``.

    Returns ``(None, None)`` on any failure — callers convert to error.
    Validates: must start with ``data:image/``, must be base64-encoded,
    decoded bytes must be non-empty.
    """
    if not isinstance(data_url, str):
        return (None, None)
    try:
        header, encoded = data_url.split(",", 1)
    except ValueError:
        return (None, None)
    # header must be like "data:image/png;base64"
    if not header.startswith("data:image/"):
        return (None, None)
    if ";base64" not in header:
        return (None, None)
    mime_part = header[len("data:"):]
    mime = mime_part.split(";")[0].strip()
    if not mime.startswith("image/"):
        return (None, None)
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except Exception:
        return (None, None)
    if not decoded:
        return (None, None)
    return (mime, decoded)


def _mime_to_ext(mime: str) -> str:
    """Map MIME type to file extension, defaulting to 'png'."""
    return {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/jpg": "jpeg",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(mime, "png")


def _extract_data_url(payload: Dict[str, Any]) -> Optional[str]:
    """Pull the first ``data:image/...;base64,...`` URL from an
    OpenRouter chat-completions response."""
    try:
        choices = payload.get("choices") or []
        if not choices:
            return None
        message = choices[0].get("message") or {}
        images = message.get("images") or []
        for item in images:
            if not isinstance(item, dict):
                continue
            image_url = item.get("image_url") or {}
            url = image_url.get("url") if isinstance(image_url, dict) else None
            if isinstance(url, str) and url.startswith("data:image/"):
                return url
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _persist_image(
    state_dir: Path, mime: str, image_bytes: bytes, prompt: str
) -> Tuple[Path, str]:
    """Write image bytes to ``state_dir`` and return ``(path, image_id)``.

    The image id is deterministic-ish: ``img_<12 hex>.<ext>`` where the
    hex comes from ``sha256(prompt + time_ns)``.

    Raises ``OSError`` on write failure.
    """
    ext = _mime_to_ext(mime)
    digest = hashlib.sha256(
        (str(prompt) + "|" + str(time.time_ns())).encode("utf-8")
    ).hexdigest()[:12]
    image_id = f"img_{digest}.{ext}"
    out_path = state_dir / image_id
    out_path.write_bytes(image_bytes)
    return out_path, image_id


def _validate_image_id(image_id: str, state_dir: Path) -> Optional[Path]:
    """Validate image_id and return the resolved path, or None on error.

    Two-layer defense:
    1. Strict regex: only ``img_<12 hex>.<ext>`` passes.
    2. ``resolved.relative_to(state_dir)`` — path escape guard.
    """
    stripped = str(image_id or "").strip()
    if not stripped or not _IMAGE_ID_RE.match(stripped):
        return None
    candidate = state_dir / stripped
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(state_dir.resolve())
    except (ValueError, OSError):
        return None
    if not resolved.is_file():
        return None
    return resolved


# ---------------------------------------------------------------------------
# Core generation (blocking; wrap with asyncio.to_thread in async callers)
# ---------------------------------------------------------------------------


def _generate_image(api_key: str, prompt: str, model: str) -> Dict[str, Any]:
    """Call OpenRouter chat completions. Never raises.

    Returns ``{"data_url": "...", "mime": "...", "model": "...", "text": "..."}``
    on success, ``{"error": "..."}`` on any failure.
    """
    if not api_key:
        return {
            "error": (
                "OPENROUTER_API_KEY is not granted. Configure the key in "
                "Ouroboros Settings and approve a key grant for nanobanana "
                "on the Skills tab."
            )
        }

    cleaned_prompt = _normalize_prompt(prompt)
    if not cleaned_prompt:
        return {"error": "prompt is empty"}
    cleaned_model = _normalize_model(model)

    url = f"{_API_BASE}/chat/completions"
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != _ALLOWED_HOST:
        return {"error": f"refusing host {parsed.netloc!r}"}

    body = json.dumps(
        {
            "model": cleaned_model,
            "messages": [{"role": "user", "content": cleaned_prompt}],
            "modalities": ["image", "text"],
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        },
        method="POST",
    )

    try:
        with _OPENER.open(request, timeout=_TIMEOUT_SEC) as response:
            raw = response.read(16 * 1024 * 1024).decode("utf-8", errors="replace")
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
        return {"error": f"upstream timed out after {_TIMEOUT_SEC}s"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

    try:
        parsed_body = json.loads(raw)
    except ValueError:
        return {"error": "upstream returned non-JSON payload"}

    if isinstance(parsed_body, dict) and parsed_body.get("error"):
        err = parsed_body["error"]
        if isinstance(err, dict):
            message = err.get("message") or err.get("code") or str(err)
        else:
            message = str(err)
        return {"error": f"openrouter: {message}"}

    data_url = _extract_data_url(parsed_body)
    if not data_url:
        text_hint = ""
        try:
            choices = parsed_body.get("choices") or []
            if choices:
                msg = choices[0].get("message") or {}
                content = msg.get("content")
                if isinstance(content, str):
                    text_hint = content.strip()[:300]
        except Exception:
            pass
        if text_hint:
            return {"error": f"no image returned — assistant said: {text_hint!r}"}
        return {"error": "no image returned by upstream"}

    text_out = ""
    try:
        content = (parsed_body["choices"][0].get("message") or {}).get("content")
        if isinstance(content, str):
            text_out = content.strip()[:500]
    except Exception:
        pass

    mime, image_bytes = _parse_data_url(data_url)
    if mime is None or image_bytes is None:
        return {"error": "upstream returned unparseable data URL"}

    return {
        "data_url": data_url,
        "mime": mime,
        "image_bytes": image_bytes,
        "model": cleaned_model,
        "text": text_out,
    }


# ---------------------------------------------------------------------------
# UI declaration (declarative v1)
# ---------------------------------------------------------------------------

_UI_RENDER: Dict[str, Any] = {
    "kind": "declarative",
    "schema_version": 1,
    "components": [
        {
            "type": "form",
            "title": "Generate an image",
            "target": "result",
            "route": "generate",
            "method": "POST",
            "submit_label": "Generate",
            "fields": [
                {
                    "name": "prompt",
                    "label": "Prompt",
                    "type": "textarea",
                    "required": True,
                },
                {
                    "name": "model",
                    "label": "Model",
                    "type": "select",
                    "default": _DEFAULT_MODEL,
                    "options": [
                        {
                            "value": "google/gemini-3.1-flash-image-preview",
                            "label": "Nano Banana (Gemini 3.1 Flash)",
                        },
                        {
                            "value": "google/gemini-3.1-flash-image-preview",
                            "label": "Nano Banana (Gemini 3.1 Flash)",
                        },
                        {
                            "value": "google/gemini-3-pro-image-preview",
                            "label": "Nano Banana Pro (Gemini 3 Pro)",
                        },
                    ],
                },
            ],
        },
        {
            "type": "status",
            "target": "result",
            "idle": "Enter a prompt and press Generate.",
            "loading": "Generating image…",
            "success": "Done.",
            "error": "Generation failed.",
        },
        {
            "type": "markdown",
            "target": "result",
            "path": "error",
        },
        # image_url points to /media route — inline Content-Disposition, good for <img>
        {
            "type": "image",
            "target": "result",
            "path": "image_url",
            "label": "Generated image",
            "alt": "Generated image",
        },
        # download_url points to /download route — attachment Content-Disposition
        {
            "type": "file",
            "target": "result",
            "path": "download_url",
            "label": "Download image",
        },
    ],
}


# ---------------------------------------------------------------------------
# PluginAPI entry point
# ---------------------------------------------------------------------------


def register(api: Any) -> None:
    """PluginAPI v1 entry point. Called exactly once per load."""

    def _resolve_api_key() -> str:
        try:
            settings = api.get_settings(["OPENROUTER_API_KEY"]) or {}
        except Exception as exc:
            api.log("warning", f"nanobanana: get_settings raised: {exc}")
            return ""
        value = settings.get("OPENROUTER_API_KEY")
        return str(value or "").strip()

    def _resolve_state_dir() -> Optional[Path]:
        try:
            raw = api.get_state_dir()
        except Exception as exc:
            api.log("warning", f"nanobanana: get_state_dir raised: {exc}")
            return None
        if not raw:
            return None
        state_dir = Path(str(raw))
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            api.log("warning", f"nanobanana: mkdir state dir failed: {exc}")
            return None
        return state_dir

    def _build_media_url(image_id: str) -> str:
        """Inline preview URL — Content-Disposition: inline."""
        return (
            f"/api/extensions/nanobanana/media"
            f"?image_id={urllib.parse.quote(image_id, safe='')}"
        )

    def _build_download_url(image_id: str) -> str:
        """Attachment download URL — Content-Disposition: attachment."""
        return (
            f"/api/extensions/nanobanana/download"
            f"?image_id={urllib.parse.quote(image_id, safe='')}"
        )

    async def _route_generate(request: Request) -> JSONResponse:
        """POST /api/extensions/nanobanana/generate"""
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        prompt = payload.get("prompt", "")
        model = payload.get("model", _DEFAULT_MODEL)

        if not _normalize_prompt(prompt):
            return JSONResponse({"error": "prompt is empty"}, status_code=400)

        api_key = _resolve_api_key()
        state_dir = _resolve_state_dir()
        if state_dir is None:
            return JSONResponse(
                {"error": "nanobanana: state dir unavailable, cannot persist image"},
                status_code=500,
            )

        def _generate_and_persist() -> Dict[str, Any]:
            result = _generate_image(api_key, prompt, model)
            if "error" in result:
                return result
            try:
                out_path, image_id = _persist_image(
                    state_dir, result["mime"], result["image_bytes"], prompt
                )
            except OSError as exc:
                return {"error": f"nanobanana: failed to write image: {exc}"}
            return {
                "image_url": _build_media_url(image_id),
                "download_url": _build_download_url(image_id),
                "image_id": image_id,
                "file_size_bytes": out_path.stat().st_size,
                "model": result["model"],
                "text": result.get("text", ""),
            }

        result = await asyncio.to_thread(_generate_and_persist)
        status_code = 200 if "error" not in result else 502
        return JSONResponse(result, status_code=status_code)

    def _route_media(request: Request) -> "FileResponse | JSONResponse":
        """GET /api/extensions/nanobanana/media?image_id=...
        Serves inline (for <img src>).
        """
        image_id = (request.query_params.get("image_id") or "").strip()
        state_dir = _resolve_state_dir()
        if state_dir is None:
            return JSONResponse({"error": "nanobanana: state dir unavailable"}, status_code=500)
        resolved = _validate_image_id(image_id, state_dir)
        if resolved is None:
            return JSONResponse({"error": "image not found"}, status_code=404)
        ext = image_id.rsplit(".", 1)[-1]
        media_type = _MIME_BY_EXT.get(ext, "image/png")
        # inline — no download attribute, browser renders <img>
        return FileResponse(str(resolved), media_type=media_type)

    def _route_download(request: Request) -> "FileResponse | JSONResponse":
        """GET /api/extensions/nanobanana/download?image_id=...
        Serves as attachment (saves to disk with real filename).
        """
        image_id = (request.query_params.get("image_id") or "").strip()
        state_dir = _resolve_state_dir()
        if state_dir is None:
            return JSONResponse({"error": "nanobanana: state dir unavailable"}, status_code=500)
        resolved = _validate_image_id(image_id, state_dir)
        if resolved is None:
            return JSONResponse({"error": "image not found"}, status_code=404)
        ext = image_id.rsplit(".", 1)[-1]
        media_type = _MIME_BY_EXT.get(ext, "image/png")
        return FileResponse(
            str(resolved),
            media_type=media_type,
            filename=image_id,  # sets Content-Disposition: attachment
        )

    def _tool_generate(*, prompt: str = "", model: str = _DEFAULT_MODEL) -> str:
        """Agent-callable image generation tool."""
        api_key = _resolve_api_key()
        state_dir = _resolve_state_dir()
        if state_dir is None:
            return json.dumps(
                {"error": "nanobanana: state dir unavailable, cannot persist image"},
                ensure_ascii=False,
            )

        result = _generate_image(api_key, prompt, model)
        if "error" in result:
            return json.dumps(result, ensure_ascii=False)

        try:
            out_path, image_id = _persist_image(
                state_dir, result["mime"], result["image_bytes"], prompt
            )
        except OSError as exc:
            return json.dumps(
                {"error": f"nanobanana: failed to write image: {exc}"},
                ensure_ascii=False,
            )

        return json.dumps(
            {
                "file_path": str(out_path),
                "image_id": image_id,
                "image_url": _build_media_url(image_id),
                "download_url": _build_download_url(image_id),
                "bytes": out_path.stat().st_size,
                "mime": result["mime"],
                "model": result["model"],
                "prompt_used": str(prompt)[:200],
                "text": result.get("text", ""),
            },
            ensure_ascii=False,
        )

    api.register_tool(
        "generate",
        _tool_generate,
        description=(
            "Generate an image from a text prompt using an OpenRouter "
            "image-generation model (default: Nano Banana, "
            "google/gemini-3.1-flash-image-preview). Saves the image "
            "to the skill's private state directory and returns a JSON "
            "string with 'file_path', 'image_id', 'image_url', "
            "'download_url', 'bytes', 'mime', 'model' on success, or 'error'."
        ),
        schema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Text description of the image to generate.",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Optional OpenRouter image model ID. "
                        f"Default: {_DEFAULT_MODEL}. One of: "
                        + ", ".join(sorted(_ALLOWED_MODELS))
                        + "."
                    ),
                },
            },
            "required": ["prompt"],
        },
        timeout_sec=_TIMEOUT_SEC + 5,
    )
    api.register_route(
        "generate",
        _route_generate,
        methods=("POST",),
    )
    api.register_route(
        "media",
        _route_media,
        methods=("GET",),
    )
    api.register_route(
        "download",
        _route_download,
        methods=("GET",),
    )
    api.register_ui_tab(
        "widget",
        "Nano Banana",
        icon="image",
        render=_UI_RENDER,
    )
    api.log("info", "nanobanana: extension registered (routes: generate, media, download; tool; ui_tab)")


__all__ = ["register"]
