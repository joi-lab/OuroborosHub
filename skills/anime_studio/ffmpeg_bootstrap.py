"""Resolve operator-provided ffmpeg/ffprobe binaries.

Never downloads remote executables. Callers must supply binaries via:
  1. ``FFMPEG_PATH`` / ``FFPROBE_PATH`` environment variables (executable files), or
  2. system ``PATH`` (``shutil.which``).

A missing binary raises ``FFmpegNotFoundError`` with install guidance.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger("anime_studio.ffmpeg_bootstrap")


class FFmpegNotFoundError(RuntimeError):
    """Raised when ffmpeg or ffprobe cannot be resolved without downloading."""


def _configured_path(tool: str) -> Optional[str]:
    """Return a pre-configured executable path from ``{TOOL}_PATH`` if valid."""
    val = os.environ.get(f"{tool.upper()}_PATH")
    if not val:
        return None
    candidate = Path(val)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        log.debug("Found pre-configured %s at %s", tool, val)
        return str(candidate)
    log.warning(
        "%s_PATH=%r is set but is not an executable file",
        tool.upper(),
        val,
    )
    return None


def _system_path(tool: str) -> Optional[str]:
    path = shutil.which(tool)
    if path:
        log.debug("Found system %s at %s", tool, path)
    return path


def resolve_tool(tool: str) -> Optional[str]:
    """Resolve *tool* (``ffmpeg`` or ``ffprobe``) without downloading."""
    return _configured_path(tool) or _system_path(tool)


def get_ffmpeg_path(state_dir: str | Path | None = None) -> Optional[str]:
    """Return ffmpeg path if available. ``state_dir`` is ignored (API compat)."""
    del state_dir  # no longer caches/downloads under state_dir
    return resolve_tool("ffmpeg")


def get_ffprobe_path(state_dir: str | Path | None = None) -> Optional[str]:
    """Return ffprobe path if available. ``state_dir`` is ignored (API compat)."""
    del state_dir
    return resolve_tool("ffprobe")


def ensure_ffmpeg(
    state_dir: str | Path | None = None,
    on_progress=None,
) -> Dict[str, str]:
    """Return ``{"ffmpeg": path, "ffprobe": path}`` from operator-provided binaries.

    Does **not** download. ``state_dir`` and ``on_progress`` are accepted for
    call-site compatibility and ignored.

    Raises:
        FFmpegNotFoundError: if either tool is missing.
    """
    del state_dir, on_progress
    missing = []
    result: Dict[str, str] = {}
    for tool in ("ffmpeg", "ffprobe"):
        path = resolve_tool(tool)
        if path:
            result[tool] = path
        else:
            missing.append(tool)

    if missing:
        tools = " and ".join(missing)
        raise FFmpegNotFoundError(
            f"{tools} not found. Install system ffmpeg/ffprobe (e.g. "
            f"`brew install ffmpeg` on macOS, `apt install ffmpeg` on Debian/Ubuntu) "
            f"or set FFMPEG_PATH / FFPROBE_PATH to executable binaries. "
            f"Automatic download of remote executables is disabled for security."
        )
    return result
