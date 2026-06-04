"""Anime Studio companion worker.

The host (server process) spawns and supervises this long-lived process. It owns
the generation pipeline: it polls the skill state dir for QUEUED jobs, runs each
job's pipeline to completion, persists job state to disk, and relays progress to
browser clients through the loopback Host Service `POST /ui/ws-message` bridge.

Why a companion: out-of-process (isolated-dep) extensions dispatch tools/routes in
short-lived per-call children, so a generation thread started inside a route would
die the moment the HTTP response returns. A host-supervised companion is the
persistent execution surface for long-running work (Ouroboros >= v6.15.0).

Env provided by the host:
- OUROBOROS_SKILL_STATE_DIR : the skill's private state dir (job store lives here)
- HOST_SERVICE_URL / HOST_SERVICE_TOKEN : loopback Host Service for WS relay
- OPENROUTER_API_KEY : granted provider key (via manifest env_from_settings)
"""
from __future__ import annotations

import asyncio
import importlib
import ipaddress
import json
import os
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

STATE_DIR = Path(os.environ.get("OUROBOROS_SKILL_STATE_DIR") or ".").resolve()
JOBS_DIR = STATE_DIR / "jobs"
HOST_SERVICE_URL = (os.environ.get("HOST_SERVICE_URL") or "http://127.0.0.1:8767").rstrip("/")
HOST_SERVICE_TOKEN = os.environ.get("HOST_SERVICE_TOKEN", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
POLL_INTERVAL_SEC = 2.0

_shutdown = threading.Event()


def _is_loopback(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").strip()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# Import the skill package modules. pipeline.py uses package-relative imports
# (`from .models import ...`), so they must be imported as `<pkg>.pipeline` with
# the skill dir's parent on sys.path. cwd is the skill payload dir; the package
# name is the skill dir's name (e.g. "anime_studio").
_SKILL_DIR = Path(__file__).resolve().parent.parent
_PKG = _SKILL_DIR.name
if str(_SKILL_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_SKILL_DIR.parent))

_models = importlib.import_module(f"{_PKG}.models")
_api_client = importlib.import_module(f"{_PKG}.api_client")
_pipeline_mod = importlib.import_module(f"{_PKG}.pipeline")

Job = _models.Job
JobStatus = _models.JobStatus
JobPhase = _models.JobPhase
OpenRouterClient = _api_client.OpenRouterClient
Pipeline = _pipeline_mod.Pipeline

_HOST_BRIDGE_OK = bool(HOST_SERVICE_TOKEN) and _is_loopback(HOST_SERVICE_URL)


def _status_value(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


def _post_ws(message_type: str, data: dict) -> None:
    """Best-effort WS relay to browser clients via the loopback Host Service."""
    if not _HOST_BRIDGE_OK:
        return
    body = json.dumps({"message_type": message_type, "data": data}).encode("utf-8")
    request = urllib.request.Request(
        f"{HOST_SERVICE_URL}/ui/ws-message",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "x-skill-token": HOST_SERVICE_TOKEN},
    )
    try:
        with urllib.request.urlopen(request, timeout=5):  # noqa: S310 - loopback Host Service
            return
    except Exception:
        return


def _save_job(job) -> None:
    job_dir = JOBS_DIR / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "job.json").write_text(job.to_json(), encoding="utf-8")


def _on_progress(job) -> None:
    """Persist job state and relay a namespaced progress event to the browser."""
    _save_job(job)
    p = job.progress
    stats = p.verification_stats or {}
    _post_ws("studio_progress", {
        "job_id": job.job_id,
        "phase": p.phase.value if hasattr(p.phase, "value") else str(p.phase),
        "status": _status_value(p.status),
        "progress_pct": p.progress_pct,
        "message": p.message,
        "character_sheets": p.character_sheets,
        "keyframes": p.keyframes,
        "video_clips": p.video_clips,
        "music_clips": p.music_clips,
        "final_video_url": p.final_video_url,
        "result_download_url": f"/api/extensions/{_PKG}/result?job_id={job.job_id}" if p.final_video_url else "",
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


def _claim_next_job():
    """Return the oldest QUEUED job, atomically claimed (status -> RUNNING)."""
    if not JOBS_DIR.is_dir():
        return None
    candidates = []
    for job_dir in JOBS_DIR.iterdir():
        job_file = job_dir / "job.json"
        if not job_file.is_file():
            continue
        try:
            data = json.loads(job_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if _status_value((data.get("progress") or {}).get("status")) != JobStatus.QUEUED.value:
            continue
        candidates.append((data.get("created_at") or "", job_dir, data))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    _, _job_dir, data = candidates[0]
    try:
        job = Job.from_dict(data)
    except Exception:
        return None
    # Claim before running so a restart/second poll does not double-process it.
    job.progress.status = JobStatus.RUNNING
    if job.progress.phase == JobPhase.QUEUED:
        job.progress.phase = JobPhase.SCENARIO
    job.progress.message = job.progress.message or "Picked up by the anime worker…"
    _save_job(job)
    return job


def _run_job(job) -> None:
    job_dir = JOBS_DIR / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    pipeline = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        client = OpenRouterClient(api_key=OPENROUTER_API_KEY, state_dir=job_dir)
        pipeline = Pipeline(
            client=client,
            state_dir=job_dir,
            on_progress=_on_progress,
            shutdown_event=_shutdown,
            lessons_dir=STATE_DIR,       # shared across jobs for progressive learning
            ffmpeg_cache_dir=STATE_DIR,  # shared cache — avoid re-downloading ffmpeg per job
        )
        loop.run_until_complete(pipeline.run(job))
    except Exception as exc:  # noqa: BLE001 - record any pipeline failure on the job
        job.progress.status = JobStatus.ERROR
        job.progress.phase = JobPhase.ERROR
        job.progress.error = str(exc)
        job.progress.message = f"Pipeline crashed: {exc}"
        _on_progress(job)
    finally:
        if pipeline is not None:
            try:
                pipeline.kill_active_processes()
            except Exception:
                pass
        _save_job(job)
        try:
            loop.close()
        except Exception:
            pass


def _handle_signal(_signum, _frame) -> None:
    _shutdown.set()


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    while not _shutdown.is_set():
        job = None
        try:
            job = _claim_next_job()
        except Exception:
            job = None
        if job is None:
            _shutdown.wait(POLL_INTERVAL_SEC)
            continue
        _run_job(job)


if __name__ == "__main__":
    main()
