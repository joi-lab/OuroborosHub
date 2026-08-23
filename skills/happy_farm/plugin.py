"""Happy Farm extension skill plugin for Ouroboros.

Provides widget registration, game configuration, and server-side state backup
for the Happy Farm simulation game.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

GAME_VERSION = "0.1.0"

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": GAME_VERSION,
    "grid_cols": 8,
    "grid_rows": 6,
    "tile_size": 64,
    "initial_coins": 150,
    "crops": {
        "wheat": {"id": "wheat", "name": "Wheat", "name_ru": "Пшеница", "icon": "🌾", "growth_sec": 12, "seed_cost": 5, "sell_price": 12, "xp": 5, "min_level": 1, "color": "#eab308"},
        "carrot": {"id": "carrot", "name": "Carrot", "name_ru": "Морковь", "icon": "🥕", "growth_sec": 24, "seed_cost": 10, "sell_price": 26, "xp": 12, "min_level": 1, "color": "#f97316"},
        "tomato": {"id": "tomato", "name": "Tomato", "name_ru": "Помидор", "icon": "🍅", "growth_sec": 40, "seed_cost": 20, "sell_price": 55, "xp": 25, "min_level": 2, "color": "#ef4444"},
        "strawberry": {"id": "strawberry", "name": "Strawberry", "name_ru": "Клубника", "icon": "🍓", "growth_sec": 60, "seed_cost": 35, "sell_price": 100, "xp": 45, "min_level": 3, "color": "#ec4899"},
        "corn": {"id": "corn", "name": "Corn", "name_ru": "Кукуруза", "icon": "🌽", "growth_sec": 90, "seed_cost": 60, "sell_price": 175, "xp": 80, "min_level": 4, "color": "#facc15"},
        "watermelon": {"id": "watermelon", "name": "Watermelon", "name_ru": "Арбуз", "icon": "🍉", "growth_sec": 130, "seed_cost": 100, "sell_price": 310, "xp": 140, "min_level": 5, "color": "#22c55e"},
        "pumpkin": {"id": "pumpkin", "name": "Magic Pumpkin", "name_ru": "Тыква", "icon": "🎃", "growth_sec": 180, "seed_cost": 180, "sell_price": 580, "xp": 260, "min_level": 6, "color": "#d97706"},
    },
    "animals": {
        "chicken": {"id": "chicken", "name": "Chicken", "name_ru": "Курица", "icon": "🐔", "cost": 120, "product": "egg", "product_name_ru": "Яйцо", "product_icon": "🥚", "produce_sec": 35, "product_sell": 45, "xp": 20, "min_level": 2},
        "cow": {"id": "cow", "name": "Cow", "name_ru": "Корова", "icon": "🐮", "cost": 350, "product": "milk", "product_name_ru": "Молоко", "product_icon": "🥛", "produce_sec": 65, "product_sell": 130, "xp": 55, "min_level": 3},
        "sheep": {"id": "sheep", "name": "Sheep", "name_ru": "Овца", "icon": "🐑", "cost": 650, "product": "wool", "product_name_ru": "Шерсть", "product_icon": "🧶", "produce_sec": 95, "product_sell": 260, "xp": 110, "min_level": 5},
    },
    "tools": [
        {"id": "hand", "name": "Hand", "name_ru": "Рука", "icon": "🖐", "desc": "Inspect & harvest"},
        {"id": "hoe", "name": "Hoe", "name_ru": "Тяпка", "icon": "⛏️", "desc": "Till grassy soil"},
        {"id": "water", "name": "Water Can", "name_ru": "Лейка", "icon": "💧", "desc": "Water dry crops"},
        {"id": "scythe", "name": "Scythe", "name_ru": "Серп", "icon": "🌾", "desc": "Mass harvest crops"},
        {"id": "seed", "name": "Seed", "name_ru": "Семена", "icon": "🌱", "desc": "Plant selected crop"},
        {"id": "fertilizer", "name": "Fertilizer", "name_ru": "Удобрение", "icon": "✨", "cost": 15, "desc": "Instant growth boost (-50% time)"},
        {"id": "feed", "name": "Feed", "name_ru": "Корм", "icon": "🌾", "cost": 8, "desc": "Feed livestock to produce"},
    ],
    "upgrades": {
        "sprinkler": {"id": "sprinkler", "name": "Auto-Sprinkler", "name_ru": "Автополивалка", "icon": "🚿", "cost": 400, "min_level": 4, "desc": "Waters 3x3 surrounding plots automatically"},
        "scarecrow": {"id": "scarecrow", "name": "Scarecrow", "name_ru": "Пугало", "icon": "🪵", "cost": 250, "min_level": 3, "desc": "Gives +20% extra harvest yields"},
        "silo": {"id": "silo", "name": "Big Silo", "name_ru": "Большой амбар", "icon": "🏡", "cost": 500, "min_level": 3, "desc": "Expands storage capacity"},
    },
    "xp_levels": [0, 50, 150, 350, 750, 1400, 2400, 4000, 6500, 10000, 15000],
}


def _get_save_file(state_dir: str) -> Path:
    p = Path(state_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p / "farm_save.json"


def load_farm_save(state_dir: str, logger: Any = None) -> Dict[str, Any]:
    save_file = _get_save_file(state_dir)
    if save_file.is_file():
        try:
            with open(save_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            if logger and hasattr(logger, "log"):
                logger.log("warning", f"farm save file is unreadable: {exc}")
    return {}


def save_farm_data(state_dir: str, data: Dict[str, Any], logger: Any = None) -> bool:
    save_file = _get_save_file(state_dir)
    try:
        temp_file = save_file.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        temp_file.replace(save_file)
        return True
    except Exception as exc:
        if logger and hasattr(logger, "log"):
            logger.log("error", f"failed to write farm save: {exc}")
        return False


def register(api: Any) -> None:
    """Register Happy Farm extension routes, UI tab, and hooks."""
    state_dir = api.get_state_dir()

    def get_config_route(_request: Any) -> Dict[str, Any]:
        save = load_farm_save(state_dir, logger=api)
        return {
            "ok": True,
            "version": GAME_VERSION,
            "config": DEFAULT_CONFIG,
            "server_time": time.time(),
            "has_cloud_save": bool(save),
            "cloud_save": save if save else None,
        }

    async def save_state_route(request: Any) -> Dict[str, Any]:
        try:
            body_data = None
            if hasattr(request, "json"):
                try:
                    res = request.json()
                    if hasattr(res, "__await__"):
                        body_data = await res
                    else:
                        body_data = res
                except Exception:
                    pass
            if not body_data:
                if isinstance(request, dict):
                    raw_body = request.get("body")
                    if isinstance(raw_body, str):
                        try:
                            body_data = json.loads(raw_body)
                        except Exception:
                            body_data = None
                    elif isinstance(raw_body, dict):
                        body_data = raw_body
                    else:
                        body_data = request

            if body_data and isinstance(body_data, dict):
                body_data["saved_at"] = time.time()
                success = save_farm_data(state_dir, body_data, logger=api)
                return {"ok": success, "saved_at": body_data["saved_at"]}
            return {"ok": False, "error": "Payload must be a JSON object"}
        except Exception as exc:
            api.log("error", f"Failed to handle save request: {exc}")
            return {"ok": False, "error": str(exc)}

    api.register_route("config", get_config_route, methods=("GET",))
    api.register_route("save", save_state_route, methods=("POST",))

    api.register_ui_tab(
        tab_id="happy_farm",
        title="Happy Farm",
        icon="leaf",
        render={
            "kind": "module",
            "entry": "widget.js",
            "span": 2,
            "height": 760,
            "max_height": 920,
        },
    )
    api.log("info", f"Happy Farm v{GAME_VERSION} initialized and UI tab registered.")
