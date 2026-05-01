---
name: music_gen
description: Generate a music clip from a text prompt via OpenRouter's Google Lyria model and play it directly inside the widget. Files are persisted to the skill's private state directory and streamed through an extension route for both inline playback and download.
version: 0.5.1
type: extension
runtime: python3
entry: plugin.py
permissions: [net, tool, route, widget, read_settings]
env_from_settings: [OPENROUTER_API_KEY]
when_to_use: User wants to generate music or an audio clip from a text prompt and listen to it inline in the web UI.
ui_tab:
  tab_id: music_gen
  title: Music generator
  icon: music
  render:
    kind: declarative
    schema_version: 1
    components:
      - type: form
        title: Generate a music clip
        target: result
        route: generate
        method: POST
        job: true
        status_route: status
        interval_ms: 2000
        max_ticks: 180
        submit_label: Generate
        fields:
          - name: prompt
            label: Prompt
            type: textarea
            required: true
      - type: status
        target: result
        idle: "Enter a music prompt and press Generate. Typical latency: 30–60 seconds."
        loading: "Generating music — this usually takes 30–60 seconds…"
        success: "Done."
        error: "Generation failed."
      - type: markdown
        target: result
        path: error
      - type: audio
        target: result
        path: clip_url
        label: Generated audio
      - type: file
        target: result
        path: clip_url
        label: Download audio
      - type: kv
        target: result
        fields:
          - path: detected
            label: Format
          - path: mime
            label: MIME
          - path: file_size_bytes
            label: File size
---

# Music generation widget

An extension skill that renders a music generator inside the **Widgets** tab.
Type a prompt, press **Generate**, and the resulting clip plays directly in
the card with a **Download audio** button.

Backed by OpenRouter's SSE audio API and Google's **`google/lyria-3-pro-preview`**
model. Lyria produces 48 kHz stereo audio; each request is capped at 20 MB and
a 180-second deadline.

## Persistence and download

Every generation writes the clip to the skill state directory:
`~/Ouroboros/data/state/skills/music_gen/audio_<12 hex>.<ext>`.

Both the `<audio>` player and the **Download audio** button stream this file
via `GET /api/extensions/music_gen/download?clip_id=...`. No data URLs in DOM.

The download route does NOT need the `OPENROUTER_API_KEY` grant — already
persisted clips are accessible even if the grant is later revoked.

## Setup

1. Put your [OpenRouter API key](https://openrouter.ai/keys) into Ouroboros
   **Settings → Providers → OpenRouter**.
2. Run `review_skill(skill="music_gen")` and wait for a **PASS** verdict.
3. **Skills** tab → **Grant access** for `OPENROUTER_API_KEY`.
4. Enable the skill. 5. **Widgets** tab → *Music generator* card.

## Usage

- **Via the widget**: type a prompt, press Generate, wait 30–60 s. Player
  + Download button appear; a small kv panel shows format/MIME/size.
- **Via the agent**: call `ext_<token>_music_gen_generate(prompt="...")`.
  Returns JSON with `file_path`, `clip_id`, `clip_url`, `bytes`, `mime`,
  `detected`, `model`, `prompt_used`.

## Security model

Same as image_gen: key via `PluginAPI.get_settings` only; single-host
allowlist; two-layer path traversal defense on download route.

## Limits

- One clip per request. Lyria ≈ $0.08/song on OpenRouter (Mar 2026).
- Max 20 MB decoded audio. Timeout: 180 s.
- State dir not auto-cleaned — delete old files freely.
