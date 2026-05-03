# OuroborosHub

**OuroborosHub is the official skills catalog for [Ouroboros](https://github.com/joi-lab/ouroboros-desktop).**

Ouroboros is a self-modifying desktop AI agent. Skills extend it with new tools, HTTP routes, widgets, scripts, and task-specific capabilities. This repository is the curated catalog the desktop app reads when you open **Skills -> OuroborosHub**.

Each catalog entry lists the files that belong to a skill and their SHA-256 hashes. Ouroboros downloads those files from this repository, verifies the hashes, runs its skill review, and only then lets the owner enable the skill.

## What is in this hub?

Current examples include:

| Skill | Type | What it does |
| --- | --- | --- |
| `weather` | extension | Live weather widget using `wttr.in`, no API key required. |
| `nanobanana` | extension | Image generation widget through OpenRouter image models. |
| `music_gen` | extension | Music generation widget through OpenRouter / Google Lyria. |
| `video_gen` | script | Video generation script skill. |
| `duckduckgo` | extension | Zero-key DuckDuckGo web search. |
| `perplexity` | extension | OpenRouter-backed deep web research with citations. |

The source of truth is [`catalog.json`](catalog.json). The actual skill payloads live under [`skills/`](skills/).

## For users

You normally do not need to clone this repository.

1. Install or run [Ouroboros Desktop](https://github.com/joi-lab/ouroboros-desktop).
2. Open **Skills**.
3. Open the **OuroborosHub** tab.
4. Pick a skill and click **Install**.
5. Wait for the security review to finish.
6. Enable the skill when it has a fresh `PASS` review.

Some skills may ask for owner-approved key grants before they can run. For example, `perplexity` needs `OPENROUTER_API_KEY`. Ouroboros handles that through the Skills UI and the desktop owner-confirmation flow.

## For skill authors

Start in the main Ouroboros repository:

- [Creating Skills for Ouroboros](https://github.com/joi-lab/ouroboros-desktop/blob/ouroboros/docs/CREATING_SKILLS.md) - practical author guide for `SKILL.md`, `PluginAPI`, widgets, review, grants, and publishing.
- [Skill Review Checklist](https://github.com/joi-lab/ouroboros-desktop/blob/ouroboros/docs/CHECKLISTS.md#skill-review-checklist) - what the reviewer models check before a skill can pass.
- [Architecture Reference](https://github.com/joi-lab/ouroboros-desktop/blob/ouroboros/docs/ARCHITECTURE.md) - how the skill loader, review state, extension loader, widgets, and marketplace surfaces work.

Skills are reviewed code. Keep them small, explicit, and honest about permissions.

## How to add a skill

A skill lives in its own folder:

```text
skills/<slug>/
  SKILL.md
  plugin.py          # for type: extension
  scripts/run.py     # for type: script
  widget.js          # optional, only for reviewed kind: module widgets
```

Then update `catalog.json` so Ouroboros can discover and verify it.

The easiest path is:

```bash
python scripts/build_catalog.py
```

That script walks `skills/*`, reads the frontmatter from each `SKILL.md`, computes SHA-256 hashes and file sizes, and rewrites `catalog.json`.

You can also edit `catalog.json` manually when needed. A minimal entry looks like this:

```json
{
  "slug": "example_skill",
  "name": "example_skill",
  "description": "Short, user-facing description.",
  "version": "0.1.0",
  "type": "extension",
  "files": [
    {
      "path": "SKILL.md",
      "sha256": "<sha256>",
      "size": 1234
    },
    {
      "path": "plugin.py",
      "sha256": "<sha256>",
      "size": 5678
    }
  ]
}
```

File paths are resolved as:

```text
https://raw.githubusercontent.com/joi-lab/OuroborosHub/main/skills/<slug>/<path>
```

`SKILL.md` must be present for every skill.

## Review and trust model

OuroborosHub is curated, but hub membership does not bypass review.

Every installed skill still goes through the normal Ouroboros lifecycle:

```text
install -> review_skill -> isolated deps (if any) -> owner enable -> execute/dispatch
```

Important constraints:

- Skill payloads are text-only. Do not vendor binary blobs, `.so`, `.dylib`, `.dll`, `.wasm`, `.pyc`, `.node`, model weights, or generated caches inside `skills/<slug>/`.
- Core setting keys such as `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, or `TELEGRAM_BOT_TOKEN` require explicit owner grants after a fresh `PASS` review.
- Dependencies should be declared through the supported skill metadata path and installed into the isolated per-skill environment by Ouroboros after review.
- Widgets should prefer host-owned declarative components. Custom `kind: "module"` widgets are sandboxed and get an extra `widget_module_safety` review item.
- Provenance files, review state, grants, dependency fingerprints, and enablement state are owner/review controlled. Skills should not write them.

## Repository layout

```text
catalog.json                 # public catalog consumed by Ouroboros
scripts/build_catalog.py      # rebuilds catalog hashes from skills/*
skills/weather/               # example extension skill
skills/duckduckgo/            # search extension
skills/perplexity/            # OpenRouter research extension
skills/<slug>/SKILL.md        # every skill starts here
```

## Related repositories

- [Ouroboros Desktop](https://github.com/joi-lab/ouroboros-desktop) - the main application.
- [Creating Skills for Ouroboros](https://github.com/joi-lab/ouroboros-desktop/blob/ouroboros/docs/CREATING_SKILLS.md) - skill authoring guide.
- [Original Ouroboros](https://github.com/joi-lab/ouroboros) - the earlier Colab/Telegram version.

## License

Skills in this repository should declare their own license in `SKILL.md` when relevant. The repository itself is maintained by Joi Lab as the official Ouroboros skills catalog.
