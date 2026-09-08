"""Unit tests for Happy Farm extension skill."""

import asyncio
import json
import pytest
from pathlib import Path

import plugin


def test_default_config_integrity():
    """Verify config has all required crop, animal, tool, and upgrade definitions."""
    cfg = plugin.DEFAULT_CONFIG
    assert "crops" in cfg
    assert "animals" in cfg
    assert "tools" in cfg
    assert "upgrades" in cfg
    assert cfg["grid_cols"] == 8
    assert cfg["grid_rows"] == 6
    assert cfg["tile_size"] >= 48

    crops = cfg["crops"]
    for crop_id in ["wheat", "carrot", "tomato", "strawberry", "corn", "watermelon", "pumpkin"]:
        assert crop_id in crops, f"Missing crop {crop_id}"
        crop = crops[crop_id]
        assert crop["id"] == crop_id
        assert crop["growth_sec"] > 0
        assert crop["seed_cost"] > 0
        assert crop["sell_price"] > crop["seed_cost"]
        assert crop["xp"] > 0
        assert "name_ru" in crop
        assert "icon" in crop

    animals = cfg["animals"]
    for animal_id in ["chicken", "cow", "sheep"]:
        assert animal_id in animals, f"Missing animal {animal_id}"
        animal = animals[animal_id]
        assert animal["id"] == animal_id
        assert animal["cost"] > 0
        assert animal["produce_sec"] > 0
        assert animal["product_sell"] > 0
        assert "product_name_ru" in animal
        assert "product_icon" in animal

    assert any(t["id"] == "scythe" for t in cfg["tools"])
    assert len(cfg["xp_levels"]) >= 8


def test_save_load_cycle(tmp_path):
    """Test saving and loading farm state from local state directory."""
    state_dir = str(tmp_path)
    assert plugin.load_farm_save(state_dir) == {}

    test_state = {
        "coins": 500,
        "level": 3,
        "xp": 450,
        "grid": [{"crop": "wheat", "stage": 3, "watered": True}],
        "inventory": {"produce": {"wheat": 15, "egg": 4}},
        "animals": [{"type": "chicken", "fed": True}],
    }
    assert plugin.save_farm_data(state_dir, test_state) is True

    loaded = plugin.load_farm_save(state_dir)
    assert loaded["coins"] == 500
    assert loaded["level"] == 3
    assert loaded["inventory"]["produce"]["wheat"] == 15
    assert len(loaded["grid"]) == 1


class MockAPI:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.routes = {}
        self.tabs = {}
        self.logs = []

    def get_state_dir(self):
        return self.state_dir

    def register_route(self, path, handler, methods=("GET",)):
        self.routes[path] = {"handler": handler, "methods": methods}

    def register_ui_tab(self, tab_id, title, icon="extension", render=None):
        self.tabs[tab_id] = {"title": title, "icon": icon, "render": render}

    def log(self, level, message):
        self.logs.append((level, message))


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


def test_plugin_registration_and_routes(tmp_path):
    api = MockAPI(str(tmp_path))
    plugin.register(api)

    assert "config" in api.routes
    assert "save" in api.routes
    assert api.routes["save"]["methods"] == ("POST",)

    assert "happy_farm" in api.tabs
    tab = api.tabs["happy_farm"]
    assert tab["title"] == "Happy Farm"
    assert tab["render"]["kind"] == "module"
    assert tab["render"]["entry"] == "widget.js"
    assert tab["render"]["height"] >= 700

    config_handler = api.routes["config"]["handler"]
    res = config_handler({})
    assert res["ok"] is True
    assert res["version"] == plugin.GAME_VERSION
    assert "crops" in res["config"]
    assert res["has_cloud_save"] is False

    save_handler = api.routes["save"]["handler"]
    save_res = asyncio.run(save_handler(FakeRequest({"coins": 1000, "level": 5})))
    assert save_res["ok"] is True
    assert "saved_at" in save_res

    res2 = config_handler({})
    assert res2["has_cloud_save"] is True
    assert res2["cloud_save"]["coins"] == 1000


def test_sync_dict_save_compatibility(tmp_path):
    """The route also supports the host's dict test shape."""
    api = MockAPI(str(tmp_path))
    plugin.register(api)
    save_handler = api.routes["save"]["handler"]
    res = asyncio.run(save_handler({"coins": 250, "level": 2}))
    assert res["ok"] is True
    assert plugin.load_farm_save(str(tmp_path))["coins"] == 250
