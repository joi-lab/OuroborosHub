---
name: nanobanana
description: Generate images from a text prompt via OpenRouter's image generation API (Nano Banana / Gemini Flash Image). Displays the result inside the widget and saves to disk for download.
version: 0.2.3
type: extension
runtime: python3
entry: plugin.py
permissions: [net, tool, route, widget, read_settings]
env_from_settings: [OPENROUTER_API_KEY]
when_to_use: User wants to generate or render an image from a text description, right now, inside the web UI. Also saves the image to disk so it can be downloaded.
ui_tab:
  tab_id: widget
  title: Nano Banana
  icon: image
  render:
    kind: declarative
    schema_version: 1
    components:
      - type: form
        title: Generate an image
        target: result
        route: generate
        method: POST
        job: true
        status_route: status
        interval_ms: 1500
        max_ticks: 120
        submit_label: Generate
        fields:
          - name: prompt
            label: Prompt
            type: textarea
            required: true
          - name: model
            label: Model
            type: select
            default: "google/gemini-3.1-flash-image-preview"
            options:
              - value: "google/gemini-3.1-flash-image-preview"
                label: "Nano Banana (Gemini 3.1 Flash)"
              - value: "google/gemini-3.1-flash-image-preview"
                label: "Nano Banana 2 (Gemini 3.1 Flash)"
              - value: "google/gemini-3-pro-image-preview"
                label: "Nano Banana Pro (Gemini 3 Pro)"
      - type: status
        target: result
        idle: "Enter a prompt and press Generate."
        loading: "Generating image…"
        success: "Done."
        error: "Generation failed."
      - type: markdown
        target: result
        path: error
      - type: image
        target: result
        path: image_url
        label: Generated image
        alt: Generated image
      - type: file
        target: result
        path: download_url
        label: Download image
---

# Nano Banana image generation widget

An extension skill that renders a generator inside the **Widgets** tab. Type
a prompt, pick a model, press **Generate**, and the image appears directly in
the card. From v0.2.0 the image is also **saved to disk** and a **Download image**
button appears below it.

Backed by OpenRouter's image generation endpoint. Three Google models available:
the default is **Nano Banana** (`google/gemini-3.1-flash-image-preview`).

## Persistence and download (v0.2.0)

From v0.2.0, every generation saves the image to the skill's private state
directory: `~/Ouroboros/data/state/skills/nanobanana/img_<12 hex chars>.<ext>`.

Two separate routes serve the image:

- `GET /api/extensions/nanobanana/media?image_id=...` — **inline** preview
  (Content-Disposition: inline), used by the `<img>` tag in the widget.
- `GET /api/extensions/nanobanana/download?image_id=...` — **attachment**
  download, sets proper filename so the file saves as `img_<hex>.png`.

Neither route requires the `OPENROUTER_API_KEY` grant — they serve files
already written to disk. A generated image stays accessible even if you later
revoke the generation grant.

## Setup

1. Put your [OpenRouter API key](https://openrouter.ai/keys) into Ouroboros
   **Settings → Providers → OpenRouter**.
2. Run `review_skill(skill="nanobanana")` and wait for a PASS verdict.
3. On the **Skills** tab, click **Grant access** for `OPENROUTER_API_KEY`.
   The grant is content-hash-bound: any edit to this skill invalidates it.
4. Enable the skill on the Skills tab.
5. Open the **Widgets** tab — the *Nano Banana* card appears.

## Usage

- **Via the widget**: type a prompt, optionally switch model, press Generate.
  The image appears below the form; the Download image button saves it locally.
- **Via the agent**: call `ext_<token>_nanobanana_generate(prompt="...")`.
  Returns JSON with `file_path`, `image_id`, `image_url`, `download_url`,
  `bytes`, `mime`, `model` on success, or `error`.

## Security model

- `OPENROUTER_API_KEY` only reaches the extension after owner grants access
  on the Skills tab. Grant is bound to current content hash.
- Key read exclusively via `PluginAPI.get_settings` — never via `os.environ`.
- Single-host allowlist (`openrouter.ai`). Cross-host redirects refused.
- Download/media routes: two-layer path traversal defense (strict regex +
  `Path.resolve().relative_to(state_dir)`).

## Limits

- Single image per request.
- Timeout: 60 seconds (dispatched via `asyncio.to_thread`).
- Prompts over 4 KB are truncated.
- State directory is not auto-cleaned. Delete old files freely — not part
  of the content hash, so deletion does not invalidate the grant.
