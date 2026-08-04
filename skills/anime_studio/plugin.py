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
import os
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("anime_studio")

# Stamped into every job record. The companion worker caches loaded Python, so a
# forgotten restart after a payload edit silently runs OLD code; comparing this
# against the payload version makes that structurally detectable instead of
# looking like a code bug.
#
# DERIVED from the manifest rather than typed, because a hand-maintained copy is
# correct only until someone bumps one of the two. It already drifted once
# (constant 3.0.0 vs manifest 3.1.0), which made a FRESH worker indistinguishable
# from a stale one and defeated the only mechanism above. Fail-safe: a missing or
# unparsable manifest must never stop the plugin from loading.
def _manifest_version(default: str = "unknown") -> str:
    try:
        head = (Path(__file__).parent / "SKILL.md").read_text(encoding="utf-8")[:4000]
        m = re.search(r"^version:\s*([^\s#]+)\s*$", head, re.MULTILINE)
        return m.group(1).strip() if m else default
    except Exception:
        return default


SKILL_VERSION = _manifest_version()

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
                        {"name": "quality_mode", "label": "Quality Mode", "type": "select", "options": ["low", "medium", "max"], "default": "medium"},
                        {"name": "budget_limit_usd", "label": "Budget limit USD (0 = auto)", "type": "number", "default": 0},
                        {"name": "resolution", "label": "Resolution", "type": "select", "options": ["480p", "720p", "1080p", "2K", "4K"], "default": "720p"},
                        {"name": "image_model", "label": "Image Generator", "type": "select", "options": ["gpt-image-2", "gpt-5-image", "gpt-5-image-mini", "nanobanana", "gemini-3-pro-image", "flux.2-pro", "flux.2-max", "seedream-4.5", "grok-imagine"], "default": "gpt-image-2"},
                        {"name": "video_model", "label": "Video Model", "type": "select", "options": ["bytedance/seedance-2.0", "bytedance/seedance-2.0-fast", "bytedance/seedance-1-5-pro", "minimax/hailuo-3", "google/veo-3.1", "google/veo-3.1-fast", "google/veo-3.1-lite", "minimax/hailuo-2.3", "kwaivgi/kling-v3.0-pro", "kwaivgi/kling-v3.0-std", "kwaivgi/kling-video-o1"], "default": "bytedance/seedance-2.0"},
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
                "num_scenes": {"type": "integer", "description": "Number of scenes (2-24); the effective cap depends on quality_mode (low=4, medium=8, max=24) and on duration (one scene per ~4s)", "default": 4},
                "quality_mode": {"type": "string", "description": "Quality mode: 'low' (1 video candidate, no judges, ~$3.2/scene ESTIMATE), 'medium' (2 candidates, 1 judge, $7.39/scene MEASURED), 'max' (3 candidates, 2 judges, ~$13/scene ESTIMATE)", "default": "medium"},
                "budget_limit_usd": {"type": "number", "description": "Hard budget limit in USD for this job; 0 = auto (estimate + 30% headroom)", "default": 0},
                "mood": {"type": "string", "description": "Overall mood", "default": "adventurous"},
                "resolution": {"type": "string", "description": "Video resolution: '480p', '720p', '1080p', '2K', or '4K' (reconciled against the chosen video model's live capability list; '2K' is currently the ONLY resolution minimax/hailuo-3 accepts)", "default": "720p"},
                "image_model": {"type": "string", "description": "Image generator: 'gpt-image-2', 'gpt-5-image', 'gpt-5-image-mini', 'nanobanana', 'gemini-3-pro-image', 'flux.2-pro', 'flux.2-max', 'seedream-4.5', 'grok-imagine'", "default": "gpt-image-2"},
                "video_model": {"type": "string", "description": "Video model: seedance-2.0/2.0-fast/1.5-pro, minimax/hailuo-3 (2K-only; flat $0.13/s + $0.04 per reference image, so cheaper than seedance at comparable quality even though seedance's headline $/s is lower), veo-3.1/fast/lite, hailuo-2.3, kling-v3.0-pro/std/o1", "default": "bytedance/seedance-2.0"},
            },
            "required": ["theme"],
        },
        timeout_sec=300,
    )

    # Long-running generation lives in a host-supervised companion process so it
    # survives the per-call out-of-process child that handles the route.
    api.register_companion_process("anime_worker")

    logger.info(f"Anime Studio v{SKILL_VERSION} extension registered (companion-backed pipeline)")


# ─── Job store (file-backed; shared with the companion) ─────────────


def _save_job(job) -> None:
    """Persist job.json ATOMICALLY (temp sibling + os.replace).

    The companion worker already writes this exact file atomically. A plain
    truncate-in-place write here meant a /status or /jobs poll racing the enqueue
    could read a half-written document and report "Job not found" for a job id
    that /generate had just returned — a transient phantom failure that looks
    like a lost job. Same primitive on both writers, so neither can expose a
    partial record to the other.
    """
    job_dir = _state_dir / "jobs" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    target = job_dir / "job.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(job.to_json(), encoding="utf-8")
    os.replace(tmp, target)


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
    except Exception as exc:
        # A corrupt/unreadable job record used to vanish silently; keep the
        # None contract for callers but preserve the cause in the log.
        logger.warning(
            "Failed to load job %s from %s: %s: %s",
            job_id, path, type(exc).__name__, exc,
        )
        return None


def _enqueue_job(settings, budget_state: dict | None = None):
    """Create a QUEUED job, persist it, and return it. The companion picks it up."""
    from .models import Job

    job = Job(settings=settings, created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    job.progress.verification_stats["skill_version"] = SKILL_VERSION
    job.progress.budget = dict(budget_state or {})
    _save_job(job)
    return job


def _status_payload(job) -> dict:
    p = job.progress
    raw_status = p.status.value if hasattr(p.status, "value") else str(p.status)
    # The host's async-job widget contract terminates on queued/running/done/error.
    # A finished-but-incomplete job is genuinely TERMINAL, so reporting the
    # out-of-vocabulary "partial" left the poller waiting forever on a job that
    # would never change again. It is projected to "done" for the poller, and the
    # honest outcome moves to `delivery_status` beside the existing
    # missing_scenes / unverified_scenes / partial_reasons detail — a projection
    # for one consumer's vocabulary, never a claim that a partial run was clean.
    return {
        "job_id": job.job_id,
        "status": "done" if raw_status == "partial" else raw_status,
        "delivery_status": raw_status,
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
        "budget": p.budget,
        "missing_scenes": p.missing_scenes,
        "unverified_scenes": p.unverified_scenes,
        "partial_reasons": p.partial_reasons,
        "created_at": job.created_at,
    }


# ─── HTTP Route Handlers ────────────────────────────────────────────


def _budget_state(estimate: dict, limit_usd: float, mode: str) -> dict:
    """Preflight budget snapshot stored on the job; the worker inherits it."""
    return {
        "limit_usd": limit_usd,
        "estimate_usd": estimate["estimate_usd"],
        "per_scene_usd": estimate["per_scene_usd"],
        "basis": estimate["basis"],
        "breakdown": estimate["breakdown"],
        "mode": mode,
        "spent_usd": 0.0,
        "hard_stop_reason": "",
    }


def _build_settings(body: dict):
    from .models import GenerationSettings
    from .modes import GLOBAL_DURATION_CAP_SEC, GLOBAL_SCENE_CAP, mode_config, normalize_mode

    try:
        duration_sec = int(body.get("duration_sec", 30))
    except (ValueError, TypeError):
        duration_sec = 30
    try:
        num_scenes = int(body.get("num_scenes", 4))
    except (ValueError, TypeError):
        num_scenes = 4
    quality_mode = normalize_mode(body.get("quality_mode"))
    mode_cfg = mode_config(quality_mode)
    try:
        budget_limit_usd = float(body.get("budget_limit_usd", 0.0))
    except (ValueError, TypeError):
        budget_limit_usd = 0.0
    if budget_limit_usd < 0:
        budget_limit_usd = 0.0
    # Duration cap rose from 60 to 240 WITH the scene cap: scenes are bounded by
    # duration_sec // 4 below, so a 20+ scene job is structurally unreachable at
    # 60s — the two caps are coupled and had to move together.
    duration_sec = min(GLOBAL_DURATION_CAP_SEC, max(10, duration_sec))
    # Every provider clip has a ~4s floor, so more scenes than duration/4 is a request
    # that cannot be satisfied: the old cap accepted 12 scenes in 10s and the result
    # overshot the requested duration by 5x with nothing saying so. The per-mode
    # scene_cap now bounds it further (low=4, medium=8, max=24), with
    # GLOBAL_SCENE_CAP as the outer bound regardless of mode.
    num_scenes = min(
        GLOBAL_SCENE_CAP,
        mode_cfg["scene_cap"],
        max(2, num_scenes),
        max(2, duration_sec // 4),
    )
    return GenerationSettings(
        theme=str(body.get("theme", "")).strip(),
        style=body.get("style", "modern anime"),
        duration_sec=duration_sec,
        num_scenes=num_scenes,
        mood=body.get("mood", "adventurous"),
        resolution=body.get("resolution", "720p"),
        aspect_ratio=body.get("aspect_ratio", "16:9"),
        video_model=body.get("video_model", "bytedance/seedance-2.0"),
        image_model=body.get("image_model", "gpt-image-2"),
        include_dialogue=body.get("include_dialogue", True),
        include_music=body.get("include_music", True),
        music_style=body.get("music_style", "orchestral cinematic"),
        quality_mode=quality_mode,
        budget_limit_usd=budget_limit_usd,
    )


async def handle_generate(request) -> Any:
    """Enqueue a new anime generation job (the companion process runs it)."""
    from starlette.responses import JSONResponse

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse(
            {"error": "JSON body must be an object"},
            status_code=400,
        )

    if not str(body.get("theme", "")).strip():
        return JSONResponse({"error": "theme is required"}, status_code=400)

    # Early reject if the provider key is not granted (the companion needs it too).
    keys = _api.get_settings(["OPENROUTER_API_KEY"])
    if not keys.get("OPENROUTER_API_KEY", ""):
        return JSONResponse(
            {"error": "OPENROUTER_API_KEY not configured or not granted"},
            status_code=403,
        )

    settings = _build_settings(body)

    # Budget preflight: the core cannot meter this skill's provider spend, so
    # refusing here is the only moment a too-expensive job costs nothing.
    from .budget import derive_limit_usd, estimate_job_usd, refusal_payload

    estimate = estimate_job_usd(
        settings.num_scenes, settings.quality_mode, include_music=settings.include_music
    )
    limit_usd = derive_limit_usd(estimate["estimate_usd"], settings.budget_limit_usd)
    if estimate["estimate_usd"] > limit_usd:
        return JSONResponse(refusal_payload(estimate, limit_usd), status_code=402)

    job = _enqueue_job(settings, _budget_state(estimate, limit_usd, settings.quality_mode))
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
    resolution: str = "720p",
    quality_mode: str = "medium",
    budget_limit_usd: float = 0.0,
) -> str:
    """Generate anime via the agent tool interface (enqueues a job)."""
    keys = _api.get_settings(["OPENROUTER_API_KEY"])
    if not keys.get("OPENROUTER_API_KEY", ""):
        return "Error: OPENROUTER_API_KEY not configured or not granted for this skill."
    if not theme:
        return "Error: theme parameter is required."

    settings = _build_settings({
        "theme": theme,
        "style": style,
        "duration_sec": duration_sec,
        "num_scenes": num_scenes,
        "mood": mood,
        "image_model": image_model,
        "video_model": video_model,
        "resolution": resolution,
        "quality_mode": quality_mode,
        "budget_limit_usd": budget_limit_usd,
    })

    # Same budget preflight as the HTTP route: refuse before anything is spent.
    from .budget import derive_limit_usd, estimate_job_usd, refusal_payload

    estimate = estimate_job_usd(
        settings.num_scenes, settings.quality_mode, include_music=settings.include_music
    )
    limit_usd = derive_limit_usd(estimate["estimate_usd"], settings.budget_limit_usd)
    if estimate["estimate_usd"] > limit_usd:
        refusal = refusal_payload(estimate, limit_usd)
        return f"Error: {refusal['error']} ({refusal['hint']})"

    job = _enqueue_job(settings, _budget_state(estimate, limit_usd, settings.quality_mode))

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
