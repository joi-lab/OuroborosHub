---
name: anime_studio
description: AI-powered 2D anime generator with VLM-verified assets, video analysis via Gemini, sequential keyframes, scene continuity chain, multi-model image/video generation, LLM-powered error recovery, and parallel asset+music pipeline
version: 2.12.0
type: extension
entry: plugin.py
permissions: [net, route, widget, ws_handler, tool, read_settings, subprocess, companion_process]
env_from_settings: [OPENROUTER_API_KEY]
when_to_use: User wants to generate a short animated 2D anime cartoon, music video, or animated scene with consistent characters, VLM verification, and narrative continuity.
timeout_sec: 300
dependencies: [Pillow]
companion_processes:
  - name: anime_worker
    command: [python3, scripts/anime_worker.py]
    runtime: python3
    restart_policy: on_failure
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
            options: ["gpt-image-2", "gpt-5-image", "gpt-5-image-mini", "nanobanana", "gemini-3-pro-image", "flux.2-pro", "flux.2-max", "seedream-4.5", "grok-imagine"]
            default: gpt-image-2
          - name: video_model
            label: Video Model
            type: select
            options: ["bytedance/seedance-2.0", "bytedance/seedance-2.0-fast", "bytedance/seedance-1-5-pro", "google/veo-3.1", "google/veo-3.1-fast", "google/veo-3.1-lite", "minimax/hailuo-2.3", "kwaivgi/kling-v3.0-pro", "kwaivgi/kling-v3.0-std", "kwaivgi/kling-video-o1"]
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

# Anime Studio v2.12

A professional-grade 2D anime cartoon generator with **VLM-verified assets**,
**video analysis via Gemini 3.1 Pro**, **sequential keyframes for continuity**,
**multi-model image/video generation**, **LLM-powered error recovery**,
**smart prompt condensation**, and **parallel asset + music pipeline**.

## What's New in v2.12

- **Robust VLM/LLM JSON handling** — Image and video VLM calls now use compressed
  image payloads, JSON mode, tolerant JSON extraction, bounded multi-image inputs,
  and retries for transient provider failures. Storyboard, scene simplification,
  and failure-advisor JSON parsing share the same hardened parser.

- **Provider-resilient generation** — If the default `gpt-image-2` image path
  returns empty choices or transient failures, Anime Studio falls back to
  `nanobanana` so character sheets, locations, and keyframes can still complete.
  Video generation no longer forces model-generated audio, avoiding Seedance audio
  moderation failures when music/dialogue are handled elsewhere in the pipeline.

## What's New in v2.11

- **Companion-process pipeline (out-of-process safe)** — The generation pipeline now
  runs in a host-supervised `anime_worker` companion process instead of a thread
  spawned inside the route handler. Routes only enqueue a job (file-backed job store)
  and poll status; the companion runs each job and relays progress to the browser
  through the Host Service WS bridge. This fixes the skill on Ouroboros >= v6.15.0,
  where isolated-dependency extensions dispatch routes in short-lived per-call
  children that cannot host a long-running pipeline thread. Requires the
  `companion_process` permission.

## What's New in v2.7

- **Smart Prompt Condensation** — Models with strict prompt character limits (e.g.
  Kling's 2500-char ceiling) now get their prompts automatically shortened by a fast
  LLM (Gemini 3.5 Flash) before submission. The LLM preserves all visual details,
  character identity, and camera directions while trimming boilerplate. Falls back to
  hard truncation if the LLM result is still too long.

- **Kling models restored** — Kling v3.0 Pro/Std and Kling Video O1 are back in the
  selection list. With automatic prompt condensation, their 2500-char limit is no
  longer a blocker. Kling has a less aggressive copyright filter than Seedance.

- **10 Video Models** — Seedance 2.0/1.5, Veo 3.1/Fast/Lite, Hailuo 2.3, and
  Kling v3.0 Pro/Std/O1. Prompt-length error detection remains as a safety net.

## What's New in v2.4

- **Smart Error Recovery (LLM Advisor)** — When a video scene fails (copyright filter,
  timeout, content policy), a fast LLM (Gemini 3.5 Flash) analyzes the error and
  recommends an action: retry the same model, switch to an alternative, or skip.
  The advisor picks the best alternative model based on the error type and scene needs.

- **Parallel Music + Assets** — Music generation now runs in parallel with asset
  generation (character sheets, locations, keyframes) via `asyncio.gather`, cutting
  total pipeline time by the duration of music generation (~30-60s).

### Retained from v2.3

- Video VLM verification via Gemini 3.1 Pro (multi-frame analysis)
- Sequential keyframes for visual continuity
- Best-of-2 character sheet selection
- Multi-dimensional video scoring (5 axes, weighted average)
- Cross-scene identity check with worst-scene regeneration
- Adaptive scene simplification on repeated failures
- Progressive prompt learning from VLM feedback

## Pipeline

1. **Scenario Generation** — LLM creates a structured storyboard with scene
   transitions, dynamic action descriptions, and narrative continuity cues
2. **Asset + Music Generation (parallel)** — Character sheets (parallel), location
   art (parallel), keyframes (SEQUENTIAL with inter-frame context), and music
   all run concurrently via `asyncio.gather`
3. **Image VLM Verification** — Each critical asset (characters, keyframes) is
   checked by Claude Sonnet for correctness and regenerated if needed (up to 2 retries)
4. **Video Animation** — Sequential scene generation with:
   - Full continuity chain (last frame → next scene reference)
   - ALL character sheets as references (not just first)
   - Explicit character identity descriptions in prompts
   - Negative prompting against text artifacts
   - **Video VLM verification via Gemini 3.1 Pro** (max 1 retry per scene)
   - **Smart error recovery** — LLM advisor recommends model switch on failure
5. **Assembly** — ffmpeg concatenation with audio mixing

## Models Used (via OpenRouter)

| Model | Purpose |
|-------|---------|
| `openai/gpt-image-2` | Character sheets, locations, keyframes (default, with fallback on transient failures) |
| `google/gemini-3.1-flash-image-preview` | Character sheets, locations, keyframes (nanobanana option and fallback) |
| `anthropic/claude-sonnet-4.6` | Scenario generation + image VLM verification |
| `google/gemini-3.5-flash` | **Error advisor + prompt condensation** — analyzes failures, condenses long prompts for models with character limits |
| `google/gemini-3.1-pro-preview` | **Video VLM verification** (multi-frame analysis) |
| `bytedance/seedance-2.0` | Video generation (default) |
| `google/veo-3.1` | Video generation (alternative — Google quality) |
| `minimax/hailuo-2.3` | Video generation (cheapest option) |
| `kwaivgi/kling-v3.0-pro` | Video generation (less strict copyright filter, auto-condensed prompts) |
| `google/lyria-3-pro-preview` | Original soundtrack clips |

## Requirements

- `OPENROUTER_API_KEY` with access to the models above
- `ffmpeg` installed on the host system (for video assembly + frame extraction)
