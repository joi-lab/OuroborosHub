"""Pipeline asset generation mixin: character sheets, keyframes, video verification, director QC loop."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
from pathlib import Path
from typing import Optional

from .api_client import run_with_timeout
from .models import Job, Scene, SceneQualityReport, Storyboard
from .prompts import (
    ADAPTIVE_SIMPLIFY_SCENE_PROMPT,
    DIRECTOR_AGENT_PROMPT,
    GEMINI_VIDEO_QC_PROMPT,
    IMAGE_CHARACTER_SHEET_PROMPT,
    VLM_COMPARE_CHARACTER_SHEETS_PROMPT,
    VLM_VERIFY_VIDEO_MULTIDIM_PROMPT,
)
from .pipeline_utils import (
    EFFORT_IMAGE_RETRIES,
    MULTIDIM_PASS_THRESHOLD,
    MULTIDIM_WEIGHTS,
    TIMEOUT_GEMINI_QC,
    TIMEOUT_IMAGE,
    TIMEOUT_VERIFY,
    TIMEOUT_VIDEO_VERIFY,
)

logger = logging.getLogger("video_studio.pipeline_assets")


class PipelineAssets:
    """Mixin providing asset generation and verification methods.

    Expects to be composed with PipelineBase (via Pipeline) so that
    self.client, self._get_effort, self._generate_image, self._warn,
    self._add_lesson, self._n_candidates, self._build_chars_block,
    self._extract_video_frames, self._extract_last_frame, self._run_ffmpeg,
    self._get_lessons_text, self.state_dir, etc. are available.
    """

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

    async def _generate_best_character_sheet(self, job: Job, char, index: int, style: str) -> Optional[str]:
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
            data = json.loads(text)
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

    async def _run_director_agent_loop(self, job: Job, storyboard: Storyboard) -> dict[int, str]:
        """Director agent reviews assembled video clips and returns per-scene notes for regeneration.

        Returns: dict mapping scene_index -> director note for scenes that need regeneration.
        Max 2 scenes per pass, max 2 passes total.
        """
        # Build scene timeline text
        timeline_parts = []
        for scene in storyboard.scenes:
            start_sec = sum(s.duration_sec for s in storyboard.scenes[:scene.index])
            end_sec = start_sec + scene.duration_sec
            causal = getattr(scene, 'causal_link', '') or ''
            timeline_parts.append(
                f"Scene {scene.index} ({start_sec:.0f}s-{end_sec:.0f}s): {scene.description}"
                + (f"\n  Causal link: {causal}" if causal else "")
            )
        scene_timeline = "\n".join(timeline_parts)

        chars_desc = "\n".join(f"- {c.name}: {c.visual_traits}" for c in storyboard.characters)
        synopsis = storyboard.synopsis or job.settings.theme

        director_notes: dict[int, str] = {}

        for pass_num in range(2):  # max 2 director passes
            # Build content with frames from actual video clips
            content = [{"type": "text", "text": DIRECTOR_AGENT_PROMPT.format(
                characters_description=chars_desc,
                style=storyboard.style,
                synopsis=synopsis,
                scene_timeline=scene_timeline,
            )}]

            # Attach first + last frame of each video clip
            frames_added = 0
            for scene in storyboard.scenes:
                video_path = scene.video_url
                if not video_path or not Path(video_path).exists():
                    # Fall back to keyframe if no video yet
                    kf = scene.keyframe_url
                    if kf and Path(kf).exists():
                        try:
                            b64 = base64.b64encode(Path(kf).read_bytes()).decode()
                            ext = Path(kf).suffix.lstrip(".")
                            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/png")
                            content.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{b64}"},
                            })
                            frames_added += 1
                        except Exception:
                            pass
                    continue

                # Extract first frame
                first_frame = self._extract_video_frames(video_path, num_frames=1, prefix=f"dir_{scene.index}_first")
                last_frame_path = self._extract_last_frame(video_path, scene.index + 1000)  # offset to avoid collision

                for frame_path in (first_frame[:1] if first_frame else []) + ([last_frame_path] if last_frame_path else []):
                    if frame_path and Path(frame_path).exists():
                        try:
                            b64 = base64.b64encode(Path(frame_path).read_bytes()).decode()
                            content.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{b64}"},
                            })
                            frames_added += 1
                            # Clean up temp frame
                            try:
                                Path(frame_path).unlink(missing_ok=True)
                            except Exception:
                                pass
                        except Exception:
                            pass

            if frames_added == 0:
                logger.info(f"Director pass {pass_num+1}: no frames available, skipping")
                break

            try:
                raw = await run_with_timeout(
                    self.client.chat_multimodal(content, model="google/gemini-2.5-pro", max_toks=16384),
                    timeout_sec=180, description=f"Director agent pass {pass_num+1}",
                )
                text = raw.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                    if text.endswith("```"):
                        text = text[:-3]
                    text = text.strip()
                data = json.loads(text)
            except Exception as e:
                logger.warning(f"Director agent pass {pass_num+1} failed: {e}")
                break

            logger.info(f"Director pass {pass_num+1}: score={data.get('overall_score','?')} approved={data.get('approved', True)}")

            if data.get("approved", True):
                break

            raw_regen = data.get("scenes_to_regen", [])[:2]
            scenes_to_regen = []
            for si in raw_regen:
                try:
                    scenes_to_regen.append(int(si))
                except (ValueError, TypeError):
                    logger.warning(f"Director QC: ignoring non-integer scene index {si!r}")
            timeline_notes = data.get("timeline_notes") or {}

            if not scenes_to_regen:
                break

            # Record director notes for these scenes
            for si in scenes_to_regen:
                note = timeline_notes.get(str(si), "")
                if note:
                    director_notes[si] = note

            return director_notes  # Return after first pass — caller regenerates, then we'll be called again if needed

        return director_notes

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
                                  model="anthropic/claude-sonnet-4.6", max_toks=16384, temperature=0.3),
                timeout_sec=60, description=f"Simplify scene {scene.index}",
            )
            text = response.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            data = json.loads(text)
            return (data.get("simplified_description", scene.description),
                    data.get("simplified_camera", "static medium shot"),
                    data.get("negative_constraints", ""))
        except Exception as e:
            logger.warning(f"Scene simplification failed: {e}")
            return scene.description, "static medium shot", ""
