"""Core generation pipeline with VLM verification, video analysis, scene continuity,
progressive prompt learning, best-of-N selection, multi-dimensional scoring,
cross-scene identity check, adaptive simplification, and parallel generation."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

from .ffmpeg_bootstrap import ensure_ffmpeg
from .api_client import OpenRouterClient, run_with_timeout
from .models import (
    Character,
    GenerationSettings,
    Job,
    JobPhase,
    JobProgress,
    JobStatus,
    Location,
    MusicCue,
    Scene,
    Storyboard,
    VerificationResult,
)
from .prompts import (
    ADAPTIVE_SIMPLIFY_SCENE_PROMPT,
    CROSS_SCENE_IDENTITY_CHECK_PROMPT,
    IMAGE_CHARACTER_SHEET_PROMPT,
    IMAGE_KEYFRAME_PROMPT,
    IMAGE_KEYFRAME_SEQUENTIAL_PROMPT,
    IMAGE_LOCATION_PROMPT,
    MUSIC_PROMPT_TEMPLATE,
    SCENARIO_SYSTEM,
    SCENARIO_USER_TEMPLATE,
    SCENE_TRANSITION_TEMPLATE,
    VIDEO_PROMPT_TEMPLATE,
    VLM_COMPARE_CHARACTER_SHEETS_PROMPT,
    VLM_VERIFY_VIDEO_MULTIDIM_PROMPT,
)

logger = logging.getLogger("anime_studio.pipeline")

TIMEOUT_SCENARIO = 240
TIMEOUT_IMAGE = 400
TIMEOUT_MUSIC = 200
TIMEOUT_VIDEO = 660
TIMEOUT_VERIFY = 45
TIMEOUT_VIDEO_VERIFY = 180

MAX_VERIFY_RETRIES = 2
MAX_VIDEO_VERIFY_RETRIES = 2  # Raised from 1: video is the most expensive asset
IMAGE_GENERATION_RETRIES = 1

# Multi-dimensional scoring: weighted average threshold
MULTIDIM_PASS_THRESHOLD = 6.5
MULTIDIM_WEIGHTS = {"identity": 0.30, "motion": 0.20, "style": 0.15, "artifacts": 0.25, "composition": 0.10}

_STDERR_CAP_BYTES = 65536  # 64 KB

_LESSONS_FILENAME = "prompt_lessons.json"

class Pipeline:
    """Full anime pipeline: VLM verification, progressive learning,
    best-of-2 selection, multi-dim scoring, identity checks, parallel gen."""

    def __init__(
        self,
        client: OpenRouterClient,
        state_dir: Path,
        on_progress: Optional[Callable[[Job], None]] = None,
        shutdown_event: Optional[threading.Event] = None,
        lessons_dir: Optional[Path] = None,
        ffmpeg_cache_dir: Optional[Path] = None,
    ):
        self.client = client
        self.state_dir = state_dir
        self.on_progress = on_progress
        self.shutdown_event = shutdown_event
        self.lessons_dir = lessons_dir or state_dir
        self.ffmpeg_cache_dir = ffmpeg_cache_dir or state_dir
        self._active_procs: list[subprocess.Popen] = []
        self._procs_lock = threading.Lock()
        self._learned_lessons: list[str] = []
        self._load_lessons()

        self._ffmpeg_path: str = "ffmpeg"
        self._ffprobe_path: str = "ffprobe"

    # ─── Progressive Learning ───

    def _load_lessons(self):
        """Load accumulated prompt lessons from previous jobs."""
        lessons_path = self.lessons_dir / _LESSONS_FILENAME
        if lessons_path.exists():
            try:
                data = json.loads(lessons_path.read_text())
                self._learned_lessons = data.get("image_lessons", [])[-10:] + data.get("video_lessons", [])[-10:]
            except Exception:
                pass

    def _persist_lessons(self, image_lessons: list[str], video_lessons: list[str]):
        """Save lessons to disk for next job."""
        lessons_path = self.lessons_dir / _LESSONS_FILENAME
        existing = {}
        if lessons_path.exists():
            try:
                existing = json.loads(lessons_path.read_text())
            except Exception:
                pass
        existing_img = existing.get("image_lessons", [])
        existing_vid = existing.get("video_lessons", [])
        all_img = list(dict.fromkeys(existing_img + image_lessons))[-20:]
        all_vid = list(dict.fromkeys(existing_vid + video_lessons))[-20:]
        lessons_path.write_text(json.dumps({
            "image_lessons": all_img,
            "video_lessons": all_vid,
        }, ensure_ascii=False, indent=2))

    def _get_lessons_text(self) -> str:
        """Format accumulated lessons for injection into prompts."""
        if not self._learned_lessons:
            return "No lessons yet — this is a fresh generation."
        return "\n".join(f"- {lesson}" for lesson in self._learned_lessons[-8:])

    def _add_lesson(self, lesson: str, category: str = "video"):
        """Add a lesson from a VLM rejection to the accumulated knowledge."""
        if lesson and lesson not in self._learned_lessons:
            self._learned_lessons.append(lesson)

    # ─── Utility ───

    def _emit(self, job: Job):
        if self.on_progress:
            self.on_progress(job)

    def _warn(self, job: Job, msg: str):
        job.progress.warnings.append(msg)
        logger.warning(msg)

    def _check_shutdown(self):
        if self.shutdown_event and self.shutdown_event.is_set():
            raise RuntimeError("Extension unloading — pipeline cancelled")

    def _run_ffmpeg(
        self, cmd: list[str], timeout: int = 120, capture_stdout: bool = False
    ) -> subprocess.CompletedProcess:
        """Run ffmpeg/ffprobe as a tracked child process.

        Child processes inherit the server's process group so the host's
        panic cleanup (os._exit / process-group kill) reaps them automatically.
        Normal cleanup uses _active_procs tracking + kill_active_processes().

        Environment is scrubbed to only PATH (needed for ffmpeg binary
        resolution) so no unrelated secrets leak to child processes.
        """
        import os

        stdout_target = subprocess.PIPE if capture_stdout else subprocess.DEVNULL
        bin_dir = str(self.ffmpeg_cache_dir / "bin")
        system_path = os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")
        scrubbed_env = {"PATH": f"{bin_dir}:{system_path}"}
        proc = subprocess.Popen(
            cmd,
            stdout=stdout_target,
            stderr=subprocess.PIPE,
            env=scrubbed_env,
        )

        with self._procs_lock:
            self._active_procs.append(proc)
        try:
            stdout_data, stderr_data = proc.communicate(timeout=timeout)
            if stderr_data and len(stderr_data) > _STDERR_CAP_BYTES:
                stderr_data = stderr_data[:_STDERR_CAP_BYTES]
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=proc.returncode,
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

    def _kill_proc(self, proc: subprocess.Popen):
        """Kill a subprocess on timeout/unload."""
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass

    def kill_active_processes(self):
        with self._procs_lock:
            for proc in self._active_procs:
                self._kill_proc(proc)
            self._active_procs.clear()

    # ─── Image Generation Router ───

    # Duration allowlists per video model — values accepted by OpenRouter /videos.
    _VIDEO_MODEL_DURATION_MAP: dict[str, list[int]] = {
        "google/veo-3.1":             [5, 8],
        "google/veo-3.1-fast":        [5, 8],
        "google/veo-3.1-lite":        [4, 6, 8],
        "minimax/hailuo-2.3":         [4, 6],
        "bytedance/seedance-1-5-pro": [4, 5, 6, 8, 10],
        "kwaivgi/kling-v3.0-pro":     [5, 10],
        "kwaivgi/kling-v3.0-std":     [5, 10],
        "kwaivgi/kling-video-o1":     [5, 10],
    }

    def _clamp_duration(self, desired_sec: int, model: str) -> int:
        """Return the nearest allowed duration for the given video model."""
        allowed = self._VIDEO_MODEL_DURATION_MAP.get(model)
        if not allowed:
            return min(15, max(4, desired_sec))
        return min(allowed, key=lambda v: abs(v - desired_sec))

    # Mapping from short UI image model names to OpenRouter model IDs.
    _IMAGE_MODEL_MAP: dict[str, str] = {
        "gpt-image-2":        "openai/gpt-5.4-image-2",
        "gpt-5-image":        "openai/gpt-5-image",
        "gpt-5-image-mini":   "openai/gpt-5-image-mini",
        "gemini-3-pro-image": "google/gemini-3-pro-image-preview",
        "flux.2-pro":         "black-forest-labs/flux.2-pro",
        "flux.2-max":         "black-forest-labs/flux.2-max",
        "seedream-4.5":       "bytedance-seed/seedream-4.5",
        "grok-imagine":       "x-ai/grok-imagine-image-quality",
    }

    async def _generate_image(self, job: Job, prompt: str, filename: str, aspect_ratio: str = "16:9") -> str:
        """Route image generation to configured model."""
        last_error: Exception | None = None
        model_names = [job.settings.image_model]
        if job.settings.image_model != "nanobanana":
            model_names.append("nanobanana")
        for model_name in model_names:
            for attempt in range(IMAGE_GENERATION_RETRIES + 1):
                try:
                    if model_name == "nanobanana":
                        return await self.client.generate_image_nanobanana(
                            prompt=prompt, filename=filename, aspect_ratio=aspect_ratio,
                        )
                    openrouter_id = self._IMAGE_MODEL_MAP.get(model_name, model_name)
                    return await self.client.generate_image(
                        prompt=prompt, filename=filename, aspect_ratio=aspect_ratio,
                        model=openrouter_id,
                    )
                except Exception as exc:
                    last_error = exc
                    if attempt >= IMAGE_GENERATION_RETRIES:
                        break
                    logger.warning(f"Image generation retry {attempt + 1} for {filename} via {model_name}: {exc}")
                    await asyncio.sleep(2.0 * (attempt + 1))
            if model_name != model_names[-1]:
                logger.warning(f"Image model {model_name} failed for {filename}; falling back to {model_names[-1]}")
        raise last_error or RuntimeError(f"Image generation failed for {filename}")

    # ─── Character Identity Block Builder ───

    def _build_characters_identity_block(self, storyboard: Storyboard, scene_chars: list[str] = None) -> str:
        chars = storyboard.characters
        if scene_chars:
            filtered = [c for c in chars if c.name in scene_chars]
            if filtered:
                chars = filtered
        lines = []
        for char in chars:
            lines.append(f"- {char.name}: {char.visual_traits}")
        return "\n".join(lines) if lines else "No specific character references."

    # ─── VLM Verification with Retry (Images) ───

    async def _verify_and_retry(
        self, job: Job, image_path: str, original_prompt: str,
        filename: str, aspect_ratio: str, char_ref_desc: str = "None",
    ) -> str:
        """Verify a generated image via VLM. Retry with improved prompt on failure."""
        stats = job.progress.verification_stats
        current_prompt = original_prompt

        for attempt in range(MAX_VERIFY_RETRIES + 1):
            if attempt > 0:
                retry_filename = f"{Path(filename).stem}_r{attempt}{Path(filename).suffix}"
                try:
                    image_path = await run_with_timeout(
                        self._generate_image(job, current_prompt, retry_filename, aspect_ratio),
                        timeout_sec=TIMEOUT_IMAGE,
                        description=f"Retry {attempt} for {filename}",
                    )
                except Exception as e:
                    self._warn(job, f"Retry {attempt} generation failed for {filename}: {e}")
                    break

            try:
                result = await run_with_timeout(
                    self.client.verify_image_vlm(image_path, original_prompt, char_ref_desc),
                    timeout_sec=TIMEOUT_VERIFY,
                    description=f"VLM verify {filename}",
                )
            except Exception as e:
                logger.warning(f"VLM verification skipped for {filename}: {e}")
                stats["skipped"] = stats.get("skipped", 0) + 1
                return image_path

            if result.get("vlm_error"):
                stats["skipped"] = stats.get("skipped", 0) + 1
                return image_path

            if result.get("passed", True):
                stats["passed"] = stats.get("passed", 0) + 1
                return image_path

            logger.info(f"VLM rejected {filename} (attempt {attempt+1}): {result.get('issues')}")
            stats["retried"] = stats.get("retried", 0) + 1

            if result.get("suggestion"):
                self._add_lesson(result["suggestion"], "image")
                current_prompt = f"{original_prompt}\n\nCRITICAL: {result['suggestion']}"

        stats["failed"] = stats.get("failed", 0) + 1
        self._warn(job, f"VLM verification failed for {filename} after {MAX_VERIFY_RETRIES} retries")
        return image_path

    # ─── Best-of-2 Character Sheet Selection ───

    async def _generate_best_of_2_character_sheet(
        self, job: Job, char: Character, index: int, style: str
    ) -> Optional[str]:
        """Generate 2 character sheets in parallel, VLM picks the best one."""
        prompt = IMAGE_CHARACTER_SHEET_PROMPT.format(
            name=char.name, visual_traits=char.visual_traits, style=style,
        )
        base_name = f"char_{index}_{char.name.lower().replace(' ', '_')}"

        tasks = [
            run_with_timeout(
                self._generate_image(job, prompt, f"{base_name}_a.png", "1:1"),
                timeout_sec=TIMEOUT_IMAGE,
                description=f"Character sheet '{char.name}' candidate A",
            ),
            run_with_timeout(
                self._generate_image(job, prompt, f"{base_name}_b.png", "1:1"),
                timeout_sec=TIMEOUT_IMAGE,
                description=f"Character sheet '{char.name}' candidate B",
            ),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        candidates = []
        for r in results:
            if not isinstance(r, Exception):
                candidates.append(r)

        if not candidates:
            self._warn(job, f"Both character sheet candidates failed for '{char.name}'")
            return None
        if len(candidates) == 1:
            return await self._verify_and_retry(
                job, candidates[0], prompt, f"{base_name}.png", "1:1",
                char_ref_desc=char.visual_traits,
            )

        compare_prompt = VLM_COMPARE_CHARACTER_SHEETS_PROMPT.format(
            name=char.name, visual_traits=char.visual_traits, style=style,
        )
        try:
            comparison = await run_with_timeout(
                self.client.compare_images_vlm(candidates[0], candidates[1], compare_prompt),
                timeout_sec=TIMEOUT_VERIFY,
                description=f"Compare sheets for '{char.name}'",
            )
            winner_idx = comparison.get("winner", 1) - 1  # 1-based to 0-based
            winner_idx = max(0, min(1, winner_idx))
            logger.info(f"Best-of-2 for '{char.name}': winner={winner_idx+1}, reason={comparison.get('reason', '?')}")
        except Exception as e:
            logger.warning(f"Character comparison failed, using first: {e}")
            winner_idx = 0

        return candidates[winner_idx]

    # ─── Multi-Dimensional Video Verification ───────────────────────

    async def _verify_video_multidim(
        self, job: Job, video_path: str, scene: Scene, storyboard: Storyboard,
    ) -> dict:
        """Verify video with multi-dimensional scoring (identity/motion/style/artifacts/composition).
        Returns {passed: bool, scores: dict, issues: list, suggestion: str}
        """
        chars_desc = self._build_characters_identity_block(storyboard, scene.characters)
        frame_paths = self._extract_video_frames(video_path, num_frames=5, prefix=f"s{scene.index}")

        if not frame_paths:
            return {"passed": True, "scores": {}, "issues": [], "suggestion": ""}

        verify_prompt = VLM_VERIFY_VIDEO_MULTIDIM_PROMPT.format(
            scene_description=scene.description,
            characters_description=chars_desc,
            style=storyboard.style,
            camera_direction=scene.camera_direction,
            learned_lessons=self._get_lessons_text(),
        )

        try:
            result = await run_with_timeout(
                self.client.analyze_multi_image_vlm(frame_paths, verify_prompt),
                timeout_sec=TIMEOUT_VIDEO_VERIFY,
                description=f"Video multidim verify scene {scene.index}",
            )

            scores = {k: result.get(k, 7) for k in MULTIDIM_WEIGHTS}
            weighted_avg = sum(scores[k] * MULTIDIM_WEIGHTS[k] for k in scores)
            result["passed"] = weighted_avg >= MULTIDIM_PASS_THRESHOLD
            result["weighted_score"] = round(weighted_avg, 2)
            result["scores"] = scores
            return result
        except Exception as e:
            logger.warning(f"Video multidim verification skipped for scene {scene.index}: {e}")
            return {"passed": True, "scores": {}, "issues": [], "suggestion": "", "vlm_error": True}
        finally:
            for fp in frame_paths:
                try:
                    Path(fp).unlink(missing_ok=True)
                except Exception:
                    pass

    # ─── Video Frame Extraction ─────────────────────────────────────

    def _extract_video_frames(self, video_path: str, num_frames: int = 5, prefix: str = "") -> list[str]:
        """Extract evenly-spaced frames from a video using tracked _run_ffmpeg.

        Args:
            prefix: unique prefix to avoid filename collisions between concurrent
                    callers (e.g. "s0" for scene 0, "xcheck_1" for cross-scene check).
        """
        output_dir = self.state_dir / "assets"
        output_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        tag = f"{prefix}_" if prefix else ""
        try:
            probe = self._run_ffmpeg(
                [self._ffprobe_path, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", video_path],
                timeout=15, capture_stdout=True,
            )
            duration_str = probe.stdout.strip() if probe.stdout else ""
            duration = float(duration_str) if duration_str else 5.0

            interval = duration / (num_frames + 1)
            for i in range(num_frames):
                timestamp = interval * (i + 1)
                output_path = str(output_dir / f"_vframe_{tag}{i}.png")
                result = self._run_ffmpeg(
                    [self._ffmpeg_path, "-y", "-ss", f"{timestamp:.2f}", "-i", video_path,
                     "-vframes", "1", "-q:v", "2", output_path],
                    timeout=15,
                )
                if result.returncode == 0 and Path(output_path).exists():
                    frames.append(output_path)
        except Exception as e:
            logger.warning(f"Frame extraction failed: {e}")
        return frames

    # ─── Last Frame Extraction ──────────────────────────────────────

    def _extract_last_frame(self, video_path: str, scene_index: int) -> Optional[str]:
        """Extract the last frame of a video clip for scene continuity."""
        output_path = str(self.state_dir / "assets" / f"lastframe_{scene_index}.png")
        try:
            probe = self._run_ffmpeg(
                [self._ffprobe_path, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", video_path],
                timeout=15, capture_stdout=True,
            )
            duration_str = probe.stdout.strip() if probe.stdout else ""
            duration = float(duration_str) if duration_str else 5.0
            timestamp = max(0, duration - 0.1)

            result = self._run_ffmpeg(
                [self._ffmpeg_path, "-y", "-ss", str(timestamp), "-i", video_path,
                 "-vframes", "1", "-q:v", "2", output_path],
                timeout=30,
            )
            if result.returncode == 0 and Path(output_path).exists():
                return output_path
        except Exception as e:
            logger.warning(f"Failed to extract last frame for scene {scene_index}: {e}")
        return None

    # ─── Adaptive Scene Simplification ──────────────────────────────

    async def _simplify_scene(self, scene: Scene, issues: list[str]) -> tuple[str, str, str]:
        """Ask LLM to simplify a scene that repeatedly fails video generation."""
        prompt = ADAPTIVE_SIMPLIFY_SCENE_PROMPT.format(
            scene_description=scene.description,
            camera_direction=scene.camera_direction,
            characters=", ".join(scene.characters),
            duration_sec=int(scene.duration_sec),
            issues_summary="\n".join(f"- {issue}" for issue in issues[-5:]),
        )
        try:
            response = await run_with_timeout(
                self.client.chat(
                    messages=[{"role": "user", "content": prompt}],
                    model="anthropic/claude-sonnet-4.6",
                    max_toks=1536,
                    temperature=0.3,
                    json_mode=True,
                ),
                timeout_sec=60,
                description=f"Simplify scene {scene.index}",
            )
            data = self.client.parse_json_response(response)
            return (
                data.get("simplified_description", scene.description),
                data.get("simplified_camera", scene.camera_direction),
                data.get("negative_constraints", ""),
            )
        except Exception as e:
            logger.warning(f"Scene simplification failed: {e}")
            return scene.description, "static medium shot", ""

    # ─── Cross-Scene Identity Verification ──────────────────────────

    async def _cross_scene_identity_check(self, job: Job, storyboard: Storyboard) -> Optional[int]:
        """After all videos: extract 1 frame per scene, check identity consistency.
        Returns the worst_scene_index if drift is major, else None.
        """
        frame_paths = []
        for i, scene in enumerate(storyboard.scenes):
            if scene.video_url and Path(scene.video_url).exists():
                mid_frames = self._extract_video_frames(scene.video_url, num_frames=1, prefix=f"xcheck_{i}")
                if mid_frames:
                    frame_paths.append(mid_frames[0])
                else:
                    frame_paths.append(None)
            else:
                frame_paths.append(None)

        valid_frames = [f for f in frame_paths if f]
        if len(valid_frames) < 2:
            return None

        chars_desc = self._build_characters_identity_block(storyboard)
        prompt = CROSS_SCENE_IDENTITY_CHECK_PROMPT.format(
            characters_description=chars_desc,
            style=storyboard.style,
        )

        try:
            result = await run_with_timeout(
                self.client.analyze_multi_image_vlm(valid_frames, prompt),
                timeout_sec=TIMEOUT_VIDEO_VERIFY,
                description="Cross-scene identity check",
            )
            severity = result.get("severity", "none")
            if severity == "major":
                worst = result.get("worst_scene_index")
                drift = result.get("drift_description", "unknown drift")
                self._warn(job, f"Cross-scene identity drift (major): {drift}")
                job.progress.verification_stats["cross_scene_drift"] = drift
                return worst if isinstance(worst, int) else None
            elif severity == "minor":
                job.progress.verification_stats["cross_scene_drift"] = result.get("drift_description", "minor")
            return None
        except Exception as e:
            logger.warning(f"Cross-scene identity check failed: {e}")
            return None
        finally:
            for fp in valid_frames:
                try:
                    Path(fp).unlink(missing_ok=True)
                except Exception:
                    pass

    # ─── Main Pipeline ──────────────────────────────────────────────

    async def run(self, job: Job) -> Job:
        """Execute the full pipeline."""
        try:
            job.progress.message = "Checking ffmpeg availability..."
            job.progress.progress_pct = 1.0
            self._emit(job)

            try:
                def _download_progress(downloaded, total, tool):
                    if total:
                        pct = min(downloaded / total * 100, 100)
                        job.progress.message = f"Downloading {tool}... {pct:.0f}%"
                    else:
                        mb = downloaded / (1024 * 1024)
                        job.progress.message = f"Downloading {tool}... {mb:.1f} MB"
                    job.progress.progress_pct = 2.0
                    self._emit(job)

                paths = ensure_ffmpeg(self.ffmpeg_cache_dir, on_progress=_download_progress)
                self._ffmpeg_path = paths["ffmpeg"]
                self._ffprobe_path = paths["ffprobe"]
            except Exception as exc:
                job.progress.phase = JobPhase.ERROR
                job.progress.status = JobStatus.ERROR
                job.progress.error = f"ffmpeg setup failed: {exc}"
                job.progress.message = job.progress.error
                return job

            self._check_shutdown()
            job.progress.phase = JobPhase.SCENARIO
            job.progress.status = JobStatus.RUNNING
            job.progress.message = "Generating storyboard..."
            job.progress.progress_pct = 5.0
            self._emit(job)

            storyboard = await run_with_timeout(
                self._generate_scenario(job.settings, job=job),
                timeout_sec=TIMEOUT_SCENARIO,
                description="Storyboard generation",
            )
            job.progress.storyboard = storyboard
            job.progress.progress_pct = 15.0
            job.progress.message = f"Storyboard ready: {storyboard.title} ({len(storyboard.scenes)} scenes)"
            self._emit(job)

            self._check_shutdown()
            job.progress.phase = JobPhase.ASSETS
            if job.settings.include_music:
                job.progress.message = "Generating assets + music in parallel..."
            else:
                job.progress.message = "Generating assets (best-of-2 character sheets, keyframes)..."
            job.progress.progress_pct = 18.0
            self._emit(job)

            parallel_tasks = [self._generate_assets(job, storyboard)]
            if job.settings.include_music:
                parallel_tasks.append(self._generate_music(job, storyboard))
            await asyncio.gather(*parallel_tasks)

            job.progress.phase = JobPhase.VERIFICATION
            stats = job.progress.verification_stats
            passed = stats.get("passed", 0)
            retried = stats.get("retried", 0)
            failed = stats.get("failed", 0)
            music_count = len(job.progress.music_clips)
            job.progress.message = (
                f"Verification: {passed} passed, {retried} retried, {failed} failed"
                + (f" | Music: {music_count} clips" if job.settings.include_music else "")
            )
            job.progress.progress_pct = 55.0
            self._emit(job)

            job.progress.message = "Assets ready. Starting animation with multi-dim scoring..."
            self._emit(job)

            self._check_shutdown()
            job.progress.phase = JobPhase.ANIMATION
            job.progress.message = "Animating scenes (multidim scoring + frame anchoring)..."
            self._emit(job)

            await self._generate_videos(job, storyboard)

            self._check_shutdown()
            job.progress.message = "Running cross-scene identity check..."
            self._emit(job)

            worst_scene = await self._cross_scene_identity_check(job, storyboard)
            if worst_scene is not None and 0 <= worst_scene < len(storyboard.scenes):
                job.progress.message = f"Identity drift detected in scene {worst_scene}. Regenerating..."
                self._emit(job)
                await self._regenerate_single_scene(job, storyboard, worst_scene)

            job.progress.progress_pct = 92.0
            job.progress.message = "All scenes animated. Assembling final video..."
            self._emit(job)

            self._check_shutdown()
            job.progress.phase = JobPhase.ASSEMBLY
            self._emit(job)

            final_path = await self._assemble(job, storyboard)
            job.progress.final_video_url = str(final_path)
            job.progress.phase = JobPhase.DONE
            job.progress.status = JobStatus.DONE
            job.progress.progress_pct = 100.0

            img_lessons = [l for l in self._learned_lessons if "text" in l.lower() or "character" in l.lower()]
            vid_lessons = [l for l in self._learned_lessons if l not in img_lessons]
            self._persist_lessons(img_lessons, vid_lessons or self._learned_lessons)

            if job.progress.warnings:
                job.progress.message = f"Done with {len(job.progress.warnings)} warning(s)."
            else:
                job.progress.message = "Animation complete!"
            self._emit(job)

        except Exception as e:
            logger.exception("Pipeline error")
            job.progress.phase = JobPhase.ERROR
            job.progress.status = JobStatus.ERROR
            job.progress.error = str(e)
            job.progress.message = f"Error: {e}"
            self._emit(job)

        return job

    # ─── Phase 1: Scenario ──────────────────────────────────────────

    async def _generate_scenario(self, settings: GenerationSettings, job: Optional[Job] = None) -> Storyboard:
        """Generate storyboard via LLM with automatic retry on malformed JSON."""
        prompt = SCENARIO_USER_TEMPLATE.format(
            theme=settings.theme, style=settings.style,
            duration_sec=settings.duration_sec, num_scenes=settings.num_scenes,
            mood=settings.mood, include_dialogue=settings.include_dialogue,
            music_style=settings.music_style,
        )
        max_attempts = 3
        last_error: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            self._check_shutdown()
            if attempt > 1 and job is not None:
                job.progress.message = f"Storyboard parse failed, retrying ({attempt}/{max_attempts})..."
                self._emit(job)
            response = await self.client.chat(
                messages=[
                    {"role": "system", "content": SCENARIO_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                model="anthropic/claude-sonnet-4.6",
                max_toks=8192, temperature=0.8,
                json_mode=True,
            )
            try:
                data = self.client.parse_json_response(response)
                scenes = []
                for s in data["scenes"]:
                    scenes.append(Scene(
                        index=s["index"], description=s["description"],
                        duration_sec=s["duration_sec"], characters=s["characters"],
                        location=s["location"], camera_direction=s["camera_direction"],
                        dialogue=s.get("dialogue"), mood=s.get("mood", "neutral"),
                        transition_from=s.get("transition_from"),
                    ))
                return Storyboard(
                    title=data["title"], synopsis=data["synopsis"],
                    style=data["style"], total_duration_sec=data["total_duration_sec"],
                    characters=[Character(**c) for c in data["characters"]],
                    locations=[Location(**loc) for loc in data["locations"]],
                    scenes=scenes,
                    music_cues=[MusicCue(**m) for m in data["music_cues"]],
                )
            except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
                last_error = e
                logger.warning(
                    f"Scenario parse failed (attempt {attempt}/{max_attempts}): {e}"
                )
                if attempt < max_attempts:
                    await asyncio.sleep(1)  # brief pause before retry
        raise ValueError(
            f"Failed to generate valid storyboard after {max_attempts} attempts. "
            f"Last error: {last_error}"
        )

    # ─── Phase 2: Assets (Best-of-2 chars parallel, locs parallel, keyframes sequential)

    async def _generate_assets(self, job: Job, storyboard: Storyboard):
        """Generate assets with maximum parallelism where dependencies allow."""

        char_tasks = [
            self._generate_best_of_2_character_sheet(job, char, i, storyboard.style)
            for i, char in enumerate(storyboard.characters)
        ]
        char_results = await asyncio.gather(*char_tasks, return_exceptions=True)

        for i, result in enumerate(char_results):
            self._check_shutdown()
            if isinstance(result, Exception):
                self._warn(job, f"Character sheet '{storyboard.characters[i].name}' failed: {result}")
            elif result:
                storyboard.characters[i].sheet_url = result
                job.progress.character_sheets.append(result)

        job.progress.progress_pct = 30.0
        job.progress.message = f"Character sheets (best-of-2): {len(job.progress.character_sheets)}/{len(storyboard.characters)}"
        self._emit(job)
        self._check_shutdown()

        loc_tasks = []
        for i, loc in enumerate(storyboard.locations):
            prompt = IMAGE_LOCATION_PROMPT.format(
                name=loc.name, visual_traits=loc.visual_traits, style=storyboard.style,
            )
            loc_tasks.append(
                run_with_timeout(
                    self._generate_image(job, prompt, f"loc_{i}_{loc.name.lower().replace(' ', '_')}.png", "16:9"),
                    timeout_sec=TIMEOUT_IMAGE,
                    description=f"Location '{loc.name}'",
                )
            )
        loc_results = await asyncio.gather(*loc_tasks, return_exceptions=True)
        for i, result in enumerate(loc_results):
            if isinstance(result, Exception):
                self._warn(job, f"Location '{storyboard.locations[i].name}' art failed: {result}")
            else:
                storyboard.locations[i].art_url = result
                job.progress.location_arts.append(result)

        self._check_shutdown()

        char_ref_desc = "; ".join(f"{c.name}: {c.visual_traits}" for c in storyboard.characters)
        chars_identity_block = self._build_characters_identity_block(storyboard)
        prev_keyframe_desc = None

        for i, scene in enumerate(storyboard.scenes):
            self._check_shutdown()
            char_names = ", ".join(scene.characters)
            loc = next((l for l in storyboard.locations if l.name == scene.location), None)
            loc_desc = loc.visual_traits if loc else scene.location

            if i == 0 or prev_keyframe_desc is None:
                prompt = IMAGE_KEYFRAME_PROMPT.format(
                    scene_description=scene.description, characters=char_names,
                    location_description=loc_desc, camera_direction=scene.camera_direction,
                    mood=scene.mood, style=storyboard.style,
                )
            else:
                prompt = IMAGE_KEYFRAME_SEQUENTIAL_PROMPT.format(
                    scene_description=scene.description, characters=char_names,
                    location_description=loc_desc, camera_direction=scene.camera_direction,
                    mood=scene.mood, style=storyboard.style,
                    prev_keyframe_context=prev_keyframe_desc,
                    characters_identity_block=chars_identity_block,
                )

            try:
                result = await run_with_timeout(
                    self._generate_image(job, prompt, f"keyframe_{scene.index}.png", "16:9"),
                    timeout_sec=TIMEOUT_IMAGE,
                    description=f"Keyframe scene {scene.index}",
                )
                verified_path = await self._verify_and_retry(
                    job, result, prompt, f"keyframe_{scene.index}.png", "16:9",
                    char_ref_desc=char_ref_desc,
                )
                scene.keyframe_url = verified_path
                job.progress.keyframes.append(verified_path)
                prev_keyframe_desc = f"Scene {scene.index}: {scene.description} (camera: {scene.camera_direction})"
            except Exception as e:
                self._warn(job, f"Keyframe scene {i} failed: {e}")

            base_pct = 32.0
            increment = 16.0 / max(1, len(storyboard.scenes))
            job.progress.progress_pct = base_pct + increment * (i + 1)
            job.progress.message = f"Keyframes: {len(job.progress.keyframes)}/{len(storyboard.scenes)}"
            self._emit(job)

    # ─── Phase 2b: Music ────────────────────────────────────────────

    async def _generate_music(self, job: Job, storyboard: Storyboard):
        """Generate music clips in parallel."""
        music_tasks = []
        for cue in storyboard.music_cues:
            prompt = MUSIC_PROMPT_TEMPLATE.format(
                mood=cue.mood, tempo=cue.tempo, style=cue.style,
                duration_sec=cue.duration_sec, description=cue.description,
            )
            music_tasks.append(
                run_with_timeout(
                    self.client.generate_music(prompt=prompt, filename=f"music_{cue.segment_index}.mp3"),
                    timeout_sec=TIMEOUT_MUSIC,
                    description=f"Music cue {cue.segment_index}",
                )
            )
        results = await asyncio.gather(*music_tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                self._warn(job, f"Music cue {i} failed: {result}")
            else:
                storyboard.music_cues[i].audio_url = result
                job.progress.music_clips.append(result)
        job.progress.message = f"Music: {len(job.progress.music_clips)}/{len(storyboard.music_cues)} clips"
        self._emit(job)

    # ─── Failure Advisor (LLM-powered error recovery) ─────────────

    AVAILABLE_VIDEO_MODELS = [
        "bytedance/seedance-2.0",
        "bytedance/seedance-2.0-fast",
        "bytedance/seedance-1-5-pro",
        "google/veo-3.1",
        "google/veo-3.1-fast",
        "google/veo-3.1-lite",
        "minimax/hailuo-2.3",
        "kwaivgi/kling-v3.0-pro",
        "kwaivgi/kling-v3.0-std",
        "kwaivgi/kling-video-o1",
    ]

    _VIDEO_MODEL_PROMPT_LIMIT: dict[str, int] = {
        "kwaivgi/kling-v3.0-pro": 2500,
        "kwaivgi/kling-v3.0-std": 2500,
        "kwaivgi/kling-video-o1": 2500,
    }

    async def _condense_prompt_for_model(self, prompt: str, model: str) -> str:
        """Shorten *prompt* via LLM if it exceeds the model's character limit."""
        limit = self._VIDEO_MODEL_PROMPT_LIMIT.get(model)
        if not limit or len(prompt) <= limit:
            return prompt
        target = limit - 50
        logger.info(f"Prompt {len(prompt)} chars > {model} limit {limit}, condensing to ~{target}")
        req = (
            "You are a video prompt editor. Rewrite the prompt below to fit within "
            f"{target} characters. Keep ALL character visuals, camera, action, mood, "
            "and style. Remove boilerplate/negative constraints. Return ONLY the text.\n\n"
            f"--- ORIGINAL ---\n{prompt}\n--- END ---"
        )
        try:
            condensed = (await run_with_timeout(
                self.client.chat(
                    messages=[{"role": "user", "content": req}],
                    model="google/gemini-3.5-flash", max_toks=1024, temperature=0.2,
                ), timeout_sec=30, description="Prompt condensation",
            )).strip()
            if 100 < len(condensed) <= limit:
                logger.info(f"Prompt condensed: {len(prompt)} → {len(condensed)} chars")
                return condensed
            logger.warning(f"Condensation returned {len(condensed)} chars, hard-truncating")
            return prompt[:limit]
        except Exception as e:
            logger.warning(f"Condensation failed ({e}), hard-truncating")
            return prompt[:limit]

    async def _get_failure_advisor_recommendation(
        self, error: str, current_model: str, scene_description: str,
    ) -> dict:
        """Ask a fast LLM to analyze a video generation failure and recommend an action."""
        alternatives = [m for m in self.AVAILABLE_VIDEO_MODELS if m != current_model]
        prompt = (
            "You are an AI video generation advisor. A video generation request failed.\n\n"
            f"Error: {error}\n"
            f"Current model: {current_model}\n"
            f"Scene: {scene_description[:300]}\n\n"
            f"Available alternative models: {', '.join(alternatives)}\n\n"
            "Analyze the error and recommend ONE action:\n"
            '- "retry_same_model" — if the error is transient (timeout, rate limit, server error, 500/502/503)\n'
            '- "switch_model" — if the error is model-specific (copyright filter, content policy, '
            "unsupported feature). Pick the best alternative from the list above.\n"
            '- "skip" — if the error is fundamental and no model can help (invalid prompt, impossible request)\n\n'
            'Return ONLY valid JSON: {"action": "...", "reason": "...", "suggested_model": "model_id_or_null"}'
        )
        last_error: Exception | None = None
        for model in ("google/gemini-3.5-flash", "anthropic/claude-sonnet-4.6"):
            try:
                response = await run_with_timeout(
                    self.client.chat(
                        messages=[{"role": "user", "content": prompt}],
                        model=model,
                        max_toks=384,
                        temperature=0.1,
                        json_mode=True,
                    ),
                    timeout_sec=45,
                    description=f"Failure advisor ({model})",
                )
                result = self.client.parse_json_response(response)
                action = result.get("action", "skip")
                if action not in ("retry_same_model", "switch_model", "skip"):
                    action = "skip"
                suggested = result.get("suggested_model")
                if action == "switch_model" and suggested not in alternatives:
                    suggested = alternatives[0] if alternatives else None
                    if not suggested:
                        action = "skip"
                return {"action": action, "reason": result.get("reason", ""), "suggested_model": suggested}
            except Exception as e:
                last_error = e
                logger.warning(f"Failure advisor call failed via {model}: {e}")
        return {"action": "skip", "reason": f"Advisor unavailable: {last_error}", "suggested_model": None}

    # ─── Phase 3: Video Animation ──────────────────────────────────

    async def _generate_videos(self, job: Job, storyboard: Storyboard):
        """Generate video for each scene with multidim scoring and adaptive simplification."""
        prev_frame_path: Optional[str] = None
        chars_identity_block = self._build_characters_identity_block(storyboard)

        for i, scene in enumerate(storyboard.scenes):
            self._check_shutdown()

            references = []
            for char_name in scene.characters:
                char = next((c for c in storyboard.characters if c.name == char_name), None)
                if char and char.sheet_url:
                    references.append(self.client.make_input_reference(char.sheet_url))

            loc = next((l for l in storyboard.locations if l.name == scene.location), None)
            if loc and loc.art_url:
                references.append(self.client.make_input_reference(loc.art_url))

            if scene.keyframe_url:
                references.append(self.client.make_input_reference(scene.keyframe_url))

            frame_images = None
            if prev_frame_path and Path(prev_frame_path).exists():
                frame_images = [self.client.make_frame_image(prev_frame_path, "first_frame")]

            continuity_note = ""
            if i > 0 and scene.transition_from:
                continuity_note = SCENE_TRANSITION_TEMPLATE.format(
                    prev_scene_description=storyboard.scenes[i - 1].description,
                    transition_type=scene.transition_from,
                )

            base_prompt = VIDEO_PROMPT_TEMPLATE.format(
                scene_description=scene.description,
                characters_identity_block=chars_identity_block,
                camera_direction=scene.camera_direction,
                mood=scene.mood, style=storyboard.style,
                duration_sec=int(scene.duration_sec),
                continuity_note=continuity_note,
            )

            lessons_text = self._get_lessons_text()
            if lessons_text and "No lessons" not in lessons_text:
                base_prompt += f"\n\nLEARNED FROM PREVIOUS GENERATIONS (apply these):\n{lessons_text}"

            video_path = None
            all_issues: list[str] = []
            current_description = scene.description
            current_camera = scene.camera_direction

            for attempt in range(MAX_VIDEO_VERIFY_RETRIES + 1):
                current_prompt = base_prompt
                if attempt > 0 and all_issues:
                    current_prompt += "\n\nCRITICAL FIXES REQUIRED: " + "; ".join(all_issues[-3:])

                if attempt >= 2 and all_issues:
                    simplified_desc, simplified_cam, neg_constraints = await self._simplify_scene(
                        scene, all_issues
                    )
                    current_description = simplified_desc
                    current_camera = simplified_cam
                    current_prompt = VIDEO_PROMPT_TEMPLATE.format(
                        scene_description=simplified_desc,
                        characters_identity_block=chars_identity_block,
                        camera_direction=simplified_cam,
                        mood=scene.mood, style=storyboard.style,
                        duration_sec=int(scene.duration_sec),
                        continuity_note=continuity_note,
                    )
                    if neg_constraints:
                        current_prompt += f"\n\nADDITIONAL NEGATIVE CONSTRAINTS: {neg_constraints}"
                    logger.info(f"Scene {scene.index} simplified for attempt {attempt+1}")

                try:
                    final_prompt = await self._condense_prompt_for_model(
                        current_prompt, job.settings.video_model,
                    )

                    video_path = await run_with_timeout(
                        self.client.generate_video(
                            prompt=final_prompt,
                            filename=f"scene_{scene.index}_v{attempt}.mp4",
                            duration=self._clamp_duration(int(scene.duration_sec), job.settings.video_model),
                            resolution=job.settings.resolution,
                            aspect_ratio=job.settings.aspect_ratio,
                            input_references=references if references else None,
                            frame_images=frame_images,
                            model=job.settings.video_model,
                            generate_audio=False,
                        ),
                        timeout_sec=TIMEOUT_VIDEO,
                        description=f"Video scene {scene.index} (attempt {attempt+1})",
                    )

                    # Multi-dimensional verification
                    verify_result = await self._verify_video_multidim(job, video_path, scene, storyboard)
                    stats = job.progress.verification_stats

                    if verify_result.get("vlm_error"):
                        stats["video_skipped"] = stats.get("video_skipped", 0) + 1
                        logger.info(f"Video scene {scene.index} verification skipped (VLM error)")
                        break
                    elif verify_result.get("passed", True):
                        stats["video_passed"] = stats.get("video_passed", 0) + 1
                        scores = verify_result.get("scores", {})
                        logger.info(
                            f"Video scene {scene.index} passed (weighted={verify_result.get('weighted_score', '?')}, "
                            f"scores={scores})"
                        )
                        break
                    else:
                        stats["video_retried"] = stats.get("video_retried", 0) + 1
                        issues = verify_result.get("issues", [])
                        suggestion = verify_result.get("suggestion", "")
                        all_issues.extend(issues)
                        if suggestion:
                            self._add_lesson(suggestion, "video")
                        scores = verify_result.get("scores", {})
                        logger.info(
                            f"Video scene {scene.index} failed multidim (attempt {attempt+1}): "
                            f"weighted={verify_result.get('weighted_score', '?')}, scores={scores}"
                        )
                        if attempt >= MAX_VIDEO_VERIFY_RETRIES:
                            stats["video_failed"] = stats.get("video_failed", 0) + 1
                            self._warn(job, f"Video scene {scene.index} failed after {MAX_VIDEO_VERIFY_RETRIES+1} attempts")

                except Exception as e:
                    error_str = str(e)
                    self._warn(job, f"Video scene {i} failed: {error_str}")

                    _err_lower = error_str.lower()
                    _is_prompt_limit = any(
                        kw in _err_lower
                        for kw in ("prompt: size must be", "prompt too long", "prompt length", "maximum prompt")
                    )
                    if _is_prompt_limit:
                        alts = [m for m in self.AVAILABLE_VIDEO_MODELS if m != job.settings.video_model]
                        self._warn(
                            job,
                            f"Model {job.settings.video_model} rejected the prompt as too long. "
                            f"Try switching to a different model (e.g. {alts[0] if alts else 'N/A'})."
                        )
                        video_path = None
                        break

                    job.progress.message = f"Scene {i} failed — consulting advisor..."
                    self._emit(job)
                    advice = await self._get_failure_advisor_recommendation(
                        error_str, job.settings.video_model, scene.description,
                    )
                    action = advice.get("action", "skip")
                    reason = advice.get("reason", "")
                    suggested_model = advice.get("suggested_model")

                    if reason:
                        self._warn(job, f"Advisor ({action}): {reason}")

                    if action == "retry_same_model":
                        job.progress.message = f"Advisor: retrying scene {i} with {job.settings.video_model}..."
                        self._emit(job)
                        try:
                            retry_prompt = await self._condense_prompt_for_model(
                                current_prompt, job.settings.video_model,
                            )
                            video_path = await run_with_timeout(
                                self.client.generate_video(
                                    prompt=retry_prompt,
                                    filename=f"scene_{scene.index}_advisor_retry.mp4",
                                    duration=self._clamp_duration(int(scene.duration_sec), job.settings.video_model),
                                    resolution=job.settings.resolution,
                                    aspect_ratio=job.settings.aspect_ratio,
                                    input_references=references if references else None,
                                    frame_images=frame_images,
                                    model=job.settings.video_model,
                                    generate_audio=False,
                                ),
                                timeout_sec=TIMEOUT_VIDEO,
                                description=f"Advisor retry scene {scene.index}",
                            )
                        except Exception as retry_e:
                            self._warn(job, f"Advisor retry also failed: {retry_e}")
                            video_path = None
                    elif action == "switch_model" and suggested_model:
                        job.progress.message = f"Advisor: switching to {suggested_model} for scene {i}..."
                        self._emit(job)
                        try:
                            switch_prompt = await self._condense_prompt_for_model(
                                current_prompt, suggested_model,
                            )
                            video_path = await run_with_timeout(
                                self.client.generate_video(
                                    prompt=switch_prompt,
                                    filename=f"scene_{scene.index}_alt.mp4",
                                    duration=self._clamp_duration(int(scene.duration_sec), suggested_model),
                                    resolution=job.settings.resolution,
                                    aspect_ratio=job.settings.aspect_ratio,
                                    input_references=references if references else None,
                                    frame_images=frame_images,
                                    model=suggested_model,
                                    generate_audio=False,
                                ),
                                timeout_sec=TIMEOUT_VIDEO,
                                description=f"Scene {scene.index} with {suggested_model}",
                            )
                        except Exception as switch_e:
                            self._warn(job, f"Alternative model {suggested_model} also failed: {switch_e}")
                            video_path = None
                    else:
                        video_path = None

                    if video_path:
                        verify_result = await self._verify_video_multidim(job, video_path, scene, storyboard)
                        stats = job.progress.verification_stats
                        if verify_result.get("vlm_error"):
                            stats["video_skipped"] = stats.get("video_skipped", 0) + 1
                            logger.info(f"Video scene {scene.index} advisor fallback verification skipped (VLM error)")
                        elif verify_result.get("passed", True):
                            stats["video_passed"] = stats.get("video_passed", 0) + 1
                            logger.info(f"Video scene {scene.index} advisor fallback passed VLM score={verify_result.get('weighted_score', '?')}")
                        else:
                            stats["video_retried"] = stats.get("video_retried", 0) + 1
                            logger.warning(f"Video scene {scene.index} advisor fallback failed VLM: {verify_result.get('issues', [])}")
                            self._warn(job, "Advisor fallback video failed quality check, using as-is")

                    break

            if video_path:
                scene.video_url = video_path
                job.progress.video_clips.append(video_path)
                prev_frame_path = self._extract_last_frame(video_path, scene.index)
                scene.prev_frame_url = prev_frame_path
            else:
                prev_frame_path = None

            base = 55.0
            increment = 35.0 / max(1, len(storyboard.scenes))
            job.progress.progress_pct = base + increment * (i + 1)
            job.progress.message = f"Animated scene {i + 1}/{len(storyboard.scenes)} (multidim scoring)"
            self._emit(job)

    # ─── Regenerate Single Scene (for cross-scene identity fix) ─────

    async def _regenerate_single_scene(self, job: Job, storyboard: Storyboard, scene_idx: int):
        """Regenerate a single scene that failed cross-scene identity check."""
        scene = storyboard.scenes[scene_idx]
        chars_identity_block = self._build_characters_identity_block(storyboard)

        references = []
        for char_name in scene.characters:
            char = next((c for c in storyboard.characters if c.name == char_name), None)
            if char and char.sheet_url:
                references.append(self.client.make_input_reference(char.sheet_url))

        frame_images = None
        if scene_idx > 0:
            prev_scene = storyboard.scenes[scene_idx - 1]
            if prev_scene.prev_frame_url and Path(prev_scene.prev_frame_url).exists():
                frame_images = [self.client.make_frame_image(prev_scene.prev_frame_url, "first_frame")]

        prompt = VIDEO_PROMPT_TEMPLATE.format(
            scene_description=scene.description,
            characters_identity_block=chars_identity_block,
            camera_direction=scene.camera_direction,
            mood=scene.mood, style=storyboard.style,
            duration_sec=int(scene.duration_sec),
            continuity_note="CRITICAL: Character identity must match other scenes exactly. Pay extra attention to hair color, outfit details, and proportions.",
        )

        try:
            video_path = await run_with_timeout(
                self.client.generate_video(
                    prompt=prompt,
                    filename=f"scene_{scene.index}_regen.mp4",
                    duration=self._clamp_duration(int(scene.duration_sec), job.settings.video_model),
                    resolution=job.settings.resolution,
                    aspect_ratio=job.settings.aspect_ratio,
                    input_references=references if references else None,
                    frame_images=frame_images,
                    model=job.settings.video_model,
                    generate_audio=False,
                ),
                timeout_sec=TIMEOUT_VIDEO,
                description=f"Regenerate scene {scene.index} (identity fix)",
            )
            scene.video_url = video_path
            for idx, clip in enumerate(job.progress.video_clips):
                if f"scene_{scene.index}_" in clip:
                    job.progress.video_clips[idx] = video_path
                    break
            logger.info(f"Regenerated scene {scene.index} for identity consistency")
        except Exception as e:
            self._warn(job, f"Scene {scene.index} regeneration failed: {e}")

    # ─── Phase 4: Assembly ──────────────────────────────────────────

    async def _assemble(self, job: Job, storyboard: Storyboard) -> str:
        """Assemble final video from clips using ffmpeg."""
        output_dir = self.state_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{job.job_id}_final.mp4"

        clips = [
            scene.video_url for scene in storyboard.scenes
            if scene.video_url and Path(scene.video_url).exists()
        ]

        if not clips:
            raise RuntimeError("No video clips were generated successfully")

        if len(clips) == 1:
            shutil.copy2(clips[0], output_path)
            return str(output_path)

        concat_file = self.state_dir / "concat.txt"
        with open(concat_file, "w") as f:
            for clip in clips:
                f.write(f"file '{clip}'\n")

        cmd = [
            self._ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_file), "-c", "copy", str(output_path),
        ]
        proc = self._run_ffmpeg(cmd, timeout=120)

        if proc.returncode != 0:
            self._check_shutdown()
            cmd_reencode = [
                self._ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
                "-i", str(concat_file),
                "-c:v", "libx264", "-preset", "fast", "-c:a", "aac",
                str(output_path),
            ]
            proc = self._run_ffmpeg(cmd_reencode, timeout=300)
            if proc.returncode != 0:
                raise RuntimeError(f"ffmpeg assembly failed: {proc.stderr[-500:]}")

        music_clips = [
            mc.audio_url for mc in storyboard.music_cues
            if mc.audio_url and Path(mc.audio_url).exists()
        ]
        if music_clips:
            self._check_shutdown()
            await self._mix_audio(output_path, music_clips)

        logger.info(f"Final video assembled: {output_path}")
        return str(output_path)

    async def _mix_audio(self, video_path, music_clips: list[str]):
        """Mix music track with video audio using ffmpeg."""
        music_concat = self.state_dir / "music_concat.txt"
        with open(music_concat, "w") as f:
            for clip in music_clips:
                f.write(f"file '{clip}'\n")

        music_merged = self.state_dir / "music_merged.mp3"
        self._run_ffmpeg(
            [self._ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
             "-i", str(music_concat), "-c", "copy", str(music_merged)],
            timeout=60,
        )

        if not music_merged.exists():
            return

        self._check_shutdown()

        video_path = Path(video_path)
        temp_output = video_path.with_suffix(".tmp.mp4")
        cmd = [
            self._ffmpeg_path, "-y",
            "-i", str(video_path),
            "-i", str(music_merged),
            "-filter_complex",
            "[0:a]volume=1.0[va];[1:a]volume=0.4[ma];[va][ma]amix=inputs=2:duration=shortest[out]",
            "-map", "0:v", "-map", "[out]",
            "-c:v", "copy", "-c:a", "aac",
            str(temp_output),
        ]
        proc = self._run_ffmpeg(cmd, timeout=120)
        if proc.returncode == 0 and temp_output.exists():
            temp_output.replace(video_path)
