"""Anime Studio extension plugin for Ouroboros."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("anime_studio")

# Module-level state
_jobs: dict[str, Any] = {}
_jobs_lock = threading.Lock()
_active_threads: list[threading.Thread] = []
_active_threads_lock = threading.Lock()
_active_pipelines: list[Any] = []
_active_pipelines_lock = threading.Lock()
_active_loops: list[asyncio.AbstractEventLoop] = []
_active_loops_lock = threading.Lock()
_shutdown_event = threading.Event()
_api = None
_state_dir: Path = Path(".")


def _is_path_confined(path: Path) -> bool:
    """Check if a resolved path is under the skill state directory."""
    try:
        resolved = path.resolve()
        state_resolved = _state_dir.resolve()
        resolved.relative_to(state_resolved)
        return True
    except (OSError, ValueError):
        return False


# Subdirectories inside _state_dir that the asset route is allowed to serve.
# This excludes the state dir root (which contains control-plane files like
# review.json, enabled.json, grants.json, auth_token.json).
_ALLOWED_ASSET_SUBDIRS = ("assets", "output", "jobs")


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

    # Clear module-level state so reloads start clean (critical: _shutdown_event
    # is set permanently by _cleanup; without clearing it, pipeline threads exit
    # immediately on reload).
    _shutdown_event.clear()
    with _jobs_lock:
        _jobs.clear()
    with _active_threads_lock:
        _active_threads.clear()
    with _active_pipelines_lock:
        _active_pipelines.clear()
    with _active_loops_lock:
        _active_loops.clear()
    _api = api
    _state_dir = Path(api.get_state_dir())
    _state_dir.mkdir(parents=True, exist_ok=True)

    # Register HTTP routes
    api.register_route("generate", handler=handle_generate, methods=("POST",))
    api.register_route("status", handler=handle_status, methods=("GET",))
    api.register_route("jobs", handler=handle_jobs, methods=("GET",))
    api.register_route("asset", handler=handle_asset, methods=("GET",))
    api.register_route("result", handler=handle_result, methods=("GET",))

    # Register WebSocket handler for real-time progress
    api.register_ws_handler("studio_ping", handler=ws_ping)

    # Register UI tab (declarative widget — async job form + subscription)
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

    # Register agent tool
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

    # Cleanup handler
    api.on_unload(_cleanup)

    logger.info("Anime Studio v2.10.0 extension registered")


# Thread join timeout — long enough for pipeline to notice shutdown_event between phases
_CLEANUP_JOIN_TIMEOUT_SEC = 10


def _cleanup():
    """Cleanup all background resources on extension unload.

    Guarantees all tracked subprocesses and threads are terminated:
    1. Sets shutdown_event so pipeline threads exit between phases
    2. Cancels all tasks in tracked event loops (interrupts in-flight HTTP)
    3. Kills tracked pipeline subprocesses (ffmpeg inherits the host's
       process group, so the host's panic kill also reaps them)
    4. Joins each pipeline thread with timeout
    5. shutdown_event stays set permanently after cleanup
    """
    global _jobs
    _shutdown_event.set()

    # Cancel all tasks in tracked event loops — interrupts in-flight HTTP calls
    with _active_loops_lock:
        for loop in _active_loops:
            try:
                if loop.is_running():
                    for task in asyncio.all_tasks(loop):
                        loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        _active_loops.clear()

    # Kill tracked pipeline subprocesses
    with _active_pipelines_lock:
        for pipeline in _active_pipelines:
            try:
                pipeline.kill_active_processes()
            except Exception:
                pass
        _active_pipelines.clear()

    # Join threads — cooperative shutdown via shutdown_event + loop cancellation.
    # The shutdown_event + loop task cancellation above should cause threads to
    # exit promptly. We join with a timeout as a safety net.
    with _active_threads_lock:
        threads_to_join = list(_active_threads)

    for t in threads_to_join:
        t.join(timeout=_CLEANUP_JOIN_TIMEOUT_SEC)

    # All threads are daemon=True so they will be terminated when the host
    # process exits. After the join timeout, any still-alive threads have
    # already had their event loops cancelled and pipelines killed above,
    # so they are blocked on I/O that will never complete. Log for visibility.
    with _active_threads_lock:
        still_alive = [t.name for t in _active_threads if t.is_alive()]
        _active_threads.clear()
        if still_alive:
            logger.warning(
                f"Cleanup: {len(still_alive)} daemon thread(s) did not join "
                f"within {_CLEANUP_JOIN_TIMEOUT_SEC}s (will die with process "
                f"as daemon threads): {still_alive}"
            )

    with _jobs_lock:
        _jobs.clear()


def _track_thread(thread: threading.Thread):
    with _active_threads_lock:
        _active_threads[:] = [t for t in _active_threads if t.is_alive()]
        _active_threads.append(thread)


def _track_loop(loop: asyncio.AbstractEventLoop):
    with _active_loops_lock:
        _active_loops.append(loop)


def _untrack_loop(loop: asyncio.AbstractEventLoop):
    with _active_loops_lock:
        if loop in _active_loops:
            _active_loops.remove(loop)


def _track_pipeline(pipeline):
    with _active_pipelines_lock:
        _active_pipelines.append(pipeline)


def _untrack_pipeline(pipeline):
    with _active_pipelines_lock:
        if pipeline in _active_pipelines:
            _active_pipelines.remove(pipeline)


# ─── HTTP Route Handlers ────────────────────────────────────────────


async def handle_generate(request) -> dict:
    """Start a new anime generation job."""
    from starlette.responses import JSONResponse

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    theme = body.get("theme", "").strip()
    if not theme:
        return JSONResponse({"error": "theme is required"}, status_code=400)

    keys = _api.get_settings(["OPENROUTER_API_KEY"])
    or_key = keys.get("OPENROUTER_API_KEY", "")
    if not or_key:
        return JSONResponse(
            {"error": "OPENROUTER_API_KEY not configured or not granted"},
            status_code=403,
        )

    from .models import GenerationSettings, Job

    # Form fields arrive as strings from HTML forms — cast numeric values.
    try:
        duration_sec = int(body.get("duration_sec", 30))
    except (ValueError, TypeError):
        duration_sec = 30
    try:
        num_scenes = int(body.get("num_scenes", 4))
    except (ValueError, TypeError):
        num_scenes = 4

    settings = GenerationSettings(
        theme=theme,
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

    job = Job(settings=settings, created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"))

    with _jobs_lock:
        _jobs[job.job_id] = job

    _save_job(job)

    thread = threading.Thread(
        target=_run_pipeline_thread,
        args=(job, or_key),
        daemon=True,
        name=f"anime_studio_{job.job_id}",
    )
    _track_thread(thread)
    thread.start()

    return JSONResponse({"job_id": job.job_id, "status": "queued"})


async def handle_status(request) -> dict:
    """Get job status — flat format for widget job polling."""
    from starlette.responses import JSONResponse

    job_id = request.query_params.get("job_id", "")
    if not job_id:
        return JSONResponse({"error": "job_id required"}, status_code=400)

    with _jobs_lock:
        job = _jobs.get(job_id)

    if not job:
        job = _load_job(job_id)
        if not job:
            return JSONResponse({"error": "Job not found"}, status_code=404)

    p = job.progress
    return JSONResponse({
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
    })


async def handle_jobs(request) -> dict:
    """List all jobs."""
    from starlette.responses import JSONResponse

    with _jobs_lock:
        jobs_list = [
            {
                "job_id": j.job_id,
                "status": j.progress.status.value if hasattr(j.progress.status, "value") else str(j.progress.status),
                "phase": j.progress.phase.value if hasattr(j.progress.phase, "value") else str(j.progress.phase),
                "progress_pct": j.progress.progress_pct,
                "title": j.progress.storyboard.title if j.progress.storyboard else "",
                "created_at": j.created_at,
            }
            for j in _jobs.values()
        ]

    return JSONResponse({"jobs": jobs_list})


async def handle_asset(request) -> Any:
    """Serve a generated asset file.

    Only serves files from the skill's own assets/output/jobs subdirectories.
    Rejects paths that resolve to the state dir root or other locations,
    preventing access to control-plane files (review.json, enabled.json,
    grants.json, auth_token.json).
    """
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

    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        job = _load_job(job_id)

    if not job or not job.progress.final_video_url:
        return JSONResponse({"error": "No result available"}, status_code=404)

    path = Path(job.progress.final_video_url)
    if not _is_asset_path_allowed(path):
        return JSONResponse({"error": "Access denied"}, status_code=403)
    if not path.exists():
        return JSONResponse({"error": "Video file not found"}, status_code=404)

    return FileResponse(
        str(path),
        media_type="video/mp4",
        filename=f"{job_id}_anime.mp4",
    )


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
    """Generate anime via the agent tool interface."""
    keys = _api.get_settings(["OPENROUTER_API_KEY"])
    or_key = keys.get("OPENROUTER_API_KEY", "")
    if not or_key:
        return "Error: OPENROUTER_API_KEY not configured or not granted for this skill."

    if not theme:
        return "Error: theme parameter is required."

    from .models import GenerationSettings, Job

    settings = GenerationSettings(
        theme=theme,
        style=style,
        duration_sec=min(60, max(10, duration_sec)),
        num_scenes=min(8, max(2, num_scenes)),
        mood=mood,
        image_model=image_model,
        video_model=video_model,
        include_dialogue=True,
        include_music=True,
    )

    job = Job(settings=settings, created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"))

    with _jobs_lock:
        _jobs[job.job_id] = job

    _save_job(job)

    thread = threading.Thread(
        target=_run_pipeline_thread,
        args=(job, or_key),
        daemon=True,
        name=f"anime_studio_{job.job_id}",
    )
    _track_thread(thread)
    thread.start()

    return (
        f"Anime generation started!\n"
        f"Job ID: {job.job_id}\n"
        f"Theme: {theme}\n"
        f"Style: {style} | Image: {image_model} | Video: {video_model}\n"
        f"Duration: {settings.duration_sec}s, {settings.num_scenes} scenes\n\n"
        f"Track progress in the Anime Studio widget tab, "
        f"or poll status via: GET /api/extensions/anime_studio/status?job_id={job.job_id}"
    )


# ─── Pipeline Runner ────────────────────────────────────────────────


def _run_pipeline_thread(job, api_key: str):
    """Run the pipeline in a background thread.

    The thread's event loop is tracked so _cleanup() can cancel all in-flight
    tasks (including blocked HTTP calls) from the main thread. This ensures
    that unload/panic terminates work promptly rather than waiting for HTTP
    timeouts to expire.
    """
    from .api_client import OpenRouterClient
    from .models import JobPhase, JobStatus
    from .pipeline import Pipeline

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _track_loop(loop)

    pipeline = None
    try:
        if _shutdown_event.is_set():
            return

        # Per-job directory isolation: each job gets its own assets/output/job.json
        # so parallel jobs and retries never collide on filenames.
        job_dir = _state_dir / "jobs" / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        client = OpenRouterClient(api_key=api_key, state_dir=job_dir)
        pipeline = Pipeline(
            client=client,
            state_dir=job_dir,
            on_progress=_on_progress,
            shutdown_event=_shutdown_event,
            lessons_dir=_state_dir,  # shared across jobs for progressive learning
            ffmpeg_cache_dir=_state_dir,  # shared across jobs — avoid re-downloading per job
        )
        _track_pipeline(pipeline)
        loop.run_until_complete(pipeline.run(job))
    except asyncio.CancelledError:
        job.progress.status = JobStatus.ERROR
        job.progress.phase = JobPhase.ERROR
        job.progress.error = "Cancelled (extension unloading)"
        job.progress.message = "Generation cancelled — extension unloaded"
    except Exception as e:
        logger.exception("Pipeline thread error")
        job.progress.status = JobStatus.ERROR
        job.progress.phase = JobPhase.ERROR
        job.progress.error = str(e)
        job.progress.message = f"Pipeline crashed: {e}"
        _on_progress(job)
    finally:
        if pipeline:
            _untrack_pipeline(pipeline)
        _untrack_loop(loop)
        _save_job(job)
        loop.close()


def _on_progress(job):
    """Called by pipeline on each progress update. Broadcasts WS + saves state."""
    _save_job(job)
    try:
        if _api and not _shutdown_event.is_set():
            p = job.progress
            stats = p.verification_stats or {}
            _api.send_ws_message("studio_progress", {
                "job_id": job.job_id,
                "phase": p.phase.value if hasattr(p.phase, "value") else str(p.phase),
                "status": p.status.value if hasattr(p.status, "value") else str(p.status),
                "progress_pct": p.progress_pct,
                "message": p.message,
                "character_sheets": p.character_sheets,
                "keyframes": p.keyframes,
                "video_clips": p.video_clips,
                "music_clips": p.music_clips,
                "final_video_url": p.final_video_url,
                "result_download_url": f"/api/extensions/anime_studio/result?job_id={job.job_id}" if p.final_video_url else "",
                "warnings": p.warnings,
                "has_warnings": bool(p.warnings),
                "warnings_display": [{"key": f"\u26a0\ufe0f {i+1}", "value": w} for i, w in enumerate(p.warnings)] if p.warnings else [],
                "has_verification": bool(stats),
                "verification_display": [
                    {"key": "\u2705 Passed", "value": str(stats.get("passed", 0))},
                    {"key": "\U0001f504 Retried", "value": str(stats.get("retried", 0))},
                    {"key": "\u274c Failed", "value": str(stats.get("failed", 0))},
                ] if stats else [],
            })
    except Exception as e:
        logger.debug(f"WS broadcast failed: {e}")


# ─── Job Persistence ────────────────────────────────────────────────


def _save_job(job):
    job_dir = _state_dir / "jobs" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / "job.json"
    path.write_text(job.to_json(), encoding="utf-8")


def _load_job(job_id: str):
    import re
    from .models import Job

    # Validate job_id to prevent path traversal (only hex/dash chars allowed)
    if not job_id or not re.match(r'^[a-fA-F0-9\-]{1,64}$', job_id):
        return None

    # Try new per-job directory first, fall back to legacy flat file
    path = _state_dir / "jobs" / job_id / "job.json"
    if not path.exists():
        path = _state_dir / "jobs" / f"{job_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Job.from_dict(data)
    except Exception:
        return None
