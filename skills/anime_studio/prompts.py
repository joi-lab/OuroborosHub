"""LLM prompt templates for storyboard/scenario generation and VLM verification."""

SCENARIO_SYSTEM = """You are a professional anime storyboard writer and director.
You create structured storyboards for short 2D anime cartoons with strong
narrative continuity between scenes. Every scene must flow into the next with
clear visual and narrative connections — NOT isolated comic panels.
Your output is ALWAYS valid JSON matching the schema below.
Focus on visual storytelling, dramatic framing, character consistency, and
SMOOTH TRANSITIONS between scenes."""

SCENARIO_USER_TEMPLATE = """Create a detailed storyboard for a short anime cartoon.

## Requirements:
- **Theme/Plot**: {theme}
- **Anime Style**: {style}
- **Total Duration**: {duration_sec} seconds
- **Number of Scenes**: {num_scenes}
- **Overall Mood**: {mood}
- **Include Dialogue**: {include_dialogue}
- **Music Style**: {music_style}

## Output JSON Schema:
{{
  "title": "string — catchy title",
  "synopsis": "string — 2-3 sentence synopsis with clear narrative arc (beginning, conflict, resolution)",
  "style": "string — specific anime visual style description (colors, line work, shading)",
  "total_duration_sec": number,
  "characters": [
    {{
      "name": "string",
      "description": "string — personality and role",
      "visual_traits": "string — DETAILED visual description: hair color/style, eye color, outfit, accessories, body type, age appearance, distinctive features. Be EXTREMELY specific for image generation consistency."
    }}
  ],
  "locations": [
    {{
      "name": "string",
      "description": "string — narrative role",
      "visual_traits": "string — DETAILED visual description: time of day, lighting, colors, architectural style, atmosphere, specific objects. Be EXTREMELY specific."
    }}
  ],
  "scenes": [
    {{
      "index": number (0-based),
      "description": "string — what happens in this scene (action, emotion, movement). Describe the MOTION and ACTION, not a static pose.",
      "duration_sec": number (4-15),
      "characters": ["character_name", ...],
      "location": "location_name",
      "camera_direction": "string — specific camera movement/framing (e.g. 'medium shot, slow zoom in on face', 'wide establishing shot, pan left to right')",
      "dialogue": "string or null — spoken text for this scene",
      "mood": "string — emotional tone",
      "transition_from": "string or null — how this scene connects visually/narratively from the previous scene (null for first scene only). Example: 'cut from the sword gleaming to character's determined eyes' or 'camera pulls back to reveal the city below'"
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
5. Music cues should cover the full duration (can overlap scenes)
6. Camera directions should be specific and achievable by video AI
7. If dialogue is included, keep it short (1-2 sentences per scene max)
8. Visual traits must be EXTREMELY detailed — these drive image generation
9. Create 1-3 characters max for consistency
10. Create 2-4 locations max
11. Each scene (except the first) MUST have a "transition_from" describing the visual/narrative bridge from the previous scene. This is critical for animation continuity.
12. Scenes should describe MOVEMENT and ACTION — not static poses. Think "character walks forward and draws sword" not "character holding sword"

Respond with ONLY the JSON object, no markdown fences, no explanation."""


IMAGE_CHARACTER_SHEET_PROMPT = """Generate a 2D anime character reference sheet for:

Character: {name}
Visual Description: {visual_traits}
Anime Style: {style}

Show the character in 4 views: front view, 3/4 view, side profile, and an expressive pose.
Clean white background. Consistent proportions across all views.
Label each view. Professional anime production character sheet style.
Sharp linework, vibrant colors, {style} aesthetic."""


IMAGE_LOCATION_PROMPT = """Generate a 2D anime background/location concept art:

Location: {name}
Visual Description: {visual_traits}
Anime Style: {style}

Wide establishing shot. Rich detail. Professional anime background painting style.
{style} color palette and atmosphere. Suitable as a scene backdrop.
No characters in the image."""


IMAGE_KEYFRAME_PROMPT = """Generate a single anime keyframe/storyboard panel:

Scene: {scene_description}
Characters present: {characters}
Location: {location_description}
Camera: {camera_direction}
Mood: {mood}
Anime Style: {style}

This is a key moment from the scene showing the ACTION in progress.
Show dynamic motion and emotion, NOT a static pose.
Use the same {style} visual language consistently.
Professional anime production keyframe quality.
The composition should clearly show what is HAPPENING — movement, interaction, expression."""


IMAGE_KEYFRAME_SEQUENTIAL_PROMPT = """Generate a single anime keyframe/storyboard panel:

Scene: {scene_description}
Characters present: {characters}
Location: {location_description}
Camera: {camera_direction}
Mood: {mood}
Anime Style: {style}

CONTINUITY FROM PREVIOUS KEYFRAME:
{prev_keyframe_context}

This keyframe MUST visually connect to the previous one. The characters should look IDENTICAL
(same hair, outfit, accessories, proportions) — only their pose and expression change.
Show dynamic motion and emotion appropriate to this scene's action.
Use the same {style} visual language consistently.
Professional anime production keyframe quality.
The composition should clearly show what is HAPPENING — movement, interaction, expression.
Characters MUST match these exact visual descriptions:
{characters_identity_block}"""


VIDEO_PROMPT_TEMPLATE = """Animate this 2D anime scene:

{scene_description}

Characters in this scene:
{characters_identity_block}

Camera movement: {camera_direction}
Mood: {mood}
Style: 2D anime, {style}
Duration: {duration_sec} seconds

{continuity_note}

REQUIREMENTS:
- Smooth 2D anime animation with fluid character motion
- Characters must maintain their exact visual identity throughout
- Natural movement and expressions, NOT static poses
- Cinematic quality framing and lighting

NEGATIVE CONSTRAINTS (NEVER include these):
- NO text overlays, NO subtitles, NO captions, NO watermarks
- NO random letters, NO floating text, NO title cards, NO UI elements
- NO gibberish text, NO signs with readable text, NO speech bubbles
- NO logo, NO brand marks, NO timecodes"""


MUSIC_PROMPT_TEMPLATE = """Compose a short instrumental piece for an anime scene:

Mood: {mood}
Tempo: {tempo}
Style: {style}
Duration: approximately {duration_sec} seconds

Description: {description}

The music should be cinematic, emotional, and suitable for a 2D anime cartoon.
No vocals. Clean production quality."""


# ─── VLM Verification Prompts ───────────────────────────────────────


VLM_VERIFY_IMAGE_PROMPT = """Analyze this generated anime image against the original specifications.

## Original Prompt:
{original_prompt}

## Character Reference Context:
{character_ref_description}

## Check these criteria (be STRICT):
1. **Prompt Faithfulness**: Does the image match what was requested? (characters, action, setting)
2. **Style Consistency**: Is it in the correct anime style? No photorealism, no 3D renders, no AI artifacts?
3. **Character Consistency**: Do characters match their described traits? (hair color, outfit, features)
4. **Technical Quality**: Free of extra limbs, malformed faces, text overlays, watermarks, blurriness?
5. **Composition**: Is the framing/camera direction approximately correct?

## Output JSON (only this, nothing else):
{{"passed": true/false, "issues": ["list of specific problems found"], "suggestion": "one-sentence prompt improvement if regenerating"}}

Be STRICT about everything. Minor variations that indicate a different character (different hair,
wrong outfit, missing accessories) are FAILURES. Only tolerate very minor artistic interpretation
differences that don't affect identity."""


VLM_VERIFY_VIDEO_PROMPT = """Analyze these frames extracted from a generated anime video clip.

## Expected Scene:
{scene_description}

## Expected Characters:
{characters_description}

## Style: {style}
## Camera Direction: {camera_direction}

## Check STRICTLY:
1. **Character Identity**: Are the characters recognizable and matching their descriptions? Same hair, outfit, features across all frames?
2. **Text Artifacts**: Are there ANY random text overlays, subtitles, watermarks, floating letters, or gibberish text visible? This is a CRITICAL failure.
3. **Animation Quality**: Is the motion fluid? No frozen frames, sudden teleportation, or extreme morphing?
4. **Style Consistency**: Is the visual style consistently 2D anime throughout? No photorealistic segments?
5. **Scene Fidelity**: Does the video depict the described action and camera movement?

## Output JSON (only this, nothing else):
{{"passed": true/false, "score": 0-10, "issues": ["list of specific problems"], "suggestion": "one-sentence improvement for regeneration prompt"}}

IMPORTANT: Score 7+ = passed. Text artifacts or wrong character identity = automatic score 0."""


SCENE_TRANSITION_TEMPLATE = """CONTINUITY CONTEXT:
The previous scene showed: {prev_scene_description}
Visual transition: {transition_type}

This scene should start from a state visually consistent with how the previous scene ended.
The last frame of the previous scene is provided as @Image(last) for visual continuity.
Characters MUST maintain their exact appearance from the previous scene."""


# ─── Best-of-2 Character Sheet Comparison ───────────────────────────


VLM_COMPARE_CHARACTER_SHEETS_PROMPT = """You are comparing two generated anime character sheets for the SAME character.
Pick the one that best matches the character specifications below.

## Character Specifications:
- Name: {name}
- Visual Traits: {visual_traits}
- Anime Style: {style}

## Evaluation criteria (in order of importance):
1. **Accuracy**: Which sheet more faithfully represents the described visual traits? (hair color/style, outfit, accessories, eye color)
2. **Consistency**: Which sheet has more consistent proportions across its views?
3. **Quality**: Which has cleaner linework, better coloring, fewer AI artifacts?
4. **Utility**: Which would serve better as a reference for maintaining character identity across animation frames?

## Output JSON (only this, nothing else):
{{"winner": 1 or 2, "reason": "one-sentence explanation of why this one is better"}}"""


# ─── Multi-Dimensional Video Scoring ────────────────────────────────


VLM_VERIFY_VIDEO_MULTIDIM_PROMPT = """Analyze these frames extracted from a generated anime video clip.
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
1. **identity**: Do characters match their descriptions? Same hair, outfit, features across all frames? (0=completely wrong character, 10=perfect match)
2. **motion**: Is animation fluid? No frozen frames, teleportation, morphing? (0=slideshow/broken, 10=cinema-quality motion)
3. **style**: Is visual style consistently 2D anime throughout? (0=photorealistic/3D/mixed, 10=perfect style consistency)
4. **artifacts**: Are there text overlays, watermarks, floating letters, gibberish, glitches? (0=severe text/artifacts everywhere, 10=completely clean)
5. **composition**: Does the video match the described camera movement and scene composition? (0=completely wrong framing, 10=exactly as directed)

## Output JSON (only this, nothing else):
{{"identity": 0-10, "motion": 0-10, "style": 0-10, "artifacts": 0-10, "composition": 0-10, "issues": ["list of specific problems found"], "suggestion": "one-sentence improvement for regeneration prompt"}}

CRITICAL RULES:
- ANY visible text/watermark/subtitle = artifacts score 0
- Wrong character (different hair/outfit) = identity score 0
- Be STRICT. A 7 is already "good enough". Reserve 9-10 for exceptional quality."""


# ─── Cross-Scene Identity Verification ──────────────────────────────


CROSS_SCENE_IDENTITY_CHECK_PROMPT = """You are reviewing frames from DIFFERENT SCENES of the same anime video.
Each image is one representative frame from a different scene, shown in order.

## Characters that should appear consistently:
{characters_description}

## Anime Style: {style}

## Task:
Check whether the characters maintain their visual IDENTITY across all scenes.
Look for:
- Hair color/style changing between scenes
- Outfit changing when it shouldn't
- Face/body proportions shifting dramatically
- Accessories appearing/disappearing

## Output JSON (only this, nothing else):
{{"consistent": true/false, "worst_scene_index": number or null, "drift_description": "what changed and where", "severity": "none|minor|major"}}

RULES:
- "minor" = slight proportion drift but character is recognizable
- "major" = character looks like a different person in some scenes
- worst_scene_index is 0-based, null if consistent
- Only check characters, not backgrounds (locations naturally change between scenes)"""


# ─── Adaptive Scene Simplification ──────────────────────────────────


ADAPTIVE_SIMPLIFY_SCENE_PROMPT = """A video AI model repeatedly failed to generate this anime scene properly.
Simplify the scene description and camera direction to make it more achievable.

## Original Scene:
- Description: {scene_description}
- Camera Direction: {camera_direction}
- Characters: {characters}
- Duration: {duration_sec}s

## Problems encountered:
{issues_summary}

## Rules for simplification:
1. Keep the SAME characters and location
2. Simplify camera movement (prefer static/slow over complex tracking)
3. Reduce the complexity of simultaneous actions
4. Keep the narrative intent but make the visual execution simpler
5. Avoid overlapping text-like patterns (signs, books, screens)

## Output JSON (only this, nothing else):
{{"simplified_description": "string — simplified scene description focused on achievable motion", "simplified_camera": "string — simpler camera direction", "negative_constraints": "string — additional things to explicitly AVOID based on the failures"}}"""
