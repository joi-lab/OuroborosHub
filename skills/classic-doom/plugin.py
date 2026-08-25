"""Classic Doom (1993) Extension Widget Plugin for Ouroboros.

Provides UI tab registration and routes for the standalone Doom 1993 2.5D FPS engine.
"""

from __future__ import annotations
from typing import Any, Dict

GAME_VERSION = "1.0.0"


def register(api: Any) -> None:
    """Register Classic Doom extension UI tab and optional status route."""
    def get_info_route(_request: Any) -> Dict[str, Any]:
        return {
            "ok": True,
            "game": "Classic Doom (1993)",
            "version": GAME_VERSION,
            "engine": "Ouroboros 2.5D Pure Raycaster",
            "maps": ["E1M1 - Hangar", "E1M2 - Nuclear Plant", "E1M8 - Phobos Anomaly"],
            "weapons": ["Fist", "Pistol", "Shotgun", "Chaingun", "Plasma Gun"],
        }

    api.register_route("info", get_info_route, methods=("GET",))

    api.register_ui_tab(
        tab_id="classic_doom",
        title="Classic Doom (1993)",
        icon="crosshairs",
        render={
            "kind": "module",
            "entry": "widget.js",
            "span": 2,
            "height": 820,
            "max_height": 960,
        },
    )
    api.log("info", f"Classic Doom (1993) v{GAME_VERSION} initialized and UI tab registered.")
