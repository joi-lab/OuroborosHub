---
name: anime_studio
description: AI-powered 2D anime generator with VLM-verified assets, video analysis via Gemini, sequential keyframes, scene continuity chain, and multi-model image generation
version: 2.2.0
type: extension
entry: plugin.py
permissions: [net, route, widget, ws_handler, tool, read_settings, subprocess]
env_from_settings: [OPENROUTER_API_KEY]
when_to_use: User wants to generate a short animated 2D anime cartoon, music video, or animated scene with consistent characters, VLM verification, and narrative continuity.
timeout_sec: 300
dependencies: [Pillow]
ui_tab:
  tab_id: studio
  title: Anime Studio
  icon: film
  render:
    kind: declarative
    schema_version: 1
    components:
      - type: form
        title: "🎬 Generate Anime"
        route: generate
        method: POST
        mode: job
        status_route: status
        fields:
          - name: theme
            label: Theme / Story
            type: textarea
            placeholder: "A young samurai discovers a magical sword in an ancient temple..."
            required: true
          - name: style
            label: Anime Style
            type: select
            options: ["modern anime", "retro 90s anime", "chibi cute anime", "dark gothic anime", "watercolor anime", "Studio Ghibli style", "cyberpunk anime", "shounen action anime"]
            default: modern anime
          - name: mood
            label: Mood
            type: select
            options: ["adventurous", "comedic", "dramatic", "melancholic", "mysterious", "romantic", "action-packed", "wholesome"]
            default: adventurous
          - name: duration_sec
            label: Duration (seconds)
            type: number
            default: 30
          - name: num_scenes
            label: Number of Scenes
            type: number
            default: 4
          - name: image_model
            label: Image Generator
            type: select
            options: ["gpt-image-2", "nanobanana"]
            default: gpt-image-2
          - name: video_model
            label: Video Model
            type: select
            options: ["bytedance/seedance-2.0", "bytedance/seedance-2.0-fast", "google/veo-3.1"]
            default: "bytedance/seedance-2.0"
          - name: music_style
            label: Music Style
            type: select
            options: ["orchestral cinematic", "electronic ambient", "acoustic guitar folk", "j-pop instrumental", "lo-fi hip hop beats", "epic battle drums"]
            default: orchestral cinematic
        submit_label: "🎬 Generate Anime"
      - type: subscription
        event: studio_progress
        render:
          - type: progress
            value_key: progress_pct
            label_key: message
          - type: gallery
            title: Character Sheets
            items_key: character_sheets
            item_type: image
            route_prefix: "asset?path="
          - type: gallery
            title: Keyframes
            items_key: keyframes
            item_type: image
            route_prefix: "asset?path="
          - type: key_value
            title: Verification
            items_key: verification_display
            condition_key: has_verification
          - type: key_value
            title: Warnings
            items_key: warnings_display
            condition_key: has_warnings
---

# Anime Studio v2.1

A professional-grade 2D anime cartoon generator with **VLM-verified assets**,
**video analysis via Gemini 3.1 Pro**, **sequential keyframes for continuity**,
and **multi-model image generation**.

## What's New in v2.1

- **Video VLM Verification (Gemini 3.1 Pro)** — After each video scene is generated,
  5 evenly-spaced frames are extracted and sent to Gemini 3.1 Pro for multi-frame
  analysis. Checks character identity, text artifacts, animation quality, style
  consistency, and scene fidelity. Failed scenes are retried with improved prompts.

- **Sequential Keyframes** — Keyframes are now generated one after another (not in
  parallel). Each keyframe includes context about the previous scene, creating
  visual continuity across the storyboard instead of isolated "comic panels".

- **Stricter VLM Verification** — Image verification no longer accepts "minor
  variations". Characters must match their exact descriptions — wrong hair, missing
  accessories, or outfit changes are now FAILURES that trigger regeneration.

- **Full Character Identity in Video Prompts** — ALL character sheets (not just the
  first match) are passed as references to the video generator, plus explicit text
  descriptions of each character's visual traits.

- **Anti-Text Negative Prompting** — Every video generation prompt now includes
  explicit negative constraints against text overlays, watermarks, subtitles,
  and floating text — the primary source of artifacts in Seedance/video models.

- **Fixed Continuity Chain** — The bug where `prev_frame_url` was stored in a
  local variable but never written to the scene object is now fixed. Each
  scene's last frame is properly passed as a reference to the next scene.

## Pipeline

1. **Scenario Generation** — LLM creates a structured storyboard with scene
   transitions, dynamic action descriptions, and narrative continuity cues
2. **Asset Generation** — Character sheets (parallel), location art (parallel),
   then keyframes (SEQUENTIAL with inter-frame context)
3. **Image VLM Verification** — Each critical asset (characters, keyframes) is
   checked by Claude Sonnet for correctness and regenerated if needed (up to 2 retries)
4. **Music Generation** — AI-composed soundtrack via Lyria 3 Pro
5. **Video Animation** — Sequential scene generation with:
   - Full continuity chain (last frame → next scene reference)
   - ALL character sheets as references (not just first)
   - Explicit character identity descriptions in prompts
   - Negative prompting against text artifacts
   - **Video VLM verification via Gemini 3.1 Pro** (max 1 retry per scene)
6. **Assembly** — ffmpeg concatenation with audio mixing

## Models Used (via OpenRouter)

| Model | Purpose |
|-------|---------|
| `openai/gpt-5.4-image-2` | Character sheets, keyframes (default) |
| `google/gemini-3.1-flash-image-preview` | Character sheets, keyframes (nanobanana option) |
| `anthropic/claude-sonnet-4.6` | Scenario generation + image VLM verification |
| `google/gemini-3.1-pro-preview` | **Video VLM verification** (multi-frame analysis) |
| `bytedance/seedance-2.0` | Video generation with reference images |
| `google/lyria-3-pro-preview` | Original soundtrack clips |

## Requirements

- `OPENROUTER_API_KEY` with access to the models above
- `ffmpeg` installed on the host system (for video assembly + frame extraction)
