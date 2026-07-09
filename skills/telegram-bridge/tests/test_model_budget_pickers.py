import importlib.util, json, sys, types
from pathlib import Path


def _load_plugin():
    root = Path(__file__).resolve().parents[1]
    pkg = types.ModuleType("tg_mbp_test"); pkg.__path__ = [str(root)]
    sys.modules["tg_mbp_test"] = pkg
    spec = importlib.util.spec_from_file_location("tg_mbp_test.plugin", root / "plugin.py")
    m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m
    spec.loader.exec_module(m); return m


class _Api:
    """state dir holds the SKILL settings; data dir holds the PARENT settings."""
    def __init__(self, state_dir, data_dir):
        self._sd = Path(state_dir); self._dd = Path(data_dir)
    def get_state_dir(self): return str(self._sd)
    def get_runtime_info(self): return {"data_dir": str(self._dd)}
    def log(self, *a, **k): pass


def _setup(tmp_path, skill_settings=None, parent_settings=None):
    plugin = _load_plugin()
    sd = tmp_path / "state"; sd.mkdir(parents=True, exist_ok=True)
    dd = tmp_path / "data"; dd.mkdir(parents=True, exist_ok=True)
    (sd / "settings.json").write_text(json.dumps(skill_settings or {}), encoding="utf-8")
    (dd / "settings.json").write_text(json.dumps(parent_settings or {}), encoding="utf-8")
    return plugin, _Api(sd, dd)


def test_model_choices_default_when_unset(tmp_path):
    plugin, api = _setup(tmp_path)
    assert plugin._model_choices(api) == list(plugin._DEFAULT_MODEL_CHOICES)


def test_model_choices_custom_and_drops_garbage(tmp_path):
    plugin, api = _setup(tmp_path, skill_settings={"TELEGRAM_MODEL_CHOICES": "a/x , , b/y ,"})
    assert plugin._model_choices(api) == ["a/x", "b/y"]


def test_get_current_budget(tmp_path):
    plugin, api = _setup(tmp_path, parent_settings={"TOTAL_BUDGET": 500.0})
    assert plugin._get_current_budget(api) == 500.0
    plugin2, api2 = _setup(tmp_path)  # missing → 0.0, never crashes
    assert plugin2._get_current_budget(api2) == 0.0


def test_no_direct_core_settings_writer(tmp_path):
    # Path-confinement (triad blocker): the skill must NOT write the core
    # settings.json itself. The model/budget buttons inject an owner request
    # instead, so this private writer must not exist.
    plugin, _ = _setup(tmp_path)
    assert not hasattr(plugin, "_write_parent_setting")


def test_model_change_command_text(tmp_path):
    plugin, _ = _setup(tmp_path)
    en = plugin._model_change_command("anthropic/claude-opus-4.8", "en")
    assert "OUROBOROS_MODEL" in en and "anthropic/claude-opus-4.8" in en
    ru = plugin._model_change_command("x/y", "ru")
    assert "OUROBOROS_MODEL" in ru and "x/y" in ru


def test_budget_change_command_text(tmp_path):
    plugin, _ = _setup(tmp_path)
    en = plugin._budget_change_command(550.0, "en")
    assert "TOTAL_BUDGET" in en and "550.00" in en


def test_parse_budget_amount(tmp_path):
    plugin, _ = _setup(tmp_path)
    assert plugin._parse_budget_amount("250") == 250.0
    assert plugin._parse_budget_amount("  $42 ") == 42.0
    assert plugin._parse_budget_amount("250.50") == 250.5
    assert plugin._parse_budget_amount("$1,000") == 1000.0   # comma = thousands sep
    assert plugin._parse_budget_amount("$1,000.50") == 1000.5
    assert plugin._parse_budget_amount("250,50") == 250.5    # RU decimal comma
    assert plugin._parse_budget_amount("1,5") == 1.5         # RU decimal comma
    # rejects
    assert plugin._parse_budget_amount("abc") is None
    assert plugin._parse_budget_amount("") is None
    assert plugin._parse_budget_amount("0") is None          # must be > 0
    assert plugin._parse_budget_amount("0.001") is None      # rounds to $0.00 → rejected
    assert plugin._parse_budget_amount("-5") is None
    assert plugin._parse_budget_amount("99999999") is None   # over the 1,000,000 cap


def test_budget_keyboard_has_custom_button(tmp_path):
    plugin, api = _setup(tmp_path, parent_settings={"TOTAL_BUDGET": 100.0})
    _, rows = plugin._build_budget_keyboard(api, "en")
    flat = [b["callback_data"] for row in rows for b in row]
    assert "set_budget:custom" in flat
    assert "set_budget:50" in flat  # increments still there


def test_bot_commands_gated_by_mode(tmp_path):
    plugin, _ = _setup(tmp_path)
    full = {c["command"] for c in plugin._bot_commands("full_access")}
    safe = {c["command"] for c in plugin._bot_commands("safe_commands")}
    # always present
    assert {"menu", "status", "help"} <= full
    assert {"menu", "status", "help"} <= safe
    # owner-control commands only in full_access
    assert {"evolve", "restart", "panic"} <= full
    assert not ({"evolve", "restart", "panic"} & safe)


def test_model_keyboard_marks_current_and_uses_index(tmp_path):
    plugin, api = _setup(
        tmp_path,
        skill_settings={"TELEGRAM_MODEL_CHOICES": "p/a, p/b"},
        parent_settings={"OUROBOROS_MODEL": "p/b"},
    )
    header, rows = plugin._build_model_keyboard(api, "en")
    assert len(rows) == 3  # one button per choice + a back row
    assert rows[0][0]["callback_data"] == "set_model:0"
    assert rows[1][0]["callback_data"] == "set_model:1"
    assert rows[1][0]["text"].startswith("✓ ")  # p/b is the active model
    assert not rows[0][0]["text"].startswith("✓")
    assert rows[-1][0]["callback_data"] == "nav:settings"


def test_budget_keyboard_increments_and_shows_current(tmp_path):
    plugin, api = _setup(tmp_path, parent_settings={"TOTAL_BUDGET": 500.0})
    header, rows = plugin._build_budget_keyboard(api, "en")
    assert [b["callback_data"] for b in rows[0]] == ["set_budget:50", "set_budget:100", "set_budget:500"]
    assert "500.00" in header
    assert rows[-1][0]["callback_data"] == "nav:settings"


def test_settings_panel_exposes_model_and_budget(tmp_path):
    plugin, api = _setup(tmp_path)
    _, rows = plugin._build_menu_settings(api, plugin._COMMAND_MODE_FULL, "en")
    flat = [b["callback_data"] for row in rows for b in row]
    assert "nav:model" in flat
    assert "nav:budget" in flat
