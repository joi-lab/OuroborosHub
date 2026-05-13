"""Core generation pipeline for Video Studio with:
- Effort-based quality control (low/regular/max)
- Parallel best-of-N candidate generation
- Gemini 2.5 Pro AV QC (effort=max)
- Director cross-scene QC pass (effort=max)
- Progressive prompt learning
- Scene continuity chain with frame_images anchoring
- Adaptive scene simplification

This module is the thin orchestrator: Pipeline.__init__, run(), and resume()
live here. Helpers, asset generation, and assembly phases are split across
pipeline_utils, pipeline_assets, and pipeline_assembly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .api_client import run_with_timeout
from .models import (
    Character,
    Job,
    JobPhase,
    JobStatus,
    Location,
    MusicCue,
    Scene,
    Storyboard,
)
from .prompts import (
    IMAGE_KEYFRAME_PROMPT,
    IMAGE_KEYFRAME_SEQUENTIAL_PROMPT,
    MUSIC_PROMPT_TEMPLATE,
    SCENARIO_SYSTEM,
    SCENARIO_USER_TEMPLATE,
    VIDEO_PROMPT_TEMPLATE,
)
from .pipeline_utils import (
    PipelineBase,
    # Re-export all constants for backward compatibility
    # (quality.py and other modules use `from .pipeline import TIMEOUT_*`)
    TIMEOUT_SCENARIO,
    TIMEOUT_IMAGE,
    TIMEOUT_MUSIC,
    TIMEOUT_VIDEO,
    TIMEOUT_VERIFY,
    TIMEOUT_VIDEO_VERIFY,
    TIMEOUT_GEMINI_QC,
    TIMEOUT_DIRECTOR_QC,
    MAX_VERIFY_RETRIES,
    MAX_VIDEO_VERIFY_RETRIES,
    MULTIDIM_PASS_THRESHOLD,
    MULTIDIM_WEIGHTS,
    _STDERR_CAP_BYTES,
    _LESSONS_FILENAME,
    EFFORT_CANDIDATES,
    EFFORT_IMAGE_RETRIES,
    EFFORT_VIDEO_RETRIES,
    EFFORT_USE_GEMINI_QC,
    EFFORT_USE_DIRECTOR_QC,
    VALID_RESOLUTIONS,
    DEFAULT_RESOLUTION,
)
from .pipeline_assets import PipelineAssets
from .pipeline_assembly import PipelineAssembly

logger = logging.getLogger("video_studio.pipeline")

# Make constants available at module level for `from .pipeline import X`
__all__ = [
    "Pipeline",
    "TIMEOUT_SCENARIO", "TIMEOUT_IMAGE", "TIMEOUT_MUSIC", "TIMEOUT_VIDEO",
    "TIMEOUT_VERIFY", "TIMEOUT_VIDEO_VERIFY", "TIMEOUT_GEMINI_QC", "TIMEOUT_DIRECTOR_QC",
    "MAX_VERIFY_RETRIES", "MAX_VIDEO_VERIFY_RETRIES",
    "MULTIDIM_PASS_THRESHOLD", "MULTIDIM_WEIGHTS",
    "EFFORT_CANDIDATES", "EFFORT_IMAGE_RETRIES", "EFFORT_VIDEO_RETRIES",
    "EFFORT_USE_GEMINI_QC", "EFFORT_USE_DIRECTOR_QC",
    "VALID_RESOLUTIONS", "DEFAULT_RESOLUTION",
]


class Pipeline(PipelineBase, PipelineAssets, PipelineAssembly):
    """Video Studio pipeline: cinematic photorealistic video with
    effort-based quality control, parallel candidate generation,
    Gemini AV QC, and Director cross-scene review.

    Inherits helpers from PipelineBase, asset generation from
    PipelineAssets, and assembly phases from PipelineAssembly.
    """

    # ─── Main run() ─────────────────────────────────────────────────

    async def run(self, job: Job):
        """Run the full pipeline end-to-end."""
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
        scenario_model = "anthropic/claude-opus-4.6" if self._get_effort(job) == "max" else "anthropic/claude-sonnet-4.6"
        scenario_max_toks = 16384 if self._get_effort(job) == "max" else 8192
        try:
            scenario_raw = await run_with_timeout(
                self.client.chat(
                    messages=[
                        {"role": "system", "content": SCENARIO_SYSTEM},
                        {"role": "user", "content": scenario_prompt},
                    ],
                    model=scenario_model, max_toks=scenario_max_toks, temperature=0.8,
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
        try:
            text = scenario_raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            scenario_data = json.loads(text)
        except Exception:
            m = re.search(r"\{[\s\S]+\}", scenario_raw)
            if m:
                try:
                    scenario_data = json.loads(m.group())
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

        chars = [Character(name=c.get("name", "Unknown"), description=c.get("description", ""),
                            visual_traits=c.get("visual_traits", c.get("description", "")))
                 for c in scenario_data.get("characters", [])]
        locs = [Location(name=l.get("name", "Unknown"), description=l.get("description", ""),
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
                causal_link=sc.get("causal_link"),
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
        p.progress_pct = 33.0
        # Abort if no character sheets at all (video would have no consistent characters)
        if chars and not p.character_sheets:
            p.phase = JobPhase.ERROR
            p.status = JobStatus.ERROR
            p.error = "All character sheet generations failed — cannot produce a video without character identity references"
            p.message = str(p.error)
            self._emit(job)
            return
        p.message = f"Character sheets done ({len(p.character_sheets)}/{len(chars)}). Extracting character identity anchors…"
        self._emit(job)
        self._check_shutdown()

        # ── Character DNA extraction (after sheets are generated) ──────────
        dna_tasks = [
            self._extract_character_dna(job, char)
            for char in storyboard.characters
        ]
        dna_results = await asyncio.gather(*dna_tasks, return_exceptions=True)
        for char, dna_result in zip(storyboard.characters, dna_results):
            if isinstance(dna_result, Exception) or not dna_result:
                self._character_dna[char.name] = f"{char.name}: {char.visual_traits}"
            else:
                self._character_dna[char.name] = str(dna_result)
                logger.info(f"Character DNA extracted for {char.name}")

        p.progress_pct = 35.0
        p.message = f"Identity anchors ready. Generating keyframes…"
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
                prev_frame = str(kf_path)  # feed into next scene's sequential prompt
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
        resolution = self._get_resolution(job)
        effort = self._get_effort(job)
        for i, scene in enumerate(storyboard.scenes):
            self._check_shutdown()
            chars_block = self._build_chars_block(storyboard, scene.characters)
            duration = self._clamp_duration(scene.duration_sec, s.video_model)

            # Build character DNA block for this scene
            scene_char_dna = "\n".join(
                self._character_dna.get(cn, cn) for cn in scene.characters
            ) if scene.characters else chars_block

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

            # For regular/max effort, use prompt variant selection
            if effort != "low" and scene_char_dna:
                try:
                    video_prompt = await self._select_best_video_prompt(
                        job, scene, storyboard, video_prompt, scene_char_dna)
                except Exception as e:
                    logger.warning(f"Prompt variant selection skipped for s{i}: {e}")

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
                    video_path = await run_with_timeout(
                        self.client.generate_video(
                            prompt=video_prompt,
                            filename=f"scene_{i}_a{attempt}.mp4",
                            model=s.video_model,
                            duration=duration,
                            resolution=resolution,
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
                    # Use diagnosis-based retry for regular/max effort
                    if effort != "low":
                        video_prompt = await self._build_diagnosis_retry_prompt(
                            job, video_prompt, qc, scene)
                    else:
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
                    p.quality_reports.append(asdict(qr))
                # Extract last frame for continuity
                lf = self._extract_last_frame(video_path, i)
                if lf:
                    scene.prev_frame_url = lf
                    prev_frame = lf

            p.progress_pct = 60.0 + (i + 1) / len(storyboard.scenes) * 30.0
            self._emit(job)

        # ── Phase 6: DIRECTOR AGENT LOOP (effort=max) ──────────────
        if self._use_director_qc(job) and len(storyboard.scenes) > 1:
            p.phase = JobPhase.DIRECTOR_QC
            p.progress_pct = 90.0
            p.message = "Director cross-scene review…"
            self._emit(job)
            self._check_shutdown()

            # Enhanced video-frames director QC for effort=max
            video_frames_result = await self._run_director_with_video_frames(job, storyboard)
            scenes_to_regen_from_frames: list[int] = []
            if video_frames_result:
                logger.info(f"Director video-frames QC: score={video_frames_result.get('overall_score', '?')}")
                if not video_frames_result.get("approved", True):
                    scenes_to_regen_from_frames = [
                        int(si) for si in video_frames_result.get("scenes_to_regen", [])[:2]
                        if isinstance(si, int) or (isinstance(si, str) and si.isdigit())
                    ]

            for director_pass in range(2):  # max 2 director passes
                # First pass uses video-frames result if available, otherwise standard director loop
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
                            # Extract new last frame for continuity
                            lf = self._extract_last_frame(new_video, si)
                            if lf:
                                scene.prev_frame_url = lf
                    except Exception as e:
                        self._warn(job, f"Director regen scene {si} pass {director_pass+1} failed: {e}")

        # ── Phase 7: ASSEMBLY ──────────────────────────────────────
        p.phase = JobPhase.ASSEMBLY
        p.progress_pct = 93.0
        p.message = "Planning transitions and color grade…"
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

        # Plan transitions and color grade in parallel (cheap LLM calls)
        transition_task = self._plan_transitions(job, storyboard)
        color_task = self._get_color_grade_filter(job)
        transitions, color_filter = await asyncio.gather(
            transition_task, color_task, return_exceptions=True,
        )
        if isinstance(transitions, Exception):
            self._warn(job, f"Transition planning failed: {transitions}")
            transitions = []
        if isinstance(color_filter, Exception):
            self._warn(job, f"Color grade planning failed: {color_filter}")
            color_filter = "eq=contrast=1.03:saturation=0.97"

        output_dir = self.state_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(output_dir / f"{job.job_id}_final.mp4")

        music_path = p.music_clips[0] if p.music_clips else None
        has_music = music_path and Path(music_path).exists()

        p.message = "Assembling final video…"
        self._emit(job)

        # Build transition lookup: {to_scene_index: {type, duration_sec}}
        transition_map: dict[int, dict] = {}
        if isinstance(transitions, list):
            for t in transitions:
                if isinstance(t, dict) and "to_scene" in t:
                    transition_map[t["to_scene"]] = {
                        "type": t.get("type", "cut"),
                        "duration_sec": min(0.8, max(0.0, float(t.get("duration_sec", 0.0)))),
                    }

        # Apply color grading to individual clips if we have a valid filter
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
                        graded_clips.append(vc)  # fallback to original
                except Exception:
                    graded_clips.append(vc)
        else:
            graded_clips = list(video_clips)

        # Apply crossfade/fade transitions between clips if planned
        has_transitions = any(
            t.get("type") in ("crossfade", "fade_black") and t.get("duration_sec", 0) > 0
            for t in transition_map.values()
        )

        if len(graded_clips) == 1 and not has_music:
            shutil.copy(graded_clips[0], output_path)
            r = type("R", (), {"returncode": 0})()
        elif has_transitions and len(graded_clips) > 1:
            # Build complex filter graph with transitions
            # For simplicity, apply crossfades using xfade filter between consecutive clips
            current_clip = graded_clips[0]
            for idx in range(1, len(graded_clips)):
                trans = transition_map.get(idx, {"type": "cut", "duration_sec": 0.0})
                trans_type = trans.get("type", "cut")
                trans_dur = trans.get("duration_sec", 0.0)

                if trans_type == "cut" or trans_dur <= 0:
                    # Hard cut: just concat
                    tmp_concat = str(self.state_dir / f"_trans_{idx}.txt")
                    Path(tmp_concat).write_text(
                        f"file '{current_clip}'\nfile '{graded_clips[idx]}'", encoding="utf-8")
                    tmp_out = str(self.state_dir / f"_trans_{idx}.mp4")
                    r = self._run_ffmpeg([
                        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                        "-i", tmp_concat, "-c", "copy", tmp_out,
                    ], timeout=60)
                    if r.returncode == 0 and Path(tmp_out).exists():
                        current_clip = tmp_out
                    else:
                        # Fallback: skip transition
                        pass
                elif trans_type == "crossfade":
                    tmp_out = str(self.state_dir / f"_xfade_{idx}.mp4")
                    offset = max(0.5, self._get_clip_duration(current_clip) - trans_dur)
                    r = self._run_ffmpeg([
                        "ffmpeg", "-y",
                        "-i", current_clip, "-i", graded_clips[idx],
                        "-filter_complex",
                        f"[0:v][1:v]xfade=transition=fade:duration={trans_dur:.2f}:offset={offset:.2f}[v]",
                        "-map", "[v]",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                        "-an", tmp_out,
                    ], timeout=90)
                    if r.returncode == 0 and Path(tmp_out).exists():
                        current_clip = tmp_out
                    else:
                        # Fallback to hard cut
                        tmp_concat = str(self.state_dir / f"_trans_{idx}.txt")
                        Path(tmp_concat).write_text(
                            f"file '{current_clip}'\nfile '{graded_clips[idx]}'", encoding="utf-8")
                        tmp_out2 = str(self.state_dir / f"_trans_{idx}.mp4")
                        self._run_ffmpeg([
                            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                            "-i", tmp_concat, "-c", "copy", tmp_out2,
                        ], timeout=60)
                        if Path(tmp_out2).exists():
                            current_clip = tmp_out2
                elif trans_type == "fade_black":
                    tmp_out = str(self.state_dir / f"_fade_{idx}.mp4")
                    offset = max(0.5, self._get_clip_duration(current_clip) - trans_dur)
                    r = self._run_ffmpeg([
                        "ffmpeg", "-y",
                        "-i", current_clip, "-i", graded_clips[idx],
                        "-filter_complex",
                        f"[0:v][1:v]xfade=transition=fadeblack:duration={trans_dur:.2f}:offset={offset:.2f}[v]",
                        "-map", "[v]",
                        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                        "-an", tmp_out,
                    ], timeout=90)
                    if r.returncode == 0 and Path(tmp_out).exists():
                        current_clip = tmp_out
                    else:
                        tmp_concat = str(self.state_dir / f"_trans_{idx}.txt")
                        Path(tmp_concat).write_text(
                            f"file '{current_clip}'\nfile '{graded_clips[idx]}'", encoding="utf-8")
                        tmp_out2 = str(self.state_dir / f"_trans_{idx}.mp4")
                        self._run_ffmpeg([
                            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                            "-i", tmp_concat, "-c", "copy", tmp_out2,
                        ], timeout=60)
                        if Path(tmp_out2).exists():
                            current_clip = tmp_out2

            # Now current_clip is the assembled video-only track; add music if needed
            if has_music:
                r = self._run_ffmpeg([
                    "ffmpeg", "-y",
                    "-i", current_clip, "-i", str(music_path),
                    "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:weights=1 0.35[aout]",
                    "-map", "0:v:0", "-map", "[aout]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                    output_path
                ], timeout=180)
                if r.returncode != 0:
                    r = self._run_ffmpeg([
                        "ffmpeg", "-y",
                        "-i", current_clip, "-i", str(music_path),
                        "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                        output_path
                    ], timeout=180)
                if r.returncode != 0:
                    shutil.copy(current_clip, output_path)
            else:
                # Add back audio from original clips if available
                # For simplicity, just copy the transitioned video
                shutil.copy(current_clip, output_path)
                r = type("R", (), {"returncode": 0})()
        else:
            # Simple concat (no transitions or single clip with music)
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

        # Clean up temp graded/transition files
        for tmp in self.state_dir.glob("_graded_*.mp4"):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        for tmp in self.state_dir.glob("_trans_*"):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        for tmp in self.state_dir.glob("_xfade_*.mp4"):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        for tmp in self.state_dir.glob("_fade_*.mp4"):
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

    # ─── Resume ─────────────────────────────────────────────────────

    async def resume(self, job: Job) -> None:
        """Resume an interrupted job from Director QC / Assembly.

        Requires: persisted storyboard and at least one existing video clip.
        Skips screenplay, character refs, keyframes, music, and base video
        generation -- picks up from Director QC then assembles.
        """
        p = job.progress
        p.status = JobStatus.RUNNING
        p.error = None
        p.phase = JobPhase.DIRECTOR_QC
        p.progress_pct = 88.0
        p.message = "Resuming interrupted job…"
        self._emit(job)

        # Rebuild video_clips from scene.video_url when the progress list is stale
        storyboard = p.storyboard
        if storyboard:
            scene_clips = [
                str(sc.video_url) for sc in storyboard.scenes
                if sc.video_url and Path(sc.video_url).exists()
            ]
            live_clips = [v for v in p.video_clips if v and Path(v).exists()]
            if len(scene_clips) > len(live_clips):
                p.video_clips = scene_clips

        # Director QC (best-effort; assembly proceeds even if this fails/skips)
        try:
            await self._run_director_qc_phase(job)
        except Exception as e:
            self._warn(job, f"Director QC skipped on resume: {e}")

        # Assembly always runs
        await self._run_assembly_phase(job)
