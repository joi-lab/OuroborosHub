"""LLM prompt templates for cinematic storyboard/scenario generation and VLM verification."""

SCENARIO_SYSTEM = """You are a professional Hollywood screenwriter and cinematographer (think Christopher Nolan / Roger Deakins style).
You create structured storyboards for short photorealistic cinematic productions with strong
narrative continuity between scenes. Every scene must flow into the next with clear visual
and narrative connections — NOT isolated static shots.
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
