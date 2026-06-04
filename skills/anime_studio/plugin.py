"""Anime Studio extension plugin for Ouroboros.

Execution model (Ouroboros >= v6.15.0): the generation pipeline runs in a
host-supervised **companion process** (`scripts/anime_worker.py`), not in the
route handlers. Routes are thin — `generate` enqueues a job to the file-backed
job store and returns immediately; `status`/`jobs`/`result`/`asset` read from
disk. The companion polls the store, runs each job, persists progress, and
relays live progress to the browser via the Host Service WS bridge. This keeps
the skill fully functional out-of-process, where route handlers run in
short-lived per-call children that cannot host a long-running pipeline thread.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("anime_studio")

_api = None
_state_dir: Path = Path(".")

# Subdirectories inside _state_dir the asset route may serve. Excludes the state
# dir root (control-plane files: review.json, enabled.json, grants.json, auth_token.json).
_ALLOWED_ASSET_SUBDIRS = ("assets", "output", "jobs")


def _is_path_confined(path: Path) -> bool:
    """Check if a resolved path is under the skill state directory."""
    try:
        resolved = path.resolve()
        state_resolved = _state_dir.resolve()
        resolved.relative_to(state_resolved)
        return True
    except (OSError, ValueError):
        return False


def _is_asset_path_allowed(path: Path) -> bool:
    """Check if a path is both confined AND inside an allowed asset subdirectory."""
    if not _is_path_confined(path):
        return False
    try:
        resolved = path.resolve()
        state_resolved = _state_dir.resolve()
        rel = resolved.relative_to(state_resolved)
        top_dir = rel.parts[0] if rel.parts else ""
        return top_dir in _ALLOWED_ASSET_SUBDIRS
    except (ValueError, OSError):
        return False


def register(api):
    """Register the Anime Studio extension."""
    global _api, _state_dir

    _api = api
    _state_dir = Path(api.get_state_dir())
    _state_dir.mkdir(parents=True, exist_ok=True)

    # HTTP routes (thin — enqueue + poll the file-backed job store).
    api.register_route("generate", handler=handle_generate, methods=("POST",))
    api.register_route("status", handler=handle_status, methods=("GET",))
    api.register_route("jobs", handler=handle_jobs, methods=("GET",))
    api.register_route("asset", handler=handle_asset, methods=("GET",))
    api.register_route("result", handler=handle_result, methods=("GET",))

    # WebSocket handler for real-time progress (companion relays via /ui/ws-message).
    api.register_ws_handler("studio_ping", handler=ws_ping)

    # UI tab (declarative widget — async job form + progress subscription).
    api.register_ui_tab(
        "studio",
        title="Anime Studio",
        icon="film",
        render={
            "kind": "declarative",
            "schema_version": 1,
            "components": [
                {
                    "type": "form",
                    "title": "\U0001f3ac Generate Anime",
                    "route": "generate",
                    "method": "POST",
                    "mode": "job",
                    "status_route": "status",
                    "fields": [
                        {"name": "theme", "label": "Theme / Story", "type": "textarea", "placeholder": "A young samurai discovers a magical sword in an ancient temple...", "required": True},
                        {"name": "style", "label": "Anime Style", "type": "select", "options": ["modern anime", "retro 90s anime", "chibi cute anime", "dark gothic anime", "watercolor anime", "Studio Ghibli style", "cyberpunk anime", "shounen action anime"], "default": "modern anime"},
                        {"name": "mood", "label": "Mood", "type": "select", "options": ["adventurous", "comedic", "dramatic", "melancholic", "mysterious", "romantic", "action-packed", "wholesome"], "default": "adventurous"},
                        {"name": "duration_sec", "label": "Duration (seconds)", "type": "number", "default": 30},
                        {"name": "num_scenes", "label": "Number of Scenes", "type": "number", "default": 4},
                        {"name": "image_model", "label": "Image Generator", "type": "select", "options": ["gpt-image-2", "gpt-5-image", "gpt-5-image-mini", "nanobanana", "gemini-3-pro-image", "flux.2-pro", "flux.2-max", "seedream-4.5", "grok-imagine"], "default": "gpt-image-2"},
                        {"name": "video_model", "label": "Video Model", "type": "select", "options": ["bytedance/seedance-2.0", "bytedance/seedance-2.0-fast", "bytedance/seedance-1-5-pro", "google/veo-3.1", "google/veo-3.1-fast", "google/veo-3.1-lite", "minimax/hailuo-2.3", "kwaivgi/kling-v3.0-pro", "kwaivgi/kling-v3.0-std", "kwaivgi/kling-video-o1"], "default": "bytedance/seedance-2.0"},
                        {"name": "music_style", "label": "Music Style", "type": "select", "options": ["orchestral cinematic", "electronic ambient", "acoustic guitar folk", "j-pop instrumental", "lo-fi hip hop beats", "epic battle drums"], "default": "orchestral cinematic"},
                    ],
                    "submit_label": "\U0001f3ac Generate Anime",
                },
                {
                    "type": "file",
                    "path": "result_download_url",
                    "label": "\U0001f3ac Download Video",
                    "condition_key": "result_download_url",
                    "filename": "anime_video.mp4",
                },
                {
                    "type": "subscription",
                    "event": "studio_progress",
                    "render": [
                        {"type": "progress", "value_key": "progress_pct", "label_key": "message"},
                        {"type": "gallery", "title": "Character Sheets", "items_key": "character_sheets", "item_type": "image", "route_prefix": "asset?path="},
                        {"type": "gallery", "title": "Keyframes", "items_key": "keyframes", "item_type": "image", "route_prefix": "asset?path="},
                        {"type": "key_value", "title": "Verification", "items_key": "verification_display", "condition_key": "has_verification"},
                        {"type": "key_value", "title": "Warnings", "items_key": "warnings_display", "condition_key": "has_warnings"},
                    ],
                },
            ],
        },
    )

    # Agent tool (enqueues a job; the companion runs it).
    api.register_tool(
        "generate_anime",
        handler=tool_generate_anime,
        description="Generate a short 2D anime cartoon with consistent characters, VLM-verified assets, storyboard, soundtrack, and video assembly",
        schema={
            "type": "object",
            "properties": {
                "theme": {"type": "string", "description": "Story theme/plot description"},
                "style": {"type": "string", "description": "Anime visual style", "default": "modern anime"},
                "duration_sec": {"type": "number", "description": "Total duration in seconds (10-60)", "default": 30},
                "num_scenes": {"type": "integer", "description": "Number of scenes (2-8)", "default": 4},
                "mood": {"type": "string", "description": "Overall mood", "default": "adventurous"},
                "image_model": {"type": "string", "description": "Image generator: 'gpt-image-2', 'gpt-5-image', 'gpt-5-image-mini', 'nanobanana', 'gemini-3-pro-image', 'flux.2-pro', 'flux.2-max', 'seedream-4.5', 'grok-imagine'", "default": "gpt-image-2"},
                "video_model": {"type": "string", "description": "Video model: seedance-2.0/1.5, veo-3.1/fast/lite, hailuo-2.3, kling-v3.0-pro/std/o1", "default": "bytedance/seedance-2.0"},
            },
            "required": ["theme"],
        },
        timeout_sec=300,
    )

    # Long-running generation lives in a host-supervised companion process so it
    # survives the per-call out-of-process child that handles the route.
    api.register_companion_process("anime_worker")

    logger.info("Anime Studio v2.12.0 extension registered (companion-backed pipeline)")


# ─── Job store (file-backed; shared with the companion) ─────────────


def _save_job(job) -> None:
    job_dir = _state_dir / "jobs" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "job.json").write_text(job.to_json(), encoding="utf-8")


def _load_job(job_id: str):
    from .models import Job

    if not job_id or not re.match(r"^[a-fA-F0-9\-]{1,64}$", job_id):
        return None
    path = _state_dir / "jobs" / job_id / "job.json"
    if not path.exists():
        path = _state_dir / "jobs" / f"{job_id}.json"  # legacy flat file
    if not path.exists():
        return None
    try:
        return Job.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _enqueue_job(settings):
    """Create a QUEUED job, persist it, and return it. The companion picks it up."""
    from .models import Job

    job = Job(settings=settings, created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"))
    _save_job(job)
    return job


def _status_payload(job) -> dict:
    p = job.progress
    return {
        "job_id": job.job_id,
        "status": p.status.value if hasattr(p.status, "value") else str(p.status),
        "phase": p.phase.value if hasattr(p.phase, "value") else str(p.phase),
        "progress_pct": p.progress_pct,
        "message": p.message,
        "character_sheets": p.character_sheets,
        "keyframes": p.keyframes,
        "video_clips": p.video_clips,
        "music_clips": p.music_clips,
        "final_video_url": p.final_video_url,
        "result_download_url": f"/api/extensions/anime_studio/result?job_id={job.job_id}" if p.final_video_url else "",
        "error": p.error,
        "warnings": p.warnings,
        "verification_stats": p.verification_stats,
        "created_at": job.created_at,
    }


# ─── HTTP Route Handlers ────────────────────────────────────────────


def _build_settings(body: dict):
    from .models import GenerationSettings

    try:
        duration_sec = int(body.get("duration_sec", 30))
    except (ValueError, TypeError):
        duration_sec = 30
    try:
        num_scenes = int(body.get("num_scenes", 4))
    except (ValueError, TypeError):
        num_scenes = 4
    return GenerationSettings(
        theme=str(body.get("theme", "")).strip(),
        style=body.get("style", "modern anime"),
        duration_sec=min(60, max(10, duration_sec)),
        num_scenes=min(8, max(2, num_scenes)),
        mood=body.get("mood", "adventurous"),
        resolution=body.get("resolution", "720p"),
        aspect_ratio=body.get("aspect_ratio", "16:9"),
        video_model=body.get("video_model", "bytedance/seedance-2.0"),
        image_model=body.get("image_model", "gpt-image-2"),
        include_dialogue=body.get("include_dialogue", True),
        include_music=body.get("include_music", True),
        music_style=body.get("music_style", "orchestral cinematic"),
    )


async def handle_generate(request) -> Any:
    """Enqueue a new anime generation job (the companion process runs it)."""
    from starlette.responses import JSONResponse

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    if not str(body.get("theme", "")).strip():
        return JSONResponse({"error": "theme is required"}, status_code=400)

    # Early reject if the provider key is not granted (the companion needs it too).
    keys = _api.get_settings(["OPENROUTER_API_KEY"])
    if not keys.get("OPENROUTER_API_KEY", ""):
        return JSONResponse(
            {"error": "OPENROUTER_API_KEY not configured or not granted"},
            status_code=403,
        )

    job = _enqueue_job(_build_settings(body))
    return JSONResponse({"job_id": job.job_id, "status": "queued"})


async def handle_status(request) -> Any:
    """Get job status — flat format for widget job polling (reads the job store)."""
    from starlette.responses import JSONResponse

    job_id = request.query_params.get("job_id", "")
    if not job_id:
        return JSONResponse({"error": "job_id required"}, status_code=400)
    job = _load_job(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse(_status_payload(job))


async def handle_jobs(request) -> Any:
    """List all jobs (scans the file-backed job store)."""
    from starlette.responses import JSONResponse

    jobs_list = []
    jobs_root = _state_dir / "jobs"
    if jobs_root.is_dir():
        for job_dir in sorted(jobs_root.iterdir()):
            if not job_dir.is_dir():
                continue
            job = _load_job(job_dir.name)
            if not job:
                continue
            p = job.progress
            jobs_list.append({
                "job_id": job.job_id,
                "status": p.status.value if hasattr(p.status, "value") else str(p.status),
                "phase": p.phase.value if hasattr(p.phase, "value") else str(p.phase),
                "progress_pct": p.progress_pct,
                "title": p.storyboard.title if p.storyboard else "",
                "created_at": job.created_at,
            })
    return JSONResponse({"jobs": jobs_list})


async def handle_asset(request) -> Any:
    """Serve a generated asset file (confined to assets/output/jobs subdirs)."""
    from starlette.responses import FileResponse, JSONResponse

    filepath = request.query_params.get("path", "")
    if not filepath:
        return JSONResponse({"error": "path required"}, status_code=400)
    path = Path(filepath)
    if not _is_asset_path_allowed(path):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(str(path))


async def handle_result(request) -> Any:
    """Serve the final video."""
    from starlette.responses import FileResponse, JSONResponse

    job_id = request.query_params.get("job_id", "")
    if not job_id:
        return JSONResponse({"error": "job_id required"}, status_code=400)
    job = _load_job(job_id)
    if not job or not job.progress.final_video_url:
        return JSONResponse({"error": "No result available"}, status_code=404)
    path = Path(job.progress.final_video_url)
    if not _is_asset_path_allowed(path):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not path.exists():
        return JSONResponse({"error": "Video file not found"}, status_code=404)
    return FileResponse(str(path), media_type="video/mp4", filename=f"{job_id}_anime.mp4")


# ─── WebSocket Handler ──────────────────────────────────────────────


async def ws_ping(data: dict) -> dict:
    return {"type": "pong", "ts": time.time()}


# ─── Agent Tool Handler ─────────────────────────────────────────────


async def tool_generate_anime(
    ctx,
    theme: str = "",
    style: str = "modern anime",
    duration_sec: float = 30,
    num_scenes: int = 4,
    mood: str = "adventurous",
    image_model: str = "gpt-image-2",
    video_model: str = "bytedance/seedance-2.0",
) -> str:
    """Generate anime via the agent tool interface (enqueues a job)."""
    keys = _api.get_settings(["OPENROUTER_API_KEY"])
    if not keys.get("OPENROUTER_API_KEY", ""):
        return "Error: OPENROUTER_API_KEY not configured or not granted for this skill."
    if not theme:
        return "Error: theme parameter is required."

    job = _enqueue_job(_build_settings({
        "theme": theme,
        "style": style,
        "duration_sec": duration_sec,
        "num_scenes": num_scenes,
        "mood": mood,
        "image_model": image_model,
        "video_model": video_model,
    }))

    return (
        f"Anime generation queued!\n"
        f"Job ID: {job.job_id}\n"
        f"Theme: {theme}\n"
        f"Style: {style} | Image: {image_model} | Video: {video_model}\n"
        f"Duration: {job.settings.duration_sec}s, {job.settings.num_scenes} scenes\n\n"
        f"The companion worker picks up the job within a couple of seconds. "
        f"Track progress in the Anime Studio widget tab, "
        f"or poll: GET /api/extensions/anime_studio/status?job_id={job.job_id}"
    )
