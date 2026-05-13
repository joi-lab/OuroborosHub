"""Pipeline assembly mixin: director QC phase and final video assembly."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .api_client import run_with_timeout
from .models import Job, JobPhase, JobStatus
from .prompts import VIDEO_PROMPT_TEMPLATE
from .pipeline_utils import TIMEOUT_VIDEO

logger = logging.getLogger("video_studio.pipeline_assembly")


class PipelineAssembly:
    """Mixin providing _run_director_qc_phase and _run_assembly_phase.

    Used by Pipeline.resume(). Expects to be composed with PipelineBase
    and PipelineAssets (via Pipeline) so that self.client, self._emit,
    self._warn, self._check_shutdown, self._use_director_qc,
    self._get_resolution, self._run_director_with_video_frames,
    self._run_director_agent_loop, self._build_chars_block,
    self._clamp_duration, self._extract_last_frame,
    self._get_color_grade_filter, self._run_ffmpeg,
    self._get_clip_duration, self._persist_lessons, etc. are available.
    """

    async def _run_director_qc_phase(self, job: Job) -> None:
        """Phase 6: Director cross-scene QC (effort=max only)."""
        s = job.settings
        p = job.progress
        storyboard = p.storyboard
        resolution = self._get_resolution(job)

        if not (self._use_director_qc(job) and storyboard and len(storyboard.scenes) > 1):
            return

        p.phase = JobPhase.DIRECTOR_QC
        p.progress_pct = 90.0
        p.message = "Director cross-scene review…"
        self._emit(job)
        self._check_shutdown()

        # Enhanced video-frames director QC
        video_frames_result = await self._run_director_with_video_frames(job, storyboard)
        scenes_to_regen_from_frames: list[int] = []
        if video_frames_result:
            logger.info(f"Director video-frames QC (resume): score={video_frames_result.get('overall_score', '?')}")
            if not video_frames_result.get("approved", True):
                scenes_to_regen_from_frames = [
                    int(si) for si in video_frames_result.get("scenes_to_regen", [])[:2]
                    if isinstance(si, int) or (isinstance(si, str) and si.isdigit())
                ]

        for director_pass in range(2):
            if director_pass == 0 and scenes_to_regen_from_frames:
                per_scene_notes = video_frames_result.get("per_scene_notes", {})
                director_notes = {
                    si: per_scene_notes.get(str(si), "Major quality or identity issue detected")
                    for si in scenes_to_regen_from_frames
                }
            else:
                director_notes = await self._run_director_agent_loop(job, storyboard)
            if not director_notes:
                break

            p.message = f"Director regen: {len(director_notes)} scene(s)…"
            self._emit(job)

            for si, director_note in director_notes.items():
                if si >= len(storyboard.scenes):
                    continue
                scene = storyboard.scenes[si]
                chars_block = self._build_chars_block(storyboard, scene.characters)
                duration = self._clamp_duration(scene.duration_sec, s.video_model)
                regen_audio_note = (
                    f"AUDIO: Generate voice and dialogue. Dialogue: {scene.dialogue}"
                    if (s.generate_audio and scene.dialogue)
                    else ("AUDIO: Generate ambient sound." if s.generate_audio else "AUDIO: No audio needed.")
                )
                regen_prompt = VIDEO_PROMPT_TEMPLATE.format(
                    scene_description=scene.description,
                    characters_identity_block=chars_block,
                    style=s.style,
                    camera_direction=scene.camera_direction,
                    mood=scene.mood,
                    duration_sec=duration,
                    generate_audio_note=regen_audio_note,
                    continuity_note=f"DIRECTOR NOTE: {director_note}",
                )
                p.message = f"Director regen scene {si+1} (pass {director_pass+1})…"
                self._emit(job)
                try:
                    new_video = await run_with_timeout(
                        self.client.generate_video(
                            prompt=regen_prompt,
                            filename=f"scene_{si}_dir_p{director_pass}.mp4",
                            model=s.video_model,
                            duration=duration,
                            resolution=resolution,
                            aspect_ratio="16:9",
                            generate_audio=s.generate_audio,
                            frame_images=None,
                            input_references=None,
                        ),
                        timeout_sec=TIMEOUT_VIDEO, description=f"Director regen s{si} pass {director_pass+1}",
                    )
                    if new_video:
                        if scene.video_url and str(scene.video_url) in p.video_clips:
                            idx = p.video_clips.index(str(scene.video_url))
                            p.video_clips[idx] = str(new_video)
                        scene.video_url = str(new_video)
                        lf = self._extract_last_frame(new_video, si)
                        if lf:
                            scene.prev_frame_url = lf
                except Exception as e:
                    self._warn(job, f"Director regen scene {si} pass {director_pass+1} failed: {e}")

    async def _run_assembly_phase(self, job: Job) -> None:
        """Phase 7: Final video assembly via ffmpeg (resume path — simpler, no transitions)."""
        p = job.progress

        p.phase = JobPhase.ASSEMBLY
        p.progress_pct = 93.0
        p.message = "Planning color grade…"
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

        # Get color grade filter for style cohesion
        color_filter = await self._get_color_grade_filter(job)

        output_dir = self.state_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(output_dir / f"{job.job_id}_final.mp4")

        music_path = p.music_clips[0] if p.music_clips else None
        has_music = music_path and Path(music_path).exists()

        p.message = "Assembling final video…"
        self._emit(job)

        # Apply color grading to clips if we have multiple
        graded_clips = []
        if color_filter and len(video_clips) > 1:
            for idx, vc in enumerate(video_clips):
                graded_path = str(self.state_dir / f"_graded_{idx}.mp4")
                try:
                    r = self._run_ffmpeg([
                        "ffmpeg", "-y", "-i", vc,
                        "-vf", color_filter,
                        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                        "-c:a", "copy",
                        graded_path,
                    ], timeout=60)
                    if r.returncode == 0 and Path(graded_path).exists():
                        graded_clips.append(graded_path)
                    else:
                        graded_clips.append(vc)
                except Exception:
                    graded_clips.append(vc)
        else:
            graded_clips = list(video_clips)

        if len(graded_clips) == 1 and not has_music:
            shutil.copy(graded_clips[0], output_path)
        else:
            concat_list = self.state_dir / "concat_list.txt"
            concat_list.write_text(
                "\n".join(f"file '{vc}'" for vc in graded_clips), encoding="utf-8"
            )
            if has_music:
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
                shutil.copy(graded_clips[0], output_path)
                self._warn(job, f"ffmpeg assembly failed (code {r.returncode}), using first clip")

        # Clean up temp graded files
        for tmp in self.state_dir.glob("_graded_*.mp4"):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

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
