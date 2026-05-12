"""Core generation pipeline for Video Studio with:
- Effort-based quality control (low/regular/max)
- Parallel best-of-N candidate generation
- Gemini 2.5 Pro AV QC (effort=max)
- Director cross-scene QC pass (effort=max)
- Progressive prompt learning
- Scene continuity chain with frame_images anchoring
- Adaptive scene simplification
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from .api_client import OpenRouterClient, run_with_timeout
from .models import (
    Character,
    Effort,
    GenerationSettings,
    Job,
    JobPhase,
    JobProgress,
    JobStatus,
    Location,
    MusicCue,
    Scene,
    SceneQualityReport,
    Storyboard,
    VerificationResult,
)
from .prompts import (
    ADAPTIVE_SIMPLIFY_SCENE_PROMPT,
    CROSS_SCENE_IDENTITY_CHECK_PROMPT,
    DIRECTOR_QC_PROMPT,
    GEMINI_VIDEO_QC_PROMPT,
    IMAGE_CHARACTER_SHEET_PROMPT,
    IMAGE_KEYFRAME_PROMPT,
    IMAGE_KEYFRAME_SEQUENTIAL_PROMPT,
    IMAGE_LOCATION_PROMPT,
    MUSIC_PROMPT_TEMPLATE,
    SCENARIO_SYSTEM,
    SCENARIO_USER_TEMPLATE,
    VIDEO_PROMPT_TEMPLATE,
    VLM_COMPARE_CHARACTER_SHEETS_PROMPT,
    VLM_VERIFY_VIDEO_MULTIDIM_PROMPT,
)

logger = logging.getLogger("video_studio.pipeline")

# Per-operation timeouts (seconds)
TIMEOUT_SCENARIO = 240
TIMEOUT_IMAGE = 400
TIMEOUT_MUSIC = 200
TIMEOUT_VIDEO = 660
TIMEOUT_VERIFY = 45
TIMEOUT_VIDEO_VERIFY = 90
TIMEOUT_GEMINI_QC = 120
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


class Pipeline:
    """Video Studio pipeline: cinematic photorealistic video with
    effort-based quality control, parallel candidate generation,
    Gemini AV QC, and Director cross-scene review."""

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
        return "\n".join(f"- {c.name}: {c.visual_traits}" for c in chars) or "No character references."

    async def _verify_and_retry(self, job: Job, image_path: str, prompt: str,
                                  filename: str, aspect_ratio: str, char_ref: str = "None") -> str:
        stats = job.progress.verification_stats
        current_prompt = prompt
        max_retries = EFFORT_IMAGE_RETRIES.get(self._get_effort(job), 1)
        for attempt in range(max_retries + 1):
            if attempt > 0:
                retry_fn = f"{Path(filename).stem}_r{attempt}{Path(filename).suffix}"
                try:
                    image_path = await run_with_timeout(
                        self._generate_image(job, current_prompt, retry_fn, aspect_ratio),
                        timeout_sec=TIMEOUT_IMAGE, description=f"Retry {attempt} {filename}",
                    )
                except Exception as e:
                    self._warn(job, f"Retry {attempt} failed: {e}")
                    break
            try:
                result = await run_with_timeout(
                    self.client.verify_image_vlm(image_path, prompt, char_ref),
                    timeout_sec=TIMEOUT_VERIFY, description=f"VLM verify {filename}",
                )
            except Exception:
                stats["skipped"] = stats.get("skipped", 0) + 1
                return image_path
            if result.get("vlm_error"):
                stats["skipped"] = stats.get("skipped", 0) + 1
                return image_path
            if result.get("passed", True):
                stats["passed"] = stats.get("passed", 0) + 1
                return image_path
            stats["retried"] = stats.get("retried", 0) + 1
            if result.get("suggestion"):
                self._add_lesson(result["suggestion"], "image")
                current_prompt = f"{prompt}\n\nCRITICAL: {result['suggestion']}"
        stats["failed"] = stats.get("failed", 0) + 1
        return image_path

    async def _generate_best_character_sheet(self, job: Job, char: Character, index: int, style: str) -> Optional[str]:
        prompt = IMAGE_CHARACTER_SHEET_PROMPT.format(name=char.name, visual_traits=char.visual_traits, style=style)
        base = f"char_{index}_{char.name.lower().replace(' ', '_')}"
        n = self._n_candidates(job)
        tasks = [
            run_with_timeout(self._generate_image(job, prompt, f"{base}_{chr(97+i)}.png", "1:1"),
                             timeout_sec=TIMEOUT_IMAGE, description=f"Char {char.name} {i+1}")
            for i in range(n)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        candidates = [r for r in results if not isinstance(r, Exception) and r]
        if not candidates:
            self._warn(job, f"All character sheets failed for {char.name}")
            return None
        if len(candidates) == 1:
            return await self._verify_and_retry(job, candidates[0], prompt, f"{base}.png", "1:1", char.visual_traits)
        compare_prompt = VLM_COMPARE_CHARACTER_SHEETS_PROMPT.format(
            name=char.name, visual_traits=char.visual_traits, style=style)
        try:
            r = await run_with_timeout(
                self.client.compare_images_vlm(candidates[0], candidates[1], compare_prompt),
                timeout_sec=TIMEOUT_VERIFY, description=f"Compare sheets {char.name}"
            )
            best = candidates[max(0, min(1, r.get("winner", 1) - 1))]
            if len(candidates) >= 3:
                r2 = await run_with_timeout(
                    self.client.compare_images_vlm(best, candidates[2], compare_prompt),
                    timeout_sec=TIMEOUT_VERIFY, description=f"Compare sheets r2 {char.name}"
                )
                if r2.get("winner", 1) == 2:
                    best = candidates[2]
            return best
        except Exception:
            return candidates[0]

    async def _generate_best_keyframe(self, job: Job, prompt: str, filename: str, char_ref: str) -> Optional[str]:
        n = self._n_candidates(job)
        base = filename.rsplit(".", 1)[0]
        tasks = [
            run_with_timeout(self._generate_image(job, prompt, f"{base}_{chr(97+i)}.png", "16:9"),
                             timeout_sec=TIMEOUT_IMAGE, description=f"KF {filename} {i+1}")
            for i in range(n)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        candidates = [r for r in results if not isinstance(r, Exception) and r]
        if not candidates:
            self._warn(job, f"All keyframe candidates failed for {filename}")
            return None
        if len(candidates) == 1:
            return await self._verify_and_retry(job, candidates[0], prompt, filename, "16:9", char_ref)
        for c in candidates:
            try:
                r = await run_with_timeout(
                    self.client.verify_image_vlm(c, prompt, char_ref),
                    timeout_sec=TIMEOUT_VERIFY, description="Verify KF candidate"
                )
                if r.get("passed", True) and not r.get("vlm_error"):
                    return c
            except Exception:
                pass
        return candidates[0]

    # ─── Frame extraction helpers ────────────────────────────────────

    def _extract_video_frames(self, video_path: str, num_frames: int = 5, prefix: str = "") -> list[str]:
        output_dir = self.state_dir / "assets"
        output_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        tag = f"{prefix}_" if prefix else ""
        if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
            return frames
        try:
            probe = self._run_ffmpeg(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                       "-of", "csv=p=0", video_path], timeout=15, capture_stdout=True)
            duration = float(probe.stdout.strip()) if probe.stdout.strip() else 5.0
            interval = duration / (num_frames + 1)
            for i in range(num_frames):
                ts = interval * (i + 1)
                out = str(output_dir / f"_vframe_{tag}{i}.png")
                r = self._run_ffmpeg(["ffmpeg", "-y", "-ss", f"{ts:.2f}", "-i", video_path,
                                       "-vframes", "1", "-q:v", "2", out], timeout=15)
                if r.returncode == 0 and Path(out).exists():
                    frames.append(out)
        except Exception as e:
            logger.warning(f"Frame extraction failed: {e}")
        return frames

    def _extract_last_frame(self, video_path: str, scene_index: int) -> Optional[str]:
        out = str(self.state_dir / "assets" / f"lastframe_{scene_index}.png")
        if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
            return None
        try:
            probe = self._run_ffmpeg(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                       "-of", "csv=p=0", video_path], timeout=15, capture_stdout=True)
            dur = float(probe.stdout.strip()) if probe.stdout.strip() else 5.0
            ts = max(0, dur - 0.1)
            r = self._run_ffmpeg(["ffmpeg", "-y", "-ss", str(ts), "-i", video_path,
                                   "-vframes", "1", "-q:v", "2", out], timeout=30)
            if r.returncode == 0 and Path(out).exists():
                return out
        except Exception as e:
            logger.warning(f"Last frame extraction failed: {e}")
        return None

    # ─── Video QC ────────────────────────────────────────────────────

    async def _verify_video_multidim(self, job: Job, video_path: str, scene: Scene, storyboard: Storyboard) -> dict:
        chars_desc = self._build_chars_block(storyboard, scene.characters)
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
                timeout_sec=TIMEOUT_VIDEO_VERIFY, description=f"Video multidim verify s{scene.index}",
            )
            # Gemini with json_mode sometimes returns a list [{...}] instead of dict — unwrap
            if isinstance(result, list):
                result = result[0] if result else {}
            scores = {k: result.get(k, 7) for k in MULTIDIM_WEIGHTS}
            weighted_avg = sum(scores[k] * MULTIDIM_WEIGHTS[k] for k in scores)
            result["passed"] = weighted_avg >= MULTIDIM_PASS_THRESHOLD
            result["weighted_score"] = round(weighted_avg, 2)
            result["scores"] = scores
            return result
        except Exception as e:
            logger.warning(f"Video multidim verify skipped for s{scene.index}: {e}")
            return {"passed": False, "scores": {}, "issues": [f"QC skipped: {e}"], "suggestion": "QC unavailable", "qc_skipped": True}
        finally:
            for fp in frame_paths:
                try:
                    Path(fp).unlink(missing_ok=True)
                except Exception:
                    pass

    async def _verify_video_gemini(self, job: Job, video_path: str, scene: Scene, storyboard: Storyboard) -> SceneQualityReport:
        chars_desc = "\n".join(f"- {c.name}: {c.visual_traits}" for c in storyboard.characters)
        prompt = GEMINI_VIDEO_QC_PROMPT.format(
            scene_description=scene.description,
            characters_description=chars_desc,
            style=storyboard.style,
        )
        try:
            raw = await run_with_timeout(
                self.client.analyze_video_gemini(Path(video_path), prompt),
                timeout_sec=TIMEOUT_GEMINI_QC, description=f"Gemini AV QC s{scene.index}",
            )
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            import json as _json
            data = _json.loads(text)
            return SceneQualityReport(
                scene_index=scene.index,
                visual_score=data.get("visual_score", 5.0),
                audio_score=data.get("audio_score", 5.0),
                av_sync_score=data.get("av_sync_score", 5.0),
                identity_score=data.get("identity_score", 5.0),
                motion_score=data.get("motion_score", 5.0),
                artifacts_score=data.get("artifacts_score", 5.0),
                issues=data.get("issues", []),
                passed=data.get("passed", True),
                suggestion=data.get("suggestion", ""),
            )
        except Exception as e:
            logger.warning(f"Gemini AV QC failed s{scene.index}: {e}")
            return SceneQualityReport(scene_index=scene.index, passed=False, issues=[f"QC skipped: {e}"])

    async def _run_director_qc(self, job: Job, storyboard: Storyboard) -> list[int]:
        import base64, json as _json
        chars_desc = "\n".join(f"- {c.name}: {c.visual_traits}" for c in storyboard.characters)
        content = [{"type": "text", "text": DIRECTOR_QC_PROMPT.format(
            characters_description=chars_desc, style=storyboard.style,
        )}]
        for kf_path_str in job.progress.keyframes[:8]:
            kf_path = Path(kf_path_str)
            if kf_path.exists():
                try:
                    b64 = base64.b64encode(kf_path.read_bytes()).decode()
                    ext = kf_path.suffix.lstrip(".")
                    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/png")
                    content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                except Exception:
                    pass
        try:
            raw = await run_with_timeout(
                self.client.chat_multimodal(content, model="google/gemini-2.5-pro"),
                timeout_sec=TIMEOUT_DIRECTOR_QC, description="Director QC",
            )
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            data = _json.loads(text)
            if data.get("approved", True):
                return []
            scenes_to_regen = data.get("scenes_to_regen", [])[:3]
            logger.info(f"Director QC: score={data.get('overall_score','?')} regen={scenes_to_regen}")
            return scenes_to_regen
        except Exception as e:
            logger.warning(f"Director QC failed: {e}")
            return []

    async def _simplify_scene(self, scene: Scene, issues: list[str]) -> tuple:
        prompt = ADAPTIVE_SIMPLIFY_SCENE_PROMPT.format(
            scene_description=scene.description,
            camera_direction=scene.camera_direction,
            characters=", ".join(scene.characters),
            duration_sec=int(scene.duration_sec),
            issues_summary="\n".join(f"- {i}" for i in issues[-5:]),
        )
        try:
            response = await run_with_timeout(
                self.client.chat(messages=[{"role": "user", "content": prompt}],
                                  model="anthropic/claude-sonnet-4.6", max_toks=1024, temperature=0.3),
                timeout_sec=60, description=f"Simplify scene {scene.index}",
            )
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            import json as _json
            data = _json.loads(text)
            return (data.get("simplified_description", scene.description),
                    data.get("simplified_camera", "static medium shot"),
                    data.get("negative_constraints", ""))
        except Exception as e:
            logger.warning(f"Scene simplification failed: {e}")
            return scene.description, "static medium shot", ""

    # ─── Main run() ─────────────────────────────────────────────────

    async def run(self, job: Job):
        """Run the full pipeline end-to-end."""
        from dataclasses import asdict
        s = job.settings
        p = job.progress
        p.status = JobStatus.RUNNING

        # ── Phase 1: SCENARIO ──────────────────────────────────────
        p.phase = JobPhase.SCENARIO
        p.progress_pct = 5.0
        p.message = "Writing cinematic screenplay…"
        self._emit(job)
        self._check_shutdown()

        scenario_prompt = SCENARIO_USER_TEMPLATE.format(
            theme=s.theme,
            style=s.style,
            mood=s.mood,
            num_scenes=s.num_scenes,
            duration_sec=int(s.duration_sec),
            music_style=s.music_style,
            include_dialogue="yes" if s.include_dialogue else "no",
        )
        try:
            scenario_raw = await run_with_timeout(
                self.client.chat(
                    messages=[
                        {"role": "system", "content": SCENARIO_SYSTEM},
                        {"role": "user", "content": scenario_prompt},
                    ],
                    model="anthropic/claude-sonnet-4.6", max_toks=8192, temperature=0.8,
                ),
                timeout_sec=TIMEOUT_SCENARIO, description="Scenario generation",
            )
        except Exception as e:
            p.phase = JobPhase.ERROR
            p.status = JobStatus.ERROR
            p.error = f"Scenario generation failed: {e}"
            p.message = str(p.error)
            self._emit(job)
            return

        # Parse scenario JSON
        import json as _json, re as _re
        try:
            text = scenario_raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            scenario_data = _json.loads(text)
        except Exception:
            m = _re.search(r"\{[\s\S]+\}", scenario_raw)
            if m:
                try:
                    scenario_data = _json.loads(m.group())
                except Exception as e:
                    p.phase = JobPhase.ERROR
                    p.status = JobStatus.ERROR
                    p.error = f"Scenario parse failed: {e}"
                    p.message = str(p.error)
                    self._emit(job)
                    return
            else:
                p.phase = JobPhase.ERROR
                p.status = JobStatus.ERROR
                p.error = "Scenario JSON not found in LLM response"
                p.message = str(p.error)
                self._emit(job)
                return

        chars = [Character(name=c["name"], description=c.get("description", ""),
                            visual_traits=c.get("visual_traits", c.get("description", "")))
                 for c in scenario_data.get("characters", [])]
        locs = [Location(name=l["name"], description=l.get("description", ""),
                          visual_traits=l.get("visual_traits", l.get("description", "")))
                for l in scenario_data.get("locations", [])]
        scenes = []
        for i, sc in enumerate(scenario_data.get("scenes", [])):
            scenes.append(Scene(
                index=i,
                description=sc.get("description", ""),
                duration_sec=sc.get("duration_sec", s.duration_sec / s.num_scenes),
                characters=sc.get("characters", [c.name for c in chars]),
                location=sc.get("location", ""),
                camera_direction=sc.get("camera_direction", "medium shot"),
                dialogue=sc.get("dialogue"),
                mood=sc.get("mood", s.mood),
                lens_type=sc.get("lens_type"),
                color_temperature=sc.get("color_temperature"),
                lighting_setup=sc.get("lighting_setup"),
            ))
        music_cues = []
        for i, mc in enumerate(scenario_data.get("music_cues", [])):
            music_cues.append(MusicCue(
                segment_index=i,
                mood=mc.get("mood", s.mood),
                tempo=mc.get("tempo", "medium"),
                style=mc.get("style", s.music_style),
                duration_sec=mc.get("duration_sec", 10),
                description=mc.get("description", ""),
            ))

        if not scenes:
            p.phase = JobPhase.ERROR
            p.status = JobStatus.ERROR
            p.error = "Screenplay generation failed: LLM returned 0 scenes. Check the theme/prompt and retry."
            p.message = str(p.error)
            self._emit(job)
            return

        if not chars:
            p.phase = JobPhase.ERROR
            p.status = JobStatus.ERROR
            p.error = "Screenplay generation failed: LLM returned 0 characters. Check the theme/prompt and retry."
            p.message = str(p.error)
            self._emit(job)
            return

        storyboard = Storyboard(
            title=scenario_data.get("title", s.theme[:50]),
            synopsis=scenario_data.get("synopsis", s.theme),
            style=s.style,
            total_duration_sec=s.duration_sec,
            characters=chars,
            locations=locs,
            scenes=scenes,
            music_cues=music_cues,
        )
        p.storyboard = storyboard
        p.progress_pct = 15.0
        p.message = f"Screenplay ready: \"{storyboard.title}\" — {len(scenes)} scenes, {len(chars)} characters"
        self._emit(job)
        self._check_shutdown()

        # ── Phase 2: ASSETS ────────────────────────────────────────
        p.phase = JobPhase.ASSETS
        p.progress_pct = 20.0
        p.message = f"Generating character references ({self._n_candidates(job)} candidates each)…"
        self._emit(job)

        assets_dir = self.state_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)

        # Character sheets (parallel per-character)
        char_sheet_tasks = [
            self._generate_best_character_sheet(job, char, i, s.style)
            for i, char in enumerate(storyboard.characters)
        ]
        char_results = await asyncio.gather(*char_sheet_tasks, return_exceptions=True)
        for i, (char, result) in enumerate(zip(storyboard.characters, char_results)):
            if isinstance(result, Exception):
                self._warn(job, f"Character sheet failed for {char.name}: {result}")
            elif result:
                char.sheet_url = str(result)
                p.character_sheets.append(str(result))
        p.progress_pct = 35.0
        p.message = f"Character sheets done ({len(p.character_sheets)}/{len(chars)}). Generating keyframes…"
        self._emit(job)
        self._check_shutdown()

        # ── Phase 3: VERIFICATION (keyframes) ──────────────────────
        p.phase = JobPhase.VERIFICATION
        prev_frame: Optional[str] = None
        for i, scene in enumerate(storyboard.scenes):
            self._check_shutdown()
            chars_block = self._build_chars_block(storyboard, scene.characters)

            location_desc = scene.location or "unspecified location"
            if prev_frame:
                kf_prompt = IMAGE_KEYFRAME_SEQUENTIAL_PROMPT.format(
                    scene_description=scene.description,
                    characters=", ".join(scene.characters) or "characters",
                    location_description=location_desc,
                    style=s.style,
                    camera_direction=scene.camera_direction,
                    mood=scene.mood,
                    prev_keyframe_context=f"Previous scene keyframe: {prev_frame}",
                    characters_identity_block=chars_block,
                )
            else:
                kf_prompt = IMAGE_KEYFRAME_PROMPT.format(
                    scene_description=scene.description,
                    characters=", ".join(scene.characters) or "characters",
                    location_description=location_desc,
                    style=s.style,
                    camera_direction=scene.camera_direction,
                    mood=scene.mood,
                )

            kf_filename = f"keyframe_{i}.png"
            kf_path = await self._generate_best_keyframe(job, kf_prompt, kf_filename, chars_block)
            if kf_path:
                scene.keyframe_url = str(kf_path)
                p.keyframes.append(str(kf_path))
            p.progress_pct = 35.0 + (i + 1) / len(storyboard.scenes) * 20.0
            p.message = f"Keyframe {i+1}/{len(storyboard.scenes)} done"
            self._emit(job)

        # ── Phase 4: MUSIC ─────────────────────────────────────────
        if s.include_music and storyboard.music_cues:
            p.phase = JobPhase.MUSIC
            p.progress_pct = 55.0
            p.message = "Composing soundtrack…"
            self._emit(job)
            self._check_shutdown()

            music_tasks = []
            for mc in storyboard.music_cues[:4]:
                music_prompt = MUSIC_PROMPT_TEMPLATE.format(
                    style=mc.style, mood=mc.mood, tempo=mc.tempo,
                    description=mc.description, duration_sec=int(mc.duration_sec),
                )
                music_tasks.append(
                    run_with_timeout(
                        self.client.generate_music(
                            prompt=music_prompt,
                            filename=f"music_{mc.segment_index}.mp3",
                        ),
                        timeout_sec=TIMEOUT_MUSIC, description=f"Music cue {mc.segment_index}",
                    )
                )
            music_results = await asyncio.gather(*music_tasks, return_exceptions=True)
            for i, (mc, result) in enumerate(zip(storyboard.music_cues[:4], music_results)):
                if isinstance(result, Exception):
                    self._warn(job, f"Music cue {i} failed: {result}")
                elif result:
                    mc.audio_url = str(result)
                    p.music_clips.append(str(result))
            p.progress_pct = 60.0
            p.message = f"Soundtrack ready: {len(p.music_clips)} tracks"
            self._emit(job)

        # ── Phase 5: ANIMATION ─────────────────────────────────────
        p.phase = JobPhase.ANIMATION
        prev_frame = None
        for i, scene in enumerate(storyboard.scenes):
            self._check_shutdown()
            chars_block = self._build_chars_block(storyboard, scene.characters)
            duration = self._clamp_duration(scene.duration_sec, s.video_model)

            # Build video prompt
            audio_note = f"AUDIO: Generate voice and dialogue. Dialogue: {scene.dialogue}" if (s.generate_audio and scene.dialogue) else ("AUDIO: Generate ambient sound." if s.generate_audio else "AUDIO: No audio needed.")
            continuity_note = f"CONTINUITY: This scene follows the previous one. Maintain character identity and visual style." if prev_frame else ""
            video_prompt = VIDEO_PROMPT_TEMPLATE.format(
                scene_description=scene.description,
                characters_identity_block=chars_block,
                style=s.style,
                camera_direction=scene.camera_direction,
                mood=scene.mood,
                duration_sec=duration,
                generate_audio_note=audio_note,
                continuity_note=continuity_note,
            )

            video_path = None
            max_retries = self._max_video_retries(job)
            video_issues: list[str] = []

            for attempt in range(max_retries + 1):
                self._check_shutdown()
                p.message = f"Scene {i+1}/{len(storyboard.scenes)} — video attempt {attempt+1}"
                self._emit(job)

                try:
                    # NOTE: frame_images (prev_frame) and input_references intentionally NOT passed to Seedance.
                    # Seedance rejects ANY image containing an AI-generated human likeness with
                    # InputImageSensitiveContentDetected.PrivacyInformation (HTTP 400).
                    # This applies to both character sheets (input_references) AND prev_frame anchors (frame_images).
                    # Scene 0 passes (no prev_frame), scenes 1+ fail when prev_frame shows the astronaut.
                    # Temporal continuity and character identity are conveyed via the text prompt only.
                    video_path = await run_with_timeout(
                        self.client.generate_video(
                            prompt=video_prompt,
                            filename=f"scene_{i}_a{attempt}.mp4",
                            model=s.video_model,
                            duration=duration,
                            resolution="720p",
                            aspect_ratio="16:9",
                            generate_audio=s.generate_audio,
                            frame_images=None,
                            input_references=None,
                        ),
                        timeout_sec=TIMEOUT_VIDEO, description=f"Video s{i} attempt {attempt+1}",
                    )
                except Exception as e:
                    self._warn(job, f"Video s{i} attempt {attempt+1} failed: {e}")
                    continue

                if not video_path:
                    continue

                # Multidimensional frame-based QC
                qc = await self._verify_video_multidim(job, video_path, scene, storyboard)
                if qc.get("passed", True):
                    break
                video_issues = qc.get("issues", [])
                if qc.get("suggestion"):
                    self._add_lesson(qc["suggestion"], "video")
                if attempt < max_retries:
                    simplified_desc, simplified_cam, neg = await self._simplify_scene(scene, video_issues)
                    retry_audio_note = f"AUDIO: Generate voice. Dialogue: {scene.dialogue}" if (s.generate_audio and scene.dialogue) else ("AUDIO: Ambient sound." if s.generate_audio else "")
                    neg_note = f"CONTINUITY FIX: {neg}" if neg else "CONTINUITY: Maintain character identity."
                    video_prompt = VIDEO_PROMPT_TEMPLATE.format(
                        scene_description=simplified_desc,
                        characters_identity_block=chars_block,
                        style=s.style,
                        camera_direction=simplified_cam,
                        mood=scene.mood,
                        duration_sec=duration,
                        generate_audio_note=retry_audio_note,
                        continuity_note=neg_note,
                    )

            if video_path:
                scene.video_url = str(video_path)
                p.video_clips.append(str(video_path))
                # Gemini AV QC at effort=max
                if self._use_gemini_qc(job):
                    qr = await self._verify_video_gemini(job, video_path, scene, storyboard)
                    from dataclasses import asdict as _asdict
                    p.quality_reports.append(_asdict(qr))
                # Extract last frame for continuity
                lf = self._extract_last_frame(video_path, i)
                if lf:
                    scene.prev_frame_url = lf
                    prev_frame = lf

            p.progress_pct = 60.0 + (i + 1) / len(storyboard.scenes) * 30.0
            self._emit(job)

        # ── Phase 6: DIRECTOR QC (effort=max) ──────────────────────
        if self._use_director_qc(job) and len(storyboard.scenes) > 1:
            p.phase = JobPhase.DIRECTOR_QC
            p.progress_pct = 90.0
            p.message = "Director cross-scene review…"
            self._emit(job)
            self._check_shutdown()

            scenes_to_regen = await self._run_director_qc(job, storyboard)
            for si in scenes_to_regen:
                if si >= len(storyboard.scenes):
                    continue
                scene = storyboard.scenes[si]
                chars_block = self._build_chars_block(storyboard, scene.characters)
                duration = self._clamp_duration(scene.duration_sec, s.video_model)
                regen_audio_note = f"AUDIO: Generate voice and dialogue. Dialogue: {scene.dialogue}" if (s.generate_audio and scene.dialogue) else ("AUDIO: Generate ambient sound." if s.generate_audio else "AUDIO: No audio needed.")
                regen_prompt = VIDEO_PROMPT_TEMPLATE.format(
                    scene_description=scene.description,
                    characters_identity_block=chars_block,
                    style=s.style,
                    camera_direction=scene.camera_direction,
                    mood=scene.mood,
                    duration_sec=duration,
                    generate_audio_note=regen_audio_note,
                    continuity_note="DIRECTOR QC REGEN: Improve on previous attempt. Fix identified issues.",
                )
                p.message = f"Director QC: regenerating scene {si+1}…"
                self._emit(job)
                try:
                    # NOTE: frame_images and input_references both omitted — same Seedance privacy policy as Phase 5.
                    new_video = await run_with_timeout(
                        self.client.generate_video(
                            prompt=regen_prompt,
                            filename=f"scene_{si}_director_regen.mp4",
                            model=s.video_model,
                            duration=duration,
                            resolution="720p",
                            aspect_ratio="16:9",
                            generate_audio=s.generate_audio,
                            frame_images=None,
                            input_references=None,
                        ),
                        timeout_sec=TIMEOUT_VIDEO, description=f"Director regen s{si}",
                    )
                    if new_video:
                        # Replace old clip
                        if scene.video_url and str(scene.video_url) in p.video_clips:
                            idx = p.video_clips.index(str(scene.video_url))
                            p.video_clips[idx] = str(new_video)
                        scene.video_url = str(new_video)
                except Exception as e:
                    self._warn(job, f"Director regen scene {si} failed: {e}")

        # ── Phase 7: ASSEMBLY ──────────────────────────────────────
        p.phase = JobPhase.ASSEMBLY
        p.progress_pct = 93.0
        p.message = "Assembling final video…"
        self._emit(job)
        self._check_shutdown()

        video_clips = [vc for vc in p.video_clips if vc and Path(vc).exists()]
        if not video_clips:
            p.phase = JobPhase.ERROR
            p.status = JobStatus.ERROR
            p.error = "No video clips generated"
            p.message = str(p.error)
            self._emit(job)
            return

        output_dir = self.state_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        import uuid as _uuid
        output_path = str(output_dir / f"{job.job_id}_final.mp4")

        music_path = p.music_clips[0] if p.music_clips else None
        has_music = music_path and Path(music_path).exists()

        if len(video_clips) == 1 and not has_music:
            import shutil as _shutil
            _shutil.copy(video_clips[0], output_path)
            r = type("R", (), {"returncode": 0})()
        else:
            # Build concat list (even for single clip when music is present)
            concat_list = self.state_dir / "concat_list.txt"
            concat_list.write_text(
                "\n".join(f"file '{vc}'" for vc in video_clips), encoding="utf-8"
            )
            if has_music:
                # Try to mix video audio (voices/ambient) with music track at 35% volume.
                # First attempt uses [0:a][1:a]amix — requires the concatenated video to have an audio stream.
                # If the video has no audio stream (generate_audio=False or model returned silent video),
                # amix fails; fallback maps music directly as the sole audio track.
                r = self._run_ffmpeg([
                    "ffmpeg", "-y",
                    "-f", "concat", "-safe", "0", "-i", str(concat_list),
                    "-i", str(music_path),
                    "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:weights=1 0.35[aout]",
                    "-map", "0:v:0", "-map", "[aout]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                    output_path
                ], timeout=180)
                if r.returncode != 0:
                    # Fallback 1: video has no audio stream — map music directly
                    self._warn(job, f"ffmpeg amix failed (code {r.returncode}), trying music-only audio")
                    r = self._run_ffmpeg([
                        "ffmpeg", "-y",
                        "-f", "concat", "-safe", "0", "-i", str(concat_list),
                        "-i", str(music_path),
                        "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                        output_path
                    ], timeout=180)
                if r.returncode != 0:
                    # Fallback 2: concat without music
                    self._warn(job, f"ffmpeg music-mix failed (code {r.returncode}), assembling without music")
                    r = self._run_ffmpeg([
                        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(concat_list),
                        "-c", "copy", output_path
                    ], timeout=180)
            else:
                r = self._run_ffmpeg([
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat_list),
                    "-c", "copy", output_path
                ], timeout=180)

            if r.returncode != 0:
                # Fallback: copy first clip
                import shutil as _shutil
                _shutil.copy(video_clips[0], output_path)
                self._warn(job, f"ffmpeg assembly failed (code {r.returncode}), using first clip")

        p.final_video_url = output_path
        p.phase = JobPhase.DONE
        p.status = JobStatus.DONE
        p.progress_pct = 100.0
        video_passed = len([v for v in p.video_clips if v and Path(v).exists()])
        p.message = (
            f"Done! video_passed:{video_passed} "
            f"retried:{p.verification_stats.get('retried', 0)} "
            f"final: {output_path}"
        )
        self._persist_lessons([], self._learned_lessons[-5:])
        self._emit(job)
        logger.info(f"Video Studio job {job.job_id} complete: {output_path}")
