import asyncio, importlib.util, json, sys, types
from pathlib import Path


def _load():
    """Load plugin (which imports the lib modules) and return the notifier module
    where the budget/task notification helpers now live (post lib split)."""
    root = Path(__file__).resolve().parents[1]
    pkg = types.ModuleType("tg_nt"); pkg.__path__ = [str(root)]; sys.modules["tg_nt"] = pkg
    spec = importlib.util.spec_from_file_location("tg_nt.plugin", root / "plugin.py")
    m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return sys.modules["tg_nt.lib.telegram_notifier"]


class _Rec:
    sent = []
    def __init__(self, token): pass
    async def send_message(self, chat_id, text, parse_mode="HTML"):
        _Rec.sent.append((chat_id, text)); return 1


def _api(tmp_path):
    data = tmp_path / "data"
    sd = data / "state" / "skills" / "telegram-bridge"; sd.mkdir(parents=True)
    (data / "logs").mkdir(parents=True, exist_ok=True)

    class A:
        def get_state_dir(self): return str(sd)
        def get_settings(self, k): return {}  # _Rec ignores the token value
        def log(self, *a, **k): pass
    return A(), data


def test_budget_threshold_notify(tmp_path, monkeypatch):
    nt = _load(); api, data = _api(tmp_path); _Rec.sent = []
    monkeypatch.setattr(nt, "TelegramClient", _Rec)
    (data / "state" / "state.json").write_text(json.dumps({"spent_usd": 850}), encoding="utf-8")
    (data / "settings.json").write_text(json.dumps({"TOTAL_BUDGET": 1000}), encoding="utf-8")
    settings = {"TELEGRAM_NOTIFY_BUDGET": "on"}; state = {}
    asyncio.run(nt._check_budget_notify(api, settings, 42, state, "en"))
    assert len(_Rec.sent) == 1 and "85%" in _Rec.sent[0][1] and state["budget_threshold"] == 80
    asyncio.run(nt._check_budget_notify(api, settings, 42, state, "en"))   # same → no new
    assert len(_Rec.sent) == 1
    (data / "state" / "state.json").write_text(json.dumps({"spent_usd": 920}), encoding="utf-8")
    asyncio.run(nt._check_budget_notify(api, settings, 42, state, "en"))
    assert len(_Rec.sent) == 2 and "92%" in _Rec.sent[1][1] and state["budget_threshold"] == 90


def test_tasks_notify_primes_then_fires(tmp_path, monkeypatch):
    nt = _load(); api, data = _api(tmp_path); _Rec.sent = []
    monkeypatch.setattr(nt, "TelegramClient", _Rec)
    chat = data / "logs" / "chat.jsonl"
    chat.write_text(json.dumps({"type": "task_summary", "task_id": "old1", "rounds": 3,
                                "outcome_axes": {"lifecycle": "completed"}}) + "\n", encoding="utf-8")
    settings = {"TELEGRAM_NOTIFY_TASKS": "on"}; state = {}
    asyncio.run(nt._check_tasks_notify(api, settings, 42, state, "en"))   # primes, no send
    assert _Rec.sent == [] and "old1" in state["notified_task_ids"]
    with open(chat, "a", encoding="utf-8") as f:
        f.write(json.dumps({"type": "task_summary", "task_id": "new1", "rounds": 5,
                            "outcome_axes": {"lifecycle": "completed"}}) + "\n")
    asyncio.run(nt._check_tasks_notify(api, settings, 42, state, "en"))
    assert len(_Rec.sent) == 1 and "new1" in _Rec.sent[0][1] and "5r" in _Rec.sent[0][1]


def test_notify_disabled_is_silent(tmp_path, monkeypatch):
    nt = _load(); api, data = _api(tmp_path); _Rec.sent = []
    monkeypatch.setattr(nt, "TelegramClient", _Rec)
    (data / "state" / "state.json").write_text(json.dumps({"spent_usd": 999}), encoding="utf-8")
    (data / "settings.json").write_text(json.dumps({"TOTAL_BUDGET": 1000}), encoding="utf-8")
    asyncio.run(nt._check_budget_notify(api, {"TELEGRAM_NOTIFY_BUDGET": "off"}, 42, {}, "en"))
    assert _Rec.sent == []
