"""Quality-control helpers extracted from pipeline.py.

Each function takes a ``pipeline`` (Pipeline instance) as its first
argument so it can access the client, state directory, and other
Pipeline methods/attributes without being a method itself.
"""
from __future__ import annotations

import base64 as _b64
import json as _json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .api_client import run_with_timeout

if TYPE_CHECKING:
    from .models import Character, Job, Scene, Storyboard
    from .pipeline import Pipeline

logger = logging.getLogger(__name__)

# Re-import the constant used by _run_director_with_video_frames
from .pipeline import TIMEOUT_DIRECTOR_QC


# ── 1. Character DNA ────────────────────────────────────────────────

async def extract_character_dna(pipeline: "Pipeline", job: "Job", char: "Character") -> str:
    """Extract VLM-based character DNA after character sheet generation.
    Returns a frozen ultra-specific descriptor for use in all video prompts."""
    from .prompts import CHARACTER_DNA_EXTRACT_PROMPT
    if not char.sheet_url or not Path(char.sheet_url).exists():
        # Fallback to original visual_traits if no sheet available
        return f"{char.name}: {char.visual_traits}"
    try:
        prompt = CHARACTER_DNA_EXTRACT_PROMPT.format(
            name=char.name,
            visual_traits=char.visual_traits,
        )
        result = await run_with_timeout(
            pipeline.client.analyze_image_vlm_text(char.sheet_url, prompt),
            timeout_sec=45, description=f"Character DNA extraction {char.name}",
        )
        if result and len(result.strip()) > 20:
            return result.strip()
    except Exception as e:
        logger.warning(f"Character DNA extraction failed for {char.name}: {e}")
    return f"{char.name}: {char.visual_traits}"


# ── 2. Prompt variant selection ─────────────────────────────────────

async def select_best_video_prompt(
    pipeline: "Pipeline", job: "Job", scene: "Scene",
    storyboard: "Storyboard", base_prompt: str, char_dna: str,
) -> str:
    """Generate 3 prompt variants and let a critic select the best one.
    This is cheap ($0.01) vs expensive video generation."""
    from .prompts import VIDEO_PROMPT_VARIANT_CRITIC_PROMPT
    s = job.settings

    # Assess motion complexity (1-5) based on scene description keywords
    motion_keywords = ["run", "fight", "jump", "chase", "explode", "sprint", "battle", "crash",
                       "collide", "rapid", "swift", "quick"]
    desc_lower = scene.description.lower()
    motion_complexity = 1 + min(4, sum(1 for k in motion_keywords if k in desc_lower) * 2)

    audio_note = (f"AUDIO: Generate voice and dialogue. Dialogue: {scene.dialogue}"
                  if (s.generate_audio and scene.dialogue)
                  else ("AUDIO: Generate ambient sound." if s.generate_audio else "AUDIO: No audio."))

    duration = pipeline._clamp_duration(scene.duration_sec, s.video_model)

    # Variant A: continuity-heavy (character DNA first)
    variant_a = f"{char_dna}\n\n{base_prompt}"

    # Variant B: action-first (action description first, then identity)
    variant_b = (
        f"SCENE ACTION: {scene.description}\n\n"
        f"CHARACTER IDENTITY (maintain exactly):\n{char_dna}\n\n"
        f"Camera: {scene.camera_direction}\nMood: {scene.mood}\n"
        f"Style: Photorealistic cinema, {s.style}\n"
        f"Duration: {duration}s\n{audio_note}\n\n"
        f"REQUIREMENTS:\n- Photorealistic quality, natural human motion\n"
        f"- NO text overlays, NO subtitles, NO anime/cartoon\n"
        f"- Characters maintain exact visual identity throughout"
    )

    # Variant C: cinematic-cue (references film language)
    variant_c = (
        f"{base_prompt}\n\n"
        f"CINEMATIC REFERENCE: {s.style} — shoot this like a Hollywood production "
        f"with {scene.camera_direction}. Character identity is CRITICAL: {char_dna[:100]}..."
    )

    critic_prompt = VIDEO_PROMPT_VARIANT_CRITIC_PROMPT.format(
        scene_description=scene.description,
        character_dna=char_dna[:200],
        motion_complexity=motion_complexity,
        variant_a=variant_a[:500],
        variant_b=variant_b[:500],
        variant_c=variant_c[:500],
    )
    try:
        response = await run_with_timeout(
            pipeline.client.chat(
                messages=[{"role": "user", "content": critic_prompt}],
                model="anthropic/claude-sonnet-4.6",
                max_toks=512,
                temperature=0.1,
            ),
            timeout_sec=30, description=f"Prompt variant selection s{scene.index}",
        )
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        data = _json.loads(text)
        winner = data.get("winner", "A")
        tweak = data.get("suggested_tweak", "")
        chosen = {"A": variant_a, "B": variant_b, "C": variant_c}.get(winner, variant_a)
        if tweak:
            chosen = f"{chosen}\n\nSPECIAL ATTENTION: {tweak}"
        logger.info(f"Scene {scene.index}: selected prompt variant {winner} ({data.get('reason', '')})")
        return chosen
    except Exception as e:
        logger.warning(f"Prompt variant selection failed for s{scene.index}: {e} — using base prompt")
        return variant_a  # default to continuity-heavy


# ── 3. Diagnosis retry prompt ───────────────────────────────────────

async def build_diagnosis_retry_prompt(
    pipeline: "Pipeline", job: "Job", original_prompt: str,
    critique: dict, scene: "Scene",
) -> str:
    """Use LLM to surgically rewrite the prompt based on failure diagnosis."""
    from .prompts import VIDEO_RETRY_DIAGNOSIS_PROMPT
    failing = [k for k, v in critique.get("scores", {}).items() if v < 6.5]
    if not failing:
        failing = ["general quality"]
    try:
        result = await run_with_timeout(
            pipeline.client.chat(
                messages=[{"role": "user", "content": VIDEO_RETRY_DIAGNOSIS_PROMPT.format(
                    original_prompt=original_prompt[:1000],
                    critique_json=_json.dumps(critique.get("scores", {})),
                    failing_dimensions=", ".join(failing),
                )}],
                model="anthropic/claude-sonnet-4.6",
                max_toks=1024,
                temperature=0.2,
            ),
            timeout_sec=30, description=f"Diagnosis retry prompt s{scene.index}",
        )
        if result and len(result.strip()) > 50:
            return result.strip()
    except Exception as e:
        logger.warning(f"Diagnosis retry failed for s{scene.index}: {e}")
    return original_prompt  # fallback to original


# ── 4. Director QC with video frames ───────────────────────────────

async def run_director_with_video_frames(
    pipeline: "Pipeline", job: "Job", storyboard: "Storyboard",
) -> dict:
    """Enhanced director QC using actual video frames (contact sheets), not just keyframes."""
    from .prompts import DIRECTOR_VIDEO_FRAMES_PROMPT

    chars_desc = "\n".join(f"- {c.name}: {c.visual_traits}" for c in storyboard.characters)
    synopsis = storyboard.synopsis or job.settings.theme

    content = []
    content.append({"type": "text", "text": DIRECTOR_VIDEO_FRAMES_PROMPT.format(
        characters_description=chars_desc,
        style=storyboard.style,
        synopsis=synopsis,
    )})

    scene_frame_counts = 0
    for scene in storyboard.scenes:
        if not scene.video_url or not Path(str(scene.video_url)).exists():
            continue
        frames = pipeline._extract_video_frames(str(scene.video_url), num_frames=5,
                                                 prefix=f"dir_s{scene.index}")
        content.append({"type": "text", "text": f"\n--- Scene {scene.index}: {scene.description[:80]} ---"})
        for frame_path in frames:
            if Path(frame_path).exists():
                try:
                    with open(frame_path, "rb") as f:
                        b64 = _b64.b64encode(f.read()).decode()
                    content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
                    scene_frame_counts += 1
                except Exception:
                    pass
        # Clean up frames after encoding
        for frame_path in frames:
            try:
                Path(frame_path).unlink(missing_ok=True)
            except Exception:
                pass

    if scene_frame_counts == 0:
        return {}

    try:
        response = await run_with_timeout(
            pipeline.client.chat_multimodal(content, model="google/gemini-2.5-pro", max_toks=16384),
            timeout_sec=TIMEOUT_DIRECTOR_QC * 2, description="Director video-frames QC",
        )
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        return _json.loads(text)
    except Exception as e:
        logger.warning(f"Director video-frames QC failed: {e}")
        return {}


# ── 5. Transition planning ──────────────────────────────────────────

async def plan_transitions(
    pipeline: "Pipeline", job: "Job", storyboard: "Storyboard",
) -> list:
    """Ask LLM to plan scene transitions for better cinematic flow."""
    from .prompts import TRANSITION_PLANNER_PROMPT

    if len(storyboard.scenes) < 2:
        return []

    scene_sequence = "\n".join(
        f"Scene {s.index}: {s.description[:80]} ({s.duration_sec:.0f}s, mood: {s.mood})"
        for s in storyboard.scenes
    )
    try:
        response = await run_with_timeout(
            pipeline.client.chat(
                messages=[{"role": "user", "content": TRANSITION_PLANNER_PROMPT.format(
                    scene_sequence=scene_sequence,
                )}],
                model="anthropic/claude-sonnet-4.6",
                max_toks=512,
                temperature=0.2,
            ),
            timeout_sec=20, description="Transition planning",
        )
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        return _json.loads(text)
    except Exception as e:
        logger.warning(f"Transition planning failed: {e}")
        return []


# ── 6. Color grading ────────────────────────────────────────────────

async def get_color_grade_filter(pipeline: "Pipeline", job: "Job") -> str:
    """Ask LLM to generate appropriate ffmpeg color filter for style cohesion."""
    from .prompts import COLOR_GRADE_PLANNER_PROMPT
    s = job.settings
    try:
        response = await run_with_timeout(
            pipeline.client.chat(
                messages=[{"role": "user", "content": COLOR_GRADE_PLANNER_PROMPT.format(
                    style=s.style,
                    mood=s.mood,
                )}],
                model="anthropic/claude-sonnet-4.6",
                max_toks=100,
                temperature=0.1,
            ),
            timeout_sec=15, description="Color grade planning",
        )
        result = response.strip().strip('"').strip("'")
        # Validate it looks like an ffmpeg filter
        if any(c in result for c in ["eq=", "colorbalance", "curves", "unsharp"]) and len(result) < 300:
            return result
    except Exception as e:
        logger.warning(f"Color grade planning failed: {e}")
    return "eq=contrast=1.03:saturation=0.97"  # safe default
