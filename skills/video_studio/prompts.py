"""LLM prompt templates for cinematic storyboard/scenario generation and VLM verification."""

SCENARIO_SYSTEM = """You are a professional Hollywood screenwriter and cinematographer (think Christopher Nolan / Roger Deakins style).
You create structured storyboards for short photorealistic cinematic productions with strong
narrative continuity between scenes. Every scene must flow into the next with clear visual
and narrative connections — NOT isolated static shots.

NARRATIVE ARCHITECTURE (MANDATORY):
- Apply strict "CAUSAL CHAIN" logic: every scene must follow from the previous one via cause-and-effect
- Use "BECAUSE OF THAT / BUT" logic: Scene B exists because of what happened in Scene A, OR as a contrast/consequence
- FORBIDDEN: Pure "AND THEN" sequences where scenes are just chronological but unrelated events
- Each scene must have a narrative PURPOSE: setup → complication → escalation → climax → resolution
- Think in story ACTS: 3-scene = 3 acts, 5-scene = setup+3 rising action+climax, 8-scene = full arc

Your output is ALWAYS valid JSON matching the schema below.
Focus on photorealistic visual storytelling, dramatic framing, character consistency,
SMOOTH TRANSITIONS between scenes, and precise cinematographic language:
lens type, focal length, color temperature, camera movement, and production design notes."""

SCENARIO_USER_TEMPLATE = """Create a detailed storyboard for a short cinematic video production.

## Requirements:
- **Theme/Plot**: {theme}
- **Visual Style**: {style}
- **Total Duration**: {duration_sec} seconds
- **Number of Scenes**: {num_scenes}
- **Overall Mood**: {mood}
- **Include Dialogue**: {include_dialogue}
- **Music Style**: {music_style}

## Output JSON Schema:
{{
  "title": "string — compelling film title",
  "synopsis": "string — 2-3 sentence synopsis with clear narrative arc (beginning, conflict, resolution)",
  "style": "string — specific visual style (color palette, lighting approach, cinematographic aesthetic)",
  "total_duration_sec": number,
  "characters": [
    {{
      "name": "string",
      "description": "string — personality, background, and role in the story",
      "visual_traits": "string — EXTREMELY DETAILED photorealistic description: ethnicity, age, hair color/length/style, eye color, facial features, build, exact outfit (fabrics, colors, cut), accessories, distinguishing marks. Be precise enough for an actor casting call."
    }}
  ],
  "locations": [
    {{
      "name": "string",
      "description": "string — narrative role and atmosphere",
      "visual_traits": "string — EXTREMELY DETAILED: time of day, natural/artificial lighting, color temperature (warm/cool/neutral), weather/atmosphere, architectural style, specific props and set dressing, color palette, texture details"
    }}
  ],
  "scenes": [
    {{
      "index": number (0-based),
      "description": "string — what happens in this scene. Describe the MOTION and ACTION in cinematic terms, not a static pose.",
      "duration_sec": number (4-15),
      "characters": ["character_name", ...],
      "location": "location_name",
      "camera_direction": "string — precise cinematographic direction: lens type (e.g. '85mm prime anamorphic'), camera movement ('slow push in', 'tracking shot left to right'), framing ('medium close-up', 'over-the-shoulder'), depth of field ('shallow, subject sharp background bokeh')",
      "dialogue": "string or null — spoken English dialogue for this scene. Keep concise (1-2 sentences per character)",
      "mood": "string — emotional tone",
      "transition_from": "string or null — cinematic transition connecting from previous scene. null for first scene only. Examples: 'hard cut from close-up to wide establishing shot', 'match cut on hand movement', 'dissolve through window reflection'",
      "causal_link": "string — why this scene MUST exist because of the previous scene. E.g. 'Having discovered the artifact, she now faces the consequence: the entire station is responding to her intrusion.' First scene: 'This is the inciting incident that sets the story in motion.'",
      "lens_type": "string — e.g. '85mm anamorphic' or '24mm wide angle'",
      "color_temperature": "string — e.g. 'warm golden 5600K', 'cool blue 3200K', 'neutral daylight'",
      "lighting_setup": "string — e.g. 'natural window key light, practical lamp fill', 'high-contrast noir side-lighting'"
    }}
  ],
  "music_cues": [
    {{
      "segment_index": number (0-based),
      "mood": "string",
      "tempo": "slow|medium|fast",
      "style": "string — instrument/genre description",
      "duration_sec": number,
      "description": "string — detailed description of what the music should sound like"
    }}
  ]
}}

## Rules:
1. Scene durations must sum to approximately {duration_sec} seconds (±10%)
2. Each scene: 4-15 seconds
3. Characters must appear in at least one scene
4. Every scene references a defined location
5. Music cues should cover the full duration
6. Camera directions MUST use professional cinematographic language with specific lens types
7. If dialogue is included, write natural English dialogue (1-2 lines per scene max)
8. Visual traits must be EXTREMELY detailed — these drive photorealistic image and video generation
9. Create 1-3 characters max for identity consistency
10. Create 2-4 locations max
11. Each scene (except the first) MUST have "transition_from" — critical for visual continuity
12. Scenes should describe MOVEMENT and ACTION in cinematic terms
13. Every scene (except the first) MUST have "causal_link" explaining WHY this scene follows from the previous — this is the most important rule for narrative coherence

Respond with ONLY the JSON object, no markdown fences, no explanation."""


IMAGE_CHARACTER_SHEET_PROMPT = """Generate a photorealistic character reference portrait for:

Character: {name}
Visual Description: {visual_traits}
Cinematic Style: {style}

Show 3 views: front-facing portrait, 3/4 angle, side profile.
PHOTOREALISTIC, professional photography lighting, cinema quality.
Sharp detail, natural skin tones, {style} lighting aesthetic.
NO animation, NO illustration, NO cartoon style — real human photography quality.
Neutral background for reference clarity."""


IMAGE_LOCATION_PROMPT = """Generate a photorealistic cinematic establishing shot:

Location: {name}
Visual Description: {visual_traits}
Cinematic Style: {style}

Wide establishing shot. Rich cinematic detail. Professional cinematography quality.
{style} color palette and atmosphere. Suitable as a scene backdrop.
NO characters in the image. PHOTOREALISTIC — not illustration, not CGI."""


IMAGE_KEYFRAME_PROMPT = """Generate a photorealistic cinematic still frame:

Scene: {scene_description}
Characters present: {characters}
Location: {location_description}
Camera: {camera_direction}
Mood: {mood}
Visual Style: {style}

This is a key moment showing the ACTION in progress.
Shot on ARRI ALEXA LF, {camera_direction}.
PHOTOREALISTIC cinema quality — NO anime, NO illustration, NO cartoon.
Cinematic color grade, natural motion, real human expressions.
Professional cinematography framing."""


IMAGE_KEYFRAME_SEQUENTIAL_PROMPT = """Generate a photorealistic cinematic still frame:

Scene: {scene_description}
Characters present: {characters}
Location: {location_description}
Camera: {camera_direction}
Mood: {mood}
Visual Style: {style}

CONTINUITY FROM PREVIOUS KEYFRAME:
{prev_keyframe_context}

This keyframe MUST visually connect to the previous one. Characters should look IDENTICAL
(same face, hair, outfit, accessories) — only pose and expression change.
PHOTOREALISTIC cinema quality — NO anime, NO illustration, NO cartoon.
Shot on ARRI ALEXA LF. Cinematic color grade, natural motion.

Characters MUST match these exact visual descriptions:
{characters_identity_block}"""


VIDEO_PROMPT_TEMPLATE = """Photorealistic cinematic scene.

{scene_description}

Characters in this scene:
{characters_identity_block}

Camera: {camera_direction}
Mood: {mood}
Style: Photorealistic cinema, {style}
Duration: {duration_sec} seconds

{generate_audio_note}
{continuity_note}

REQUIREMENTS:
- Photorealistic quality, natural human motion and expressions
- Cinematic color grading and lighting — NO anime, NO illustration, NO CGI
- Characters maintain their exact visual identity throughout
- {style} visual language consistently applied

NEGATIVE CONSTRAINTS (NEVER include):
- NO text overlays, NO subtitles, NO captions, NO watermarks, NO floating letters
- NO anime art style, NO cartoon rendering, NO obvious CGI artifacts
- NO robotic or unnatural movement
- NO brand logos, NO timecodes"""


MUSIC_PROMPT_TEMPLATE = """Compose a short instrumental piece for a cinematic scene:

Mood: {mood}
Tempo: {tempo}
Style: {style}
Duration: approximately {duration_sec} seconds

Description: {description}

The music should be cinematic, emotional, and suitable for a photorealistic film production.
No vocals. Clean production quality. Hollywood film score aesthetic."""


# ─── VLM Verification Prompts ───────────────────────────────────────


VLM_VERIFY_IMAGE_PROMPT = """Analyze this generated image against the original specifications.

## Original Prompt:
{original_prompt}

## Character Reference Context:
{character_ref_description}

## Check these criteria (be STRICT):
1. **Photorealism**: Is this genuinely photorealistic? No anime, no illustration, no CGI artifacts?
2. **Prompt Faithfulness**: Does the image match what was requested? (characters, action, setting)
3. **Character Consistency**: Do characters match their described traits? (hair, outfit, face, features)
4. **Technical Quality**: Free of extra limbs, malformed faces, text overlays, watermarks, blurriness?
5. **Composition**: Is the framing/camera direction approximately correct?

## Output JSON (only this, nothing else):
{{"passed": true/false, "issues": ["list of specific problems found"], "suggestion": "one-sentence prompt improvement if regenerating"}}

Be STRICT. Wrong character appearance, non-photorealistic rendering, or AI artifacts are FAILURES."""


VLM_VERIFY_VIDEO_MULTIDIM_PROMPT = """Analyze these frames extracted from a generated cinematic video clip.
Score each dimension independently on a 0-10 scale.

## Expected Scene:
{scene_description}

## Expected Characters:
{characters_description}

## Style: {style}
## Camera Direction: {camera_direction}

## Learned lessons from previous generations (apply these):
{learned_lessons}

## Score each dimension (0-10):
1. **identity**: Do characters match their descriptions? Same face, hair, outfit, features across all frames? (0=completely wrong, 10=perfect match)
2. **motion**: Is human movement natural and fluid? (0=robotic/CGI/slideshow, 10=natural cinema quality)
3. **style**: Is this genuinely photorealistic? (0=anime/cartoon/CGI/illustration, 10=real film quality)
4. **artifacts**: Are there text overlays, watermarks, floating letters, glitches? (0=severe artifacts, 10=completely clean)
5. **composition**: Does the video match the described camera movement and scene composition? (0=completely wrong, 10=exactly as directed)

## Output JSON (only this, nothing else):
{{"identity": 0-10, "motion": 0-10, "style": 0-10, "artifacts": 0-10, "composition": 0-10, "issues": ["list of specific problems"], "suggestion": "one-sentence improvement for regeneration prompt"}}

CRITICAL RULES:
- ANY visible text/watermark/subtitle = artifacts score 0
- Wrong character identity (different hair/face/outfit) = identity score 0
- Any anime/cartoon style present = style score 0
- Score 7+ = passed threshold. Reserve 9-10 for exceptional quality."""


GEMINI_VIDEO_QC_PROMPT = """Analyze this video clip for a photorealistic cinematic production.
You have access to the full video including audio. Evaluate both visual and audio quality.

Expected scene: {scene_description}
Expected characters: {characters_description}
Style: {style}

Score each dimension 0-10:
- visual_score: Overall photorealistic quality and scene fidelity (0=animation/CGI, 10=real cinema)
- audio_score: Audio quality and appropriateness (ambient sound, dialogue clarity) — score 5 if video has no audio track
- av_sync_score: Audio-visual synchronization quality — score 5 if no audio
- identity_score: Character identity consistency with descriptions across all frames
- motion_score: Naturalness of human movement (0=robotic/CGI motion, 10=natural human motion)
- artifacts_score: Freedom from text overlays, watermarks, AI artifacts (0=severe, 10=completely clean)

Output JSON only:
{{"visual_score": 0-10, "audio_score": 0-10, "av_sync_score": 0-10, "identity_score": 0-10, "motion_score": 0-10, "artifacts_score": 0-10, "issues": ["specific problems found"], "passed": true/false, "suggestion": "one sentence improvement"}}

passed=true if: average of all scores >= 6.5 AND artifacts_score >= 7 AND identity_score >= 6
IMPORTANT: Score 7 = good enough. Reserve 9-10 for exceptional quality. Be strict but fair."""


DIRECTOR_QC_PROMPT = """You are an executive producer reviewing a short film production.
You are seeing representative keyframes from each scene in order.

Characters in this production:
{characters_description}

Visual Style: {style}

Review for these CROSS-SCENE issues:
1. **Style drift**: Does the visual style remain consistent throughout all scenes?
2. **Character identity**: Do the same characters look like the same people across every scene?
3. **Lighting continuity**: Is the lighting/color grade consistent within the same location?
4. **Narrative flow**: Do the scenes tell a coherent story following the synopsis?
5. **Quality outliers**: Are any scenes clearly worse in quality than the others?

Output JSON only:
{{"overall_score": 0-10, "scenes_to_regen": [list of 0-based scene indices with major issues, max 3], "issues": ["list of specific cross-scene problems"], "approved": true/false}}

RULES:
- Only add to scenes_to_regen if overall_score < 7 AND that specific scene is clearly worse
- approved=true if overall_score >= 7
- Maximum 3 scenes in scenes_to_regen — be selective
- If all scenes are acceptable, return scenes_to_regen: [] and approved: true"""


VLM_COMPARE_CHARACTER_SHEETS_PROMPT = """You are comparing two generated photorealistic character reference portraits for the SAME person.
Pick the one that best matches the character specifications below.

## Character Specifications:
- Name: {name}
- Visual Traits: {visual_traits}
- Cinematic Style: {style}

## Evaluation criteria (in order of importance):
1. **Photorealism**: Which is more genuinely photorealistic? (no AI artifacts, no illustration)
2. **Accuracy**: Which more faithfully represents the described visual traits? (hair, outfit, face features)
3. **Quality**: Which has better lighting, sharper detail, more natural appearance?
4. **Reference Utility**: Which would better maintain character identity across video frames?

## Output JSON (only this, nothing else):
{{"winner": 1 or 2, "reason": "one-sentence explanation of why this one is better"}}"""


CROSS_SCENE_IDENTITY_CHECK_PROMPT = """You are reviewing frames from DIFFERENT SCENES of the same cinematic production.
Each image is one representative frame from a different scene, shown in order.

## Characters that should appear consistently:
{characters_description}

## Style: {style}

## Task:
Check whether the characters maintain their visual IDENTITY across all scenes.
Look for:
- Hair color/style changing between scenes
- Outfit changing when it shouldn't
- Face/body proportions shifting dramatically
- Skin tone inconsistencies

## Output JSON (only this, nothing else):
{{"consistent": true/false, "worst_scene_index": number or null, "drift_description": "what changed and where", "severity": "none|minor|major"}}

RULES:
- "minor" = slight variation but character is recognizable
- "major" = character looks like a different person in some scenes
- worst_scene_index is 0-based, null if consistent"""


DIRECTOR_AGENT_PROMPT = """You are an experienced film director reviewing a completed short film.
You are seeing representative frames from each scene in production order.

## Production details:
Characters: {characters_description}
Visual Style: {style}
Story Synopsis: {synopsis}

## Scene timeline:
{scene_timeline}

## Your task:
Review the assembled film for NARRATIVE COHERENCE and VISUAL QUALITY.

Check for:
1. **Narrative flow**: Do scenes tell a coherent story with cause-and-effect? Or is it a disconnected highlight reel?
2. **Character continuity**: Do characters look like the same people across scenes?
3. **Visual consistency**: Is the style/color/lighting consistent?
4. **Pacing**: Are scene transitions smooth or jarring?
5. **Story arc**: Is there a clear beginning, conflict, and resolution?

For each problematic scene, provide a SPECIFIC director's note explaining exactly what's wrong and what would fix it.

Output JSON only:
{{"overall_score": 0-10, "approved": true/false, "scenes_to_regen": [list of 0-based scene indices, max 2], "timeline_notes": {{"0": "specific note about scene 0", "2": "specific note about scene 2"}}, "narrative_assessment": "2-3 sentence assessment of the story coherence", "issues": ["list of specific problems"]}}

RULES:
- approved=true if overall_score >= 7
- scenes_to_regen max 2 entries — only scenes that significantly harm the narrative
- timeline_notes keys are scene index strings ("0", "1", etc.)
- If all scenes are acceptable, return scenes_to_regen: [] and approved: true
- Your timeline_notes will be passed directly to the video generation model as director instructions"""


ADAPTIVE_SIMPLIFY_SCENE_PROMPT = """A video AI model repeatedly failed to generate this cinematic scene properly.
Simplify the scene description to make it more achievable while keeping cinematic quality.

## Original Scene:
- Description: {scene_description}
- Camera Direction: {camera_direction}
- Characters: {characters}
- Duration: {duration_sec}s

## Problems encountered:
{issues_summary}

## Rules for simplification:
1. Keep the SAME characters and location
2. Simplify camera movement (prefer static or slow push over complex tracking)
3. Reduce simultaneous actions
4. Keep the narrative intent but make the visual execution simpler
5. Avoid elements known to cause AI artifacts (signs, books, screens with text)

## Output JSON (only this, nothing else):
{{"simplified_description": "string — simplified cinematic description", "simplified_camera": "string — simpler camera direction", "negative_constraints": "string — additional things to explicitly AVOID based on the failures"}}"""


# ─── Character DNA Extraction ────────────────────────────────────────
CHARACTER_DNA_EXTRACT_PROMPT = """You are a casting director. Analyze this character reference image and write an EXTREMELY SPECIFIC visual descriptor.

Character name: {name}
Original description: {visual_traits}

Extract an ultra-precise "Character DNA" string that will be copy-pasted verbatim into every video prompt for this character. It must be SELF-CONTAINED and FREEZE the character's appearance.

Include ALL of the following with maximum precision:
- Exact face shape (heart/oval/square/round), cheekbone prominence, jaw angle
- Eyes: exact color with any flecks/rings, spacing, lid shape, brow arch, lash quality
- Nose: bridge width, tip shape
- Mouth: lip fullness ratio, width, any micro-features (gap in teeth, dimple)
- Skin: specific tone (NOT just "light" — use "warm ivory with pink undertones"), texture, any marks
- Hair: exact color (paint-chip specificity), texture, length to exact landmark, part position
- Build: height cues, shoulder-hip ratio, posture tendency
- ONE highly distinctive micro-feature that makes this person uniquely recognizable
- Dominant wardrobe signature (the ONE item that most identifies them)

Write as a dense single paragraph starting with the character name. Maximum 120 words.
This MUST work as standalone identity anchor when image conditioning is unavailable.

Output ONLY the character DNA paragraph, no JSON, no explanations."""


# ─── Video Prompt Variants ────────────────────────────────────────────
VIDEO_PROMPT_VARIANT_CRITIC_PROMPT = """You are a video generation expert. You have 3 different prompt variants for the same scene.
Choose the best one for a text-to-video model (Seedance 2.0 / ByteDance).

## Scene intent:
{scene_description}
## Character identity to preserve:
{character_dna}
## Motion complexity assessment: {motion_complexity}/5

## Variant A (continuity-heavy):
{variant_a}

## Variant B (action-first):
{variant_b}

## Variant C (cinematic-cue):
{variant_c}

## Your task:
Select the variant most likely to produce a high-quality, consistent video clip.
Consider:
- Does it put character identity early in the prompt?
- Is the action clear and singular?
- Is motion complexity appropriate for the model?
- Does it avoid abstract/poetic language?

Output JSON only:
{{"winner": "A" or "B" or "C", "reason": "one sentence why this variant is best", "suggested_tweak": "optional one-sentence refinement to apply to the winner"}}"""


# ─── Diagnosis-based retry ────────────────────────────────────────────
VIDEO_RETRY_DIAGNOSIS_PROMPT = """You are a video director. A generated clip failed quality checks. Rewrite the prompt to fix the specific problems.

## Original prompt:
{original_prompt}

## Failure diagnosis (scores 0-10, pass threshold 6.5):
{critique_json}

## Failing dimensions:
{failing_dimensions}

## Surgical rewrite rules:
- If identity_score < 6: Move character DNA to the very first line; add shot type that shows distinguishing features; emphasize wardrobe signature; use "medium shot" not close-up
- If motion_score < 6: Reduce to ONE primary action; use stable camera; add "natural weight transfer", "realistic motion physics"
- If style_score < 6: Front-load photorealism cues; add specific camera body reference; remove any abstract/surreal language
- If artifacts_score < 6: Add explicit negatives "no text overlay, no morphing, no warping, no extra limbs, anatomically correct"
- If composition_score < 6: Simplify camera movement; specify exact framing (medium close-up, over-the-shoulder, etc.)

NEVER: simplify by removing character DNA or style lock
NEVER: make it shorter by removing important specifics
ALWAYS: preserve what scored well (7+)

Output ONLY the rewritten prompt, no explanations, no JSON."""


# ─── Contact sheet Director QC ───────────────────────────────────────
DIRECTOR_VIDEO_FRAMES_PROMPT = """You are an executive producer reviewing actual generated video frames from a short film production.
You are seeing 5 frames extracted at 0%%, 25%%, 50%%, 75%%, 100%% timestamps from each scene clip.

## Characters in this production:
{characters_description}

## Visual Style: {style}
## Story synopsis: {synopsis}

## Review each scene's actual frames for:
1. CHARACTER IDENTITY: Does the character look like the same person across ALL frames within each clip? And across scenes?
2. TEMPORAL CONSISTENCY: Within a single clip, does the character's face/hair/clothing stay stable frame-to-frame?
3. STYLE CONSISTENCY: Does the photorealistic quality and color grading feel like the same film?
4. MOTION QUALITY: Does movement look natural or robotic/morphing?
5. NARRATIVE COHERENCE: Can you follow the story from these frames in sequence?

## Output JSON:
{{"overall_score": 0-10, "approved": true/false, "scenes_to_regen": [list of 0-based scene indices with major identity or quality issues, max 2], "cross_scene_issues": ["specific cross-scene problems found"], "per_scene_notes": {{"0": "note", "1": "note"}}, "narrative_assessment": "2-3 sentences on story coherence and visual flow"}}

RULES:
- approved=true if overall_score >= 7
- scenes_to_regen only if that scene has MAJOR problems (different character face, severe artifacts, completely wrong style)
- Maximum 2 entries in scenes_to_regen
- If all scenes are acceptable, scenes_to_regen=[] and approved=true"""


# ─── Intelligent transitions ─────────────────────────────────────────
TRANSITION_PLANNER_PROMPT = """You are a film editor. Plan the transitions between scenes for optimal cinematic flow.

## Scene sequence:
{scene_sequence}

## For each cut point (between scenes N and N+1), choose:
- "cut" — immediate hard cut (same location/time, continuous action, high energy)
- "crossfade" — 0.3-0.8s dissolve (mood shift, time skip, reflective moment)
- "fade_black" — dip to black 0.3-0.5s (major location change, time jump, act break)

## Rules:
- Prefer hard cuts for action/tension
- Use crossfade for emotional transitions and location changes
- Use fade_black only for major act breaks (max once per video)
- Never use the same transition 3 times in a row
- Cut points should respect narrative rhythm

Output JSON only — array of transition objects:
[{{"from_scene": 0, "to_scene": 1, "type": "cut"|"crossfade"|"fade_black", "duration_sec": 0.0-0.8, "reason": "brief reason"}}]

Output ONLY the JSON array, no other text."""


# ─── Color normalization ─────────────────────────────────────────────
COLOR_GRADE_PLANNER_PROMPT = """You are a colorist reviewing frames from multiple scenes of the same film.
The scenes were generated separately and may have slight color inconsistencies.

## Target style: {style}
## Target mood: {mood}

Generate a single FFmpeg video filter string that applies a subtle, cohesive color grade
appropriate for this style. The filter should UNIFY the scenes without looking processed.

Requirements:
- Use only: eq, colorbalance, curves, colorchannelmixer, vibrance, unsharp
- Subtle values only (contrast 0.9-1.15, brightness -0.05 to +0.05, saturation 0.8-1.2)
- Match the mood: {mood} suggests specific color temperature preferences
- For cinematic styles: slight contrast boost, subtle saturation control
- For thriller/noir: reduce saturation slightly, add contrast
- For romantic: warm tones, slight softness (unsharp with low values)
- Keep it under 200 characters total

Output ONLY the FFmpeg filter string (e.g., "eq=contrast=1.05:brightness=0.01:saturation=0.95,unsharp=3:3:0.5"), nothing else."""
