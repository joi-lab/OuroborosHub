"""One strict reader for this skill's own ``settings.json``.

The extension child and the companion process both read the same file, so they
share one contract: an absent file means "not configured yet" and is the only
condition that yields an empty mapping. A file that exists but cannot be read,
is not JSON, or is not a JSON object is a typed error, never a silent empty
settings dict that would look like a missing Presence binding and let a save
overwrite bytes nobody could read.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

SETTINGS_FILENAME = "settings.json"


class LocalSettingsError(RuntimeError):
    """``settings.json`` exists but is unreadable, not JSON, or not an object."""


def settings_path(state_dir: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(state_dir) / SETTINGS_FILENAME


def load_local_settings(state_dir: pathlib.Path | str) -> dict[str, Any]:
    """Return the saved settings object, or ``{}`` only when the file is absent."""

    path = settings_path(state_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise LocalSettingsError(
            f"{SETTINGS_FILENAME} exists but could not be read: {exc}"
        ) from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LocalSettingsError(
            f"{SETTINGS_FILENAME} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise LocalSettingsError(f"{SETTINGS_FILENAME} must contain a JSON object")
    return value
