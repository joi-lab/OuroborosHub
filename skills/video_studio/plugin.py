"""Video Studio extension plugin for Ouroboros."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("video_studio")

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
    try:
        resolved = path.resolve()
        state_resolved = _state_dir.resolve()
        # Use is_relative_to (Python 3.9+) for cross-platform correctness
        try:
            resolved.relative_to(state_resolved)
            return True
        except ValueError:
            return False
    except (OSError, ValueError):
        return False


_ALLOWED_ASSET_SUBDIRS = ("assets", "output", "jobs")


def _is_asset_path_allowed(path: Path) -> bool:
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
    """Register the Video Studio extension."""
    global _api, _state_dir

    # Clear shutdown event on each load/reload so jobs aren't immediately cancelled
    # after a disable→enable cycle in the same process.
    _shutdown_event.clear()

    _api = api
    _state_dir = Path(api.get_state_dir())
    _state_dir.mkdir(parents=True, exist_ok=True)

    # Register HTTP routes
    api.register_route("generate", handler=handle_generate, methods=("POST",))
    api.register_route("status", handler=handle_status, methods=("GET",))
    api.register_route("jobs", handler=handle_jobs, methods=("GET",))
    api.register_route("asset", handler=handle_asset, methods=("GET",))
    api.register_route("result", handler=handle_result, methods=("GET",))

    # Register WebSocket handler
    api.register_ws_handler("studio_ping", handler=ws_ping)

    # Register UI tab (matches SKILL.md declarative widget)
    api.register_ui_tab(
        "video_studio",
        title="Video Studio",
        icon="video",
        render={
            "kind": "declarative",
            "schema_version": 1,
            "components": [
                {
                    "type": "form",
                    "title": "\U0001f3ac Generate Cinematic Video",
                    "route": "generate",
                    "method": "POST",
                    "mode": "job",
                    "status_route": "status",
                    "fields": [
                        {"name": "theme", "label": "Theme / Story", "type": "textarea",
                         "placeholder": "A detective investigates a mystery in rain-soaked neon-lit city...",
                         "required": True},
                        {"name": "style", "label": "Visual Style", "type": "select",
                         "options": ["photorealistic cinematic", "documentary realism", "noir thriller",
                                     "romantic drama", "sci-fi blockbuster", "action thriller",
                                     "period drama", "horror atmospheric"],
                         "default": "photorealistic cinematic"},
                        {"name": "mood", "label": "Mood", "type": "select",
                         "options": ["dramatic", "tense", "romantic", "melancholic", "triumphant",
                                     "mysterious", "comedic", "action-packed"],
                         "default": "dramatic"},
                        {"name": "duration_sec", "label": "Duration (seconds)", "type": "number", "default": 30},
                        {"name": "num_scenes", "label": "Number of Scenes", "type": "number", "default": 4},
                        {"name": "effort", "label": "Quality Effort", "type": "select",
                         "options": ["low", "regular", "max"], "default": "regular"},
                        {"name": "video_model", "label": "Video Model", "type": "select",
                         "options": ["bytedance/seedance-2.0", "bytedance/seedance-2.0-fast", "google/veo-3.1"],
                         "default": "bytedance/seedance-2.0"},
                        {"name": "music_style", "label": "Music Style", "type": "select",
                         "options": ["orchestral cinematic", "electronic ambient", "acoustic intimate",
                                     "jazz noir", "epic action", "minimalist tension"],
                         "default": "orchestral cinematic"},
                        {"name": "generate_audio", "label": "Generate Voice/Dialogue", "type": "select",
                         "options": ["true", "false"], "default": "true"},
                    ],
                    "submit_label": "\U0001f3ac Generate Video",
                },
                {
                    "type": "subscription",
                    "event": "video_studio_progress",
                    "render": [
                        {"type": "progress", "value_key": "progress_pct", "label_key": "message"},
                        {"type": "gallery", "title": "Character References",
                         "items_key": "character_sheets", "item_type": "image", "route_prefix": "asset?path="},
                        {"type": "gallery", "title": "Scene Keyframes",
                         "items_key": "keyframes", "item_type": "image", "route_prefix": "asset?path="},
                        {"type": "key_value", "title": "Quality Scores",
                         "items_key": "quality_display", "condition_key": "has_quality"},
                        {"type": "key_value", "title": "Warnings",
                         "items_key": "warnings_display", "condition_key": "has_warnings"},
                    ],
                },
            ],
        },
    )

    # Register agent tool
    api.register_tool(
        "generate_video",
        handler=tool_generate_video,
        description=(
            "Generate a photorealistic cinematic video with Hollywood-grade quality, "
            "Gemini multimodal AV QC, parallel best-of-N candidates, "
            "effort-based quality control, and voice/dialogue synthesis"
        ),
        schema={
            "type": "object",
            "properties": {
                "theme": {"type": "string", "description": "Story theme/plot description"},
                "style": {"type": "string", "description": "Visual style", "default": "photorealistic cinematic"},
                "mood": {"type": "string", "description": "Overall mood", "default": "dramatic"},
                "duration_sec": {"type": "number", "description": "Total duration in seconds (10-120)", "default": 30},
                "num_scenes": {"type": "integer", "description": "Number of scenes (2-8)", "default": 4},
                "effort": {"type": "string", "description": "Quality effort: low/regular/max", "default": "regular"},
                "video_model": {"type": "string", "description": "Video model", "default": "bytedance/seedance-2.0"},
                "music_style": {"type": "string", "description": "Music style", "default": "orchestral cinematic"},
                "generate_audio": {"type": "boolean", "description": "Generate voice/dialogue via Seedance", "default": True},
            },
            "required": ["theme"],
        },
        timeout_sec=300,
    )

    api.on_unload(_cleanup)

    logger.info("Video Studio v1.2.0 extension registered")


_CLEANUP_JOIN_TIMEOUT_SEC = 30  # All threads are daemon threads; processes are killed before join


def _cleanup():
    """Cleanup all background resources on extension unload."""
    global _jobs
    _shutdown_event.set()

    with _active_loops_lock:
        for loop in _active_loops:
            try:
                if loop.is_running():
                    for task in asyncio.all_tasks(loop):
                        loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        _active_loops.clear()

    with _active_pipelines_lock:
        for pipeline in _active_pipelines:
            try:
                pipeline.kill_active_processes()
            except Exception:
                pass
        _active_pipelines.clear()

    with _active_threads_lock:
        threads_to_join = list(_active_threads)

    for t in threads_to_join:
        t.join(timeout=_CLEANUP_JOIN_TIMEOUT_SEC)

    with _active_threads_lock:
        still_alive = [t.name for t in _active_threads if t.is_alive()]
        _active_threads.clear()
        if still_alive:
            logger.warning(
                f"Cleanup: {len(still_alive)} daemon thread(s) did not join "
                f"within {_CLEANUP_JOIN_TIMEOUT_SEC}s: {still_alive}"
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


async def handle_generate(request) -> Any:
    """Start a new video generation job."""
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

    import shutil
    missing_bins = [b for b in ("ffmpeg", "ffprobe") if not shutil.which(b)]
    if missing_bins:
        return JSONResponse(
            {"error": f"Required binaries not found on PATH: {', '.join(missing_bins)}. "
                      "Install ffmpeg (e.g. `brew install ffmpeg`) and ensure it is on PATH."},
            status_code=503,
        )

    # Cheap OpenRouter key validity preflight (no cost, fast) — async to avoid blocking the event loop
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=8.0) as _ac:
            _r = await _ac.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {or_key}"},
            )
        if _r.status_code == 401:
            return JSONResponse({"error": "OPENROUTER_API_KEY is invalid or revoked"}, status_code=403)
    except Exception:
        pass  # Network down or timeout — proceed optimistically, pipeline will surface the error

    from .models import GenerationSettings, Job

    generate_audio_raw = body.get("generate_audio", "true")
    if isinstance(generate_audio_raw, str):
        generate_audio = generate_audio_raw.lower() == "true"
    else:
        generate_audio = bool(generate_audio_raw)

    try:
        duration_sec = min(120, max(10, float(body.get("duration_sec", 30) or 30)))
    except (TypeError, ValueError):
        duration_sec = 30.0
    try:
        num_scenes = min(8, max(2, int(body.get("num_scenes", 4) or 4)))
    except (TypeError, ValueError):
        num_scenes = 4

    settings = GenerationSettings(
        theme=theme,
        style=body.get("style", "photorealistic cinematic"),
        duration_sec=duration_sec,
        num_scenes=num_scenes,
        mood=body.get("mood", "dramatic"),
        video_model=body.get("video_model", "bytedance/seedance-2.0"),
        music_style=body.get("music_style", "orchestral cinematic"),
        effort=body.get("effort", "regular"),
        generate_audio=generate_audio,
        include_dialogue=generate_audio,
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
        name=f"video_studio_{job.job_id}",
    )
    _track_thread(thread)
    thread.start()

    return JSONResponse({"job_id": job.job_id, "status": "queued"})


async def handle_status(request) -> Any:
    """Get job status."""
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
    quality_reports = p.quality_reports or []
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
        "error": p.error,
        "warnings": p.warnings,
        "verification_stats": p.verification_stats,
        "quality_reports": quality_reports,
        "created_at": job.created_at,
    })


async def handle_jobs(request) -> Any:
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
    """Serve a generated asset file."""
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

    return FileResponse(str(path), media_type="video/mp4")


# ─── WebSocket Handler ──────────────────────────────────────────────


async def ws_ping(data: dict) -> dict:
    return {"type": "pong", "ts": time.time()}


# ─── Agent Tool Handler ─────────────────────────────────────────────


async def tool_generate_video(
    ctx,
    theme: str = "",
    style: str = "photorealistic cinematic",
    mood: str = "dramatic",
    duration_sec: float = 30,
    num_scenes: int = 4,
    effort: str = "regular",
    video_model: str = "bytedance/seedance-2.0",
    music_style: str = "orchestral cinematic",
    generate_audio: bool = True,
) -> str:
    """Generate a cinematic photorealistic video via the agent tool interface."""
    keys = _api.get_settings(["OPENROUTER_API_KEY"])
    or_key = keys.get("OPENROUTER_API_KEY", "")
    if not or_key:
        return "Error: OPENROUTER_API_KEY not configured or not granted for this skill."

    if not theme:
        return "Error: theme parameter is required."

    import shutil
    missing_bins = [b for b in ("ffmpeg", "ffprobe") if not shutil.which(b)]
    if missing_bins:
        return (f"Error: Required binaries not found on PATH: {', '.join(missing_bins)}. "
                "Install ffmpeg (e.g. `brew install ffmpeg`) and ensure it is on PATH.")

    # Cheap OpenRouter key validity preflight (no cost, fast) — async to avoid blocking the event loop
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=8.0) as _ac:
            _r = await _ac.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {or_key}"},
            )
        if _r.status_code == 401:
            return "Error: OPENROUTER_API_KEY is invalid or revoked."
    except Exception:
        pass  # Network down or timeout — proceed optimistically

    from .models import GenerationSettings, Job

    settings = GenerationSettings(
        theme=theme,
        style=style,
        mood=mood,
        duration_sec=min(120, max(10, duration_sec)),
        num_scenes=min(8, max(2, num_scenes)),
        video_model=video_model,
        music_style=music_style,
        effort=effort if effort in ("low", "regular", "max") else "regular",
        generate_audio=generate_audio,
        include_dialogue=generate_audio,
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
        name=f"video_studio_{job.job_id}",
    )
    _track_thread(thread)
    thread.start()

    return (
        f"Video generation started!\n"
        f"Job ID: {job.job_id}\n"
        f"Theme: {theme}\n"
        f"Style: {style} | Mood: {mood} | Effort: {effort}\n"
        f"Video model: {video_model} | Audio: {generate_audio}\n"
        f"Duration: {settings.duration_sec}s, {settings.num_scenes} scenes\n\n"
        f"Track progress in the Video Studio widget tab, "
        f"or poll: GET /api/extensions/video_studio/status?job_id={job.job_id}"
    )


# ─── Pipeline Runner ────────────────────────────────────────────────


def _run_pipeline_thread(job, api_key: str):
    """Run the video pipeline in a background thread."""
    # Ensure isolated deps (.ouroboros_env) are on sys.path in this thread.
    # extension_isolated_deps scope is context-local; background threads need
    # an explicit path injection so PIL and other deps are importable.
    # We use _state_dir.parent.parent to find the real skill payload dir
    # (state_dir is ~/Ouroboros/data/state/skills/video_studio/, skill payload
    # is ~/Ouroboros/data/skills/external/video_studio/) rather than __file__
    # (which points at the staged __extension_imports copy).
    import sys
    from pathlib import Path as _Path
    # _state_dir is set in register() from api.get_state_dir()
    # _state_dir = ~/Ouroboros/data/state/skills/video_studio
    # Navigate up 3 levels: video_studio -> skills -> state -> data
    _data_root = _state_dir.parent.parent.parent  # ~/Ouroboros/data/
    # Discover the real skill payload dir without hardcoding bucket or name.
    # _state_dir is data/state/skills/<name>/ — derive name from it.
    _skill_name = _state_dir.name
    _skill_payload = None
    for _bucket in ("external", "clawhub", "ouroboroshub"):
        _candidate = _data_root / "skills" / _bucket / _skill_name
        if (_candidate / ".ouroboros_env").exists():
            _skill_payload = _candidate
            break
    if _skill_payload is None:
        # Fallback: try any bucket that contains the skill dir
        for _bucket in ("external", "clawhub", "ouroboroshub"):
            _candidate = _data_root / "skills" / _bucket / _skill_name
            if _candidate.exists():
                _skill_payload = _candidate
                break
    _site_pkgs = list((_skill_payload / ".ouroboros_env" / "python" / "lib").glob("python*/site-packages")) if _skill_payload else []
    for _sp in _site_pkgs:
        _sp_str = str(_sp)
        if _sp_str not in sys.path:
            sys.path.insert(0, _sp_str)

    from .api_client import OpenRouterClient
    from .pipeline import Pipeline

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _track_loop(loop)

    pipeline = None
    try:
        if _shutdown_event.is_set():
            return

        job_dir = _state_dir / "jobs" / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        client = OpenRouterClient(api_key=api_key, state_dir=job_dir)
        pipeline = Pipeline(
            client=client,
            state_dir=job_dir,
            on_progress=_on_progress,
            shutdown_event=_shutdown_event,
            lessons_dir=_state_dir,
        )
        _track_pipeline(pipeline)
        loop.run_until_complete(pipeline.run(job))
    except asyncio.CancelledError:
        from .models import JobStatus
        job.progress.status = JobStatus.ERROR
        job.progress.error = "Cancelled (extension unloading)"
        job.progress.message = "Generation cancelled — extension unloaded"
        _on_progress(job)
    except Exception as e:
        logger.exception("Pipeline thread error")
        job.progress.status = JobStatus.ERROR
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
    """Called by pipeline on each update. Broadcasts WS + saves state."""
    _save_job(job)
    try:
        if _api and not _shutdown_event.is_set():
            p = job.progress
            stats = p.verification_stats or {}
            quality_reports = p.quality_reports or []
            quality_display = []
            for r in quality_reports:
                if isinstance(r, dict):
                    si = r.get("scene_index", "?")
                    def _fmt(v, default=0.0):
                        try:
                            return f"{float(v):.1f}"
                        except (TypeError, ValueError):
                            return "?"
                    quality_display.append({
                        "key": f"Scene {si}",
                        "value": (
                            f"visual={_fmt(r.get('visual_score'))} "
                            f"audio={_fmt(r.get('audio_score'))} "
                            f"av_sync={_fmt(r.get('av_sync_score'))}"
                        ),
                    })
            _api.send_ws_message("video_studio_progress", {
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
                "warnings": p.warnings,
                "has_warnings": bool(p.warnings),
                "warnings_display": [{"key": f"\u26a0\ufe0f {i+1}", "value": w} for i, w in enumerate(p.warnings)] if p.warnings else [],
                "has_quality": bool(quality_display),
                "quality_display": quality_display,
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

    # Sanitize job_id to alphanumeric + dash/underscore to prevent path traversal
    job_id = re.sub(r"[^a-zA-Z0-9_-]", "", job_id)
    if not job_id:
        return None

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
