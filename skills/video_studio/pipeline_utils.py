"""Pipeline base class: shared helpers, ffmpeg, resolution, lessons, emit/warn/shutdown."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

from .api_client import OpenRouterClient
from .models import Job, Storyboard

logger = logging.getLogger("video_studio.pipeline_utils")

# Per-operation timeouts (seconds)
TIMEOUT_SCENARIO = 240
TIMEOUT_IMAGE = 400
TIMEOUT_MUSIC = 200
TIMEOUT_VIDEO = 660
TIMEOUT_VERIFY = 45
TIMEOUT_VIDEO_VERIFY = 90
TIMEOUT_GEMINI_QC = 240
TIMEOUT_DIRECTOR_QC = 90

# VLM verification settings
MAX_VERIFY_RETRIES = 2
MAX_VIDEO_VERIFY_RETRIES = 2

# Multi-dimensional scoring thresholds
MULTIDIM_PASS_THRESHOLD = 6.5
MULTIDIM_WEIGHTS = {"identity": 0.30, "motion": 0.20, "style": 0.15, "artifacts": 0.25, "composition": 0.10}

_STDERR_CAP_BYTES = 65536
_LESSONS_FILENAME = "prompt_lessons.json"

# Effort-based generation settings
EFFORT_CANDIDATES = {"low": 1, "regular": 2, "max": 3}
EFFORT_IMAGE_RETRIES = {"low": 0, "regular": 1, "max": 2}
EFFORT_VIDEO_RETRIES = {"low": 0, "regular": 1, "max": 2}
EFFORT_USE_GEMINI_QC = {"low": False, "regular": False, "max": True}
EFFORT_USE_DIRECTOR_QC = {"low": False, "regular": False, "max": True}

# Resolution settings
VALID_RESOLUTIONS = {"720p", "1080p"}
DEFAULT_RESOLUTION = "720p"


class PipelineBase:
    """Shared helpers, ffmpeg, resolution, lessons, emit/warn/shutdown.

    Serves as the base class for Pipeline via mixin composition.
    """

    def __init__(
        self,
        client: OpenRouterClient,
        state_dir: Path,
        on_progress: Optional[Callable[[Job], None]] = None,
        shutdown_event: Optional[threading.Event] = None,
        lessons_dir: Optional[Path] = None,
    ):
        self.client = client
        self.state_dir = state_dir
        self.on_progress = on_progress
        self.shutdown_event = shutdown_event
        self.lessons_dir = lessons_dir or state_dir
        self._active_procs: list[subprocess.Popen] = []
        self._procs_lock = threading.Lock()
        self._learned_lessons: list[str] = []
        self._character_dna: dict[str, str] = {}  # char_name -> DNA string
        self._load_lessons()

    # ─── Progressive Learning ────────────────────────────────────────

    def _load_lessons(self):
        lessons_path = self.lessons_dir / _LESSONS_FILENAME
        if lessons_path.exists():
            try:
                data = json.loads(lessons_path.read_text())
                self._learned_lessons = data.get("image_lessons", [])[-10:] + data.get("video_lessons", [])[-10:]
            except Exception:
                pass

    def _persist_lessons(self, image_lessons: list[str], video_lessons: list[str]):
        lessons_path = self.lessons_dir / _LESSONS_FILENAME
        existing = {}
        if lessons_path.exists():
            try:
                existing = json.loads(lessons_path.read_text())
            except Exception:
                pass
        all_img = list(dict.fromkeys(existing.get("image_lessons", []) + image_lessons))[-20:]
        all_vid = list(dict.fromkeys(existing.get("video_lessons", []) + video_lessons))[-20:]
        lessons_path.write_text(json.dumps({"image_lessons": all_img, "video_lessons": all_vid}, ensure_ascii=False, indent=2))

    def _get_lessons_text(self) -> str:
        if not self._learned_lessons:
            return "No lessons yet."
        return "\n".join(f"- {l}" for l in self._learned_lessons[-8:])

    def _add_lesson(self, lesson: str, category: str = "video"):
        if lesson and lesson not in self._learned_lessons:
            self._learned_lessons.append(lesson)

    # ─── Utilities ───────────────────────────────────────────────────

    def _emit(self, job: Job):
        if self.on_progress:
            self.on_progress(job)

    def _warn(self, job: Job, msg: str):
        job.progress.warnings.append(msg)
        logger.warning(msg)

    def _check_shutdown(self):
        if self.shutdown_event and self.shutdown_event.is_set():
            raise RuntimeError("Extension unloading — pipeline cancelled")

    def _get_effort(self, job: Job) -> str:
        effort = getattr(job.settings, "effort", "regular")
        if effort not in ("low", "regular", "max"):
            return "regular"
        return effort

    def _n_candidates(self, job: Job) -> int:
        return EFFORT_CANDIDATES.get(self._get_effort(job), 2)

    def _max_video_retries(self, job: Job) -> int:
        return EFFORT_VIDEO_RETRIES.get(self._get_effort(job), 1)

    def _use_gemini_qc(self, job: Job) -> bool:
        return EFFORT_USE_GEMINI_QC.get(self._get_effort(job), False)

    def _use_director_qc(self, job: Job) -> bool:
        return EFFORT_USE_DIRECTOR_QC.get(self._get_effort(job), False)

    # ─── ffmpeg helpers ──────────────────────────────────────────────

    def _run_ffmpeg(self, cmd: list, timeout: int = 120, capture_stdout: bool = False):
        stdout_target = subprocess.PIPE if capture_stdout else subprocess.DEVNULL
        scrubbed_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")}
        # start_new_session=False (default): ffmpeg inherits the worker process group
        # so host /panic os.killpg covers ffmpeg children automatically.
        # _kill_proc uses proc.kill() directly; no separate pgid needed.
        proc = subprocess.Popen(cmd, stdout=stdout_target, stderr=subprocess.PIPE,
                                 env=scrubbed_env)

        with self._procs_lock:
            self._active_procs.append(proc)
        try:
            stdout_data, stderr_data = proc.communicate(timeout=timeout)
            if stderr_data and len(stderr_data) > _STDERR_CAP_BYTES:
                stderr_data = stderr_data[:_STDERR_CAP_BYTES]
            return subprocess.CompletedProcess(
                args=cmd, returncode=proc.returncode,
                stdout=stdout_data.decode(errors="replace") if stdout_data else "",
                stderr=stderr_data.decode(errors="replace") if isinstance(stderr_data, bytes) else (stderr_data or ""),
            )
        except subprocess.TimeoutExpired:
            self._kill_proc(proc)
            proc.wait()
            raise
        finally:
            with self._procs_lock:
                if proc in self._active_procs:
                    self._active_procs.remove(proc)

    def _get_clip_duration(self, video_path: str) -> float:
        """Get duration of a video clip via ffprobe. Returns 5.0 as fallback."""
        if not shutil.which("ffprobe"):
            return 5.0
        try:
            probe = self._run_ffmpeg(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", video_path],
                timeout=15, capture_stdout=True,
            )
            return float(probe.stdout.strip()) if probe.stdout.strip() else 5.0
        except Exception:
            return 5.0

    def _kill_proc(self, proc):
        """Kill an ffmpeg process. Inherits host worker process group so /panic covers it."""
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass

    def kill_active_processes(self):
        with self._procs_lock:
            for proc in list(self._active_procs):
                self._kill_proc(proc)
            self._active_procs.clear()

    # ─── Resolution & Character DNA helpers ────────────────────────────

    def _get_resolution(self, job: Job) -> str:
        """Get validated resolution from settings."""
        res = getattr(job.settings, "resolution", DEFAULT_RESOLUTION)
        return res if res in VALID_RESOLUTIONS else DEFAULT_RESOLUTION

    async def _extract_character_dna(self, job: Job, char) -> str:
        from .quality import extract_character_dna
        return await extract_character_dna(self, job, char)

    async def _select_best_video_prompt(self, job: Job, scene, storyboard: Storyboard,
                                         base_prompt: str, char_dna: str) -> str:
        from .quality import select_best_video_prompt
        return await select_best_video_prompt(self, job, scene, storyboard, base_prompt, char_dna)

    async def _build_diagnosis_retry_prompt(self, job: Job, original_prompt: str,
                                             critique: dict, scene) -> str:
        from .quality import build_diagnosis_retry_prompt
        return await build_diagnosis_retry_prompt(self, job, original_prompt, critique, scene)

    async def _run_director_with_video_frames(self, job: Job, storyboard: Storyboard) -> dict:
        from .quality import run_director_with_video_frames
        return await run_director_with_video_frames(self, job, storyboard)

    async def _plan_transitions(self, job: Job, storyboard: Storyboard) -> list:
        from .quality import plan_transitions
        return await plan_transitions(self, job, storyboard)

    async def _get_color_grade_filter(self, job: Job) -> str:
        from .quality import get_color_grade_filter
        return await get_color_grade_filter(self, job)

    # ─── Duration clamping ───────────────────────────────────────────

    _VEO_ALLOWED: dict = {
        "google/veo-3.1": [5, 8],
        "google/veo-3.1-fast": [5, 8],
        "google/veo-3.1-lite": [4, 6, 8],
        # Seedance 2.0: supports any integer 4–15 s
        "bytedance/seedance-2.0": list(range(4, 16)),
    }

    def _clamp_duration(self, desired: float, model: str) -> int:
        allowed = self._VEO_ALLOWED.get(model)
        if allowed:
            return min(allowed, key=lambda v: abs(v - desired))
        return int(min(15, max(4, desired)))

    # ─── Image generation ────────────────────────────────────────────

    async def _generate_image(self, job: Job, prompt: str, filename: str, aspect_ratio: str = "16:9"):
        model = getattr(job.settings, "image_model", "nanobanana")
        if model == "nanobanana":
            return await self.client.generate_image_nanobanana(
                prompt=prompt, filename=filename, aspect_ratio=aspect_ratio
            )
        return await self.client.generate_image(
            prompt=prompt, filename=filename, aspect_ratio=aspect_ratio,
            model="openai/gpt-5.4-image-2"
        )

    def _build_chars_block(self, storyboard: Storyboard, names: Optional[list] = None) -> str:
        chars = storyboard.characters
        if names:
            filtered = [c for c in chars if c.name in names]
            if filtered:
                chars = filtered
        lines = []
        for c in chars:
            # Prefer character DNA if extracted, fall back to visual_traits
            dna = self._character_dna.get(c.name)
            if dna:
                lines.append(f"- {dna}")
            else:
                lines.append(f"- {c.name}: {c.visual_traits}")
        return "\n".join(lines) or "No character references."
