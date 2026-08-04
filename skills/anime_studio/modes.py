"""Quality-mode matrix for Anime Studio.

Cost honesty: only the `medium` per-scene cost is MEASURED — $7.39/scene,
derived from exactly one 2-scene medium run that cost $14.77. The `low` and
`max` per-scene numbers are EXTRAPOLATIONS from that single data point (fewer /
more candidates, judges, and continuity passes), not facts. Treat every
non-medium estimate as a planning aid, never as a billing statement.
"""
from __future__ import annotations

from types import MappingProxyType

QUALITY_MODES = ("low", "medium", "max")
DEFAULT_QUALITY_MODE = "medium"

# Hard outer bounds, independent of quality mode. Kept here (not in plugin.py)
# so the worker and the route clamp against the same authority.
GLOBAL_SCENE_CAP = 24
GLOBAL_DURATION_CAP_SEC = 240

# Per-mode generation policy. Frozen (read-only mapping of read-only rows is
# approximated with MappingProxyType; consumers get mutable shallow copies via
# mode_config()).
MODE_MATRIX = MappingProxyType({
    "low": MappingProxyType({
        "video_candidates": 1,
        "continuity": "off",
        "continuity_regen_max": 0,
        "judges": 0,
        "scene_cap": 4,
        "est_usd_per_scene": 3.2,
        "est_basis": "extrapolated",
    }),
    "medium": MappingProxyType({
        "video_candidates": 2,
        "continuity": "adjacent",
        "continuity_regen_max": 1,
        "judges": 1,
        "scene_cap": 8,
        "est_usd_per_scene": 7.39,
        "est_basis": "measured",
    }),
    "max": MappingProxyType({
        "video_candidates": 3,
        "continuity": "all_recheck",
        "continuity_regen_max": 2,
        "judges": 2,
        "scene_cap": 24,
        "est_usd_per_scene": 13.0,
        "est_basis": "extrapolated",
    }),
})


def normalize_mode(value) -> str:
    """Coerce arbitrary input to a known quality mode.

    Unknown / None / blank / non-string values fall back to
    DEFAULT_QUALITY_MODE; matching is case-insensitive and
    whitespace-tolerant. Never raises.
    """
    if not isinstance(value, str):
        return DEFAULT_QUALITY_MODE
    candidate = value.strip().lower()
    if candidate in MODE_MATRIX:
        return candidate
    return DEFAULT_QUALITY_MODE


def mode_config(mode) -> dict:
    """Return a mutable shallow copy of the matrix row for the normalized mode."""
    return dict(MODE_MATRIX[normalize_mode(mode)])
