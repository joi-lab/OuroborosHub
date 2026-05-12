---
name: video_studio
description: Hollywood-grade photorealistic video production with Gemini multimodal AV QC, parallel best-of-N candidate generation, effort-based quality control (low/regular/max), voice/dialogue synthesis, and Director cross-scene review
version: 1.1.0
type: extension
entry: plugin.py
permissions: [net, route, widget, ws_handler, tool, read_settings, subprocess]
env_from_settings: [OPENROUTER_API_KEY]
when_to_use: User wants to generate a photorealistic cinematic video, film scene, documentary, live-action style video, or realistic narrative with consistent characters, Gemini multimodal quality control, and Hollywood-grade production quality. Separate from anime_studio which generates 2D anime cartoons.
timeout_sec: 300
dependencies: [Pillow]
ui_tab:
  tab_id: video_studio
  title: Video Studio
  icon: video
  render:
    kind: declarative
    schema_version: 1
    components:
      - type: form
        title: "🎬 Generate Cinematic Video"
        route: generate
        method: POST
        mode: job
        status_route: status
        fields:
          - name: theme
            label: Theme / Story
            type: textarea
            placeholder: "A detective investigates a mystery in rain-soaked neon-lit city..."
            required: true
          - name: style
            label: Visual Style
            type: select
            options: ["photorealistic cinematic", "documentary realism", "noir thriller", "romantic drama", "sci-fi blockbuster", "action thriller", "period drama", "horror atmospheric"]
            default: photorealistic cinematic
          - name: mood
            label: Mood
            type: select
            options: ["dramatic", "tense", "romantic", "melancholic", "triumphant", "mysterious", "comedic", "action-packed"]
            default: dramatic
          - name: duration_sec
            label: Duration (seconds)
            type: number
            default: 30
          - name: num_scenes
            label: Number of Scenes
            type: number
            default: 4
          - name: effort
            label: Quality Effort
            type: select
            options: ["low", "regular", "max"]
            default: regular
          - name: video_model
            label: Video Model
            type: select
            options: ["bytedance/seedance-2.0", "bytedance/seedance-2.0-fast", "google/veo-3.1"]
            default: "bytedance/seedance-2.0"
          - name: music_style
            label: Music Style
            type: select
            options: ["orchestral cinematic", "electronic ambient", "acoustic intimate", "jazz noir", "epic action", "minimalist tension"]
            default: orchestral cinematic
          - name: generate_audio
            label: Generate Voice/Dialogue
            type: select
            options: ["true", "false"]
            default: "true"
        submit_label: "🎬 Generate Video"
      - type: subscription
        event: video_studio_progress
        render:
          - type: progress
            value_key: progress_pct
            label_key: message
          - type: gallery
            title: Character References
            items_key: character_sheets
            item_type: image
            route_prefix: "asset?path="
          - type: gallery
            title: Scene Keyframes
            items_key: keyframes
            item_type: image
            route_prefix: "asset?path="
          - type: key_value
            title: Quality Scores
            items_key: quality_display
            condition_key: has_quality
          - type: key_value
            title: Warnings
            items_key: warnings_display
            condition_key: has_warnings
---

# Video Studio v1.0

Hollywood-grade photorealistic video generator with Gemini multimodal AV QC.

## Key Features

- **Effort Levels**: `low` (fast, 1 candidate), `regular` (2 parallel candidates), `max` (3 candidates + Gemini AV QC + Director pass)
- **Parallel Candidate Generation**: Character refs, keyframes, and video clips generated in parallel batches; best candidate selected by VLM
- **Gemini 2.5 Pro AV QC**: At effort=max, full video+audio analysis via Gemini (visual, audio, AV sync, identity, motion)
- **Director QC Pass**: At effort=max, cross-scene coherence review by Gemini; flags scenes for regeneration
- **Voice/Dialogue**: When generate_audio=true, Seedance natively generates voices for dialogue lines
- **Progressive Learning**: Lessons from VLM rejections persist across jobs and inject into future prompts

## Pipeline (effort=max)

1. **Scenario** — Cinematic screenwriter LLM creates storyboard with lens specs, color temp, camera notes
2. **Character Refs** — 3 candidates in parallel per character → VLM picks best
3. **Location Art** — Parallel generation
4. **Keyframes** — Sequential with inter-scene context chain; 3 candidates → VLM picks best
5. **Music** — AI-composed soundtrack via Lyria 3 Pro
6. **Video** — Sequential scenes with continuity chain; Gemini AV QC per scene; Director cross-scene pass
7. **Assembly** — ffmpeg concat with audio mix

## Models Used (via OpenRouter)

| Model | Purpose |
|-------|---------|
| `google/gemini-3.1-flash-image-preview` | Character refs, keyframes (default) |
| `openai/gpt-5.4-image-2` | Alternative via gpt-image-2 |
| `anthropic/claude-sonnet-4.6` | Scenario + image VLM verification |
| `google/gemini-2.5-pro` | Video AV QC + Director QC (effort=max) |
| `bytedance/seedance-2.0` | Video generation (default) |
| `google/lyria-3-pro-preview` | Original soundtrack |
