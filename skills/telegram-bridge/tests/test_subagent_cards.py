import asyncio, importlib.util, json, sys, types
from pathlib import Path


def _load_plugin():
    root = Path(__file__).resolve().parents[1]
    pkg = types.ModuleType("tg_sub_test"); pkg.__path__ = [str(root)]
    sys.modules["tg_sub_test"] = pkg
    spec = importlib.util.spec_from_file_location("tg_sub_test.plugin", root / "plugin.py")
    m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m
    spec.loader.exec_module(m); return m


class _Api:
    def __init__(self, sd): self.sd = Path(sd)
    def get_state_dir(self): return str(self.sd)
    def get_settings(self, keys): return {"TELEGRAM_BOT_TOKEN": "token"}
    def log(self, *a, **k): pass


class _Rec:
    sent = []; edited = []; _id = [1000]
    def __init__(self, token): pass
    async def send_message(self, chat_id, text, parse_mode="HTML"):
        _Rec._id[0] += 1; _Rec.sent.append((chat_id, text, parse_mode, _Rec._id[0])); return _Rec._id[0]
    async def edit_message_text(self, chat_id, message_id, text, parse_mode="HTML"):
        _Rec.edited.append((chat_id, message_id, text, parse_mode)); return True
    async def send_chat_action(self, chat_id, action="typing"): pass


def _setup(tmp_path, monkeypatch, **settings):
    plugin = _load_plugin()
    sd = tmp_path / "state"; sd.mkdir(parents=True, exist_ok=True)
    base = {"TELEGRAM_CHAT_ID": "42", "TELEGRAM_MIRROR_MODE": "all"}; base.update(settings)
    (sd / "settings.json").write_text(json.dumps(base), encoding="utf-8")
    monkeypatch.setattr(plugin, "TelegramClient", _Rec)
    _Rec.sent = []; _Rec.edited = []; _Rec._id = [1000]
    return plugin, _Api(sd)


def test_subagent_gets_one_edited_bubble(tmp_path, monkeypatch):
    plugin, api = _setup(tmp_path, monkeypatch)
    handle = plugin._make_outbound(api)
    asyncio.run(handle({"text": "Subagent abc scheduled (researcher)", "is_progress": True,
                        "subagent_event": "scheduled", "subagent_task_id": "abc123", "subagent_role": "researcher"}))
    assert len(_Rec.sent) == 1 and not _Rec.edited
    first_id = _Rec.sent[0][3]
    asyncio.run(handle({"text": "Subagent abc completed (researcher)", "is_progress": True,
                        "subagent_event": "completed", "subagent_task_id": "abc123", "subagent_role": "researcher", "cost_usd": 0.04}))
    assert len(_Rec.sent) == 1                       # NO second message
    assert len(_Rec.edited) == 1 and _Rec.edited[0][1] == first_id
    assert "done" in _Rec.edited[0][2] and "$0.04" in _Rec.edited[0][2]


def test_generic_progress_gated_by_default(tmp_path, monkeypatch):
    plugin, api = _setup(tmp_path, monkeypatch)
    handle = plugin._make_outbound(api)
    asyncio.run(handle({"text": "Thinking...", "is_progress": True}))
    assert not _Rec.sent and not _Rec.edited        # dropped


def test_mirror_progress_on_restores_firehose(tmp_path, monkeypatch):
    plugin, api = _setup(tmp_path, monkeypatch, TELEGRAM_MIRROR_PROGRESS="on")
    handle = plugin._make_outbound(api)
    asyncio.run(handle({"text": "Thinking...", "is_progress": True}))
    assert len(_Rec.sent) == 1                       # mirrored when opted in


def test_real_reply_is_sent(tmp_path, monkeypatch):
    plugin, api = _setup(tmp_path, monkeypatch)
    handle = plugin._make_outbound(api)
    asyncio.run(handle({"text": "Here is your answer", "is_progress": False}))
    assert len(_Rec.sent) == 1 and "answer" in _Rec.sent[0][1]


def test_subagent_cards_off_hides_activity(tmp_path, monkeypatch):
    plugin, api = _setup(tmp_path, monkeypatch, TELEGRAM_SUBAGENT_CARDS="off")
    handle = plugin._make_outbound(api)
    asyncio.run(handle({"text": "Subagent abc scheduled", "is_progress": True,
                        "subagent_event": "scheduled", "subagent_task_id": "abc"}))
    assert not _Rec.sent and not _Rec.edited




def test_markdown_hint_false_sends_plain(tmp_path, monkeypatch):
    plugin, api = _setup(tmp_path, monkeypatch)
    handle = plugin._make_outbound(api)
    asyncio.run(handle({"text": "use * and a_b_c.txt", "is_progress": False, "markdown": False}))
    assert _Rec.sent and _Rec.sent[0][2] == ""        # plain → no markdown→HTML mangling


def test_markdown_hint_absent_or_true_sends_html(tmp_path, monkeypatch):
    plugin, api = _setup(tmp_path, monkeypatch)
    handle = plugin._make_outbound(api)
    asyncio.run(handle({"text": "**bold**", "is_progress": False}))            # no markdown key
    assert _Rec.sent and _Rec.sent[0][2] == "HTML"
    _Rec.sent.clear()
    asyncio.run(handle({"text": "**bold**", "is_progress": False, "markdown": True}))
    assert _Rec.sent and _Rec.sent[0][2] == "HTML"




def test_subagent_card_shows_work_commentary(tmp_path, monkeypatch):
    # Complaint #2: the card must carry the live work note (like the UI), not
    # just a bare status line.
    plugin, api = _setup(tmp_path, monkeypatch)
    handle = plugin._make_outbound(api)
    asyncio.run(handle({"text": "I will read lines 710 to 765 of ouroboros/llm.py.",
                        "is_progress": True, "subagent_event": "progress",
                        "subagent_task_id": "27f40120", "subagent_role": "planning-scout-1"}))
    assert len(_Rec.sent) == 1
    card = _Rec.sent[0][1]
    assert "planning-scout-1" in card
    assert "I will read lines 710 to 765" in card  # the actual commentary


def test_subagent_progress_edits_one_bubble_not_a_flood(tmp_path, monkeypatch):
    # Complaint #1: a stream of progress notes for ONE subagent must edit a
    # single bubble in place (the note changes each time), never post new ones.
    plugin, api = _setup(tmp_path, monkeypatch)
    handle = plugin._make_outbound(api)
    for note in ("reading llm.py retry logic", "searching the recovery loop", "examining vision params"):
        asyncio.run(handle({"text": note, "is_progress": True, "subagent_event": "progress",
                            "subagent_task_id": "27f40120", "subagent_role": "planning-scout-1"}))
    assert len(_Rec.sent) == 1     # ONE bubble — not a flood
    assert len(_Rec.edited) == 2   # subsequent notes edit in place
    assert "examining vision params" in _Rec.edited[-1][2]  # shows the latest note


def test_edit_message_not_modified_treated_as_success():
    # Identical-text edits ("message is not modified") must read as success so
    # the card render never falls back to posting a duplicate bubble.
    plugin = _load_plugin()
    client = plugin.TelegramClient("token")

    async def _raise(msg):
        raise RuntimeError(msg)

    async def call_not_modified(method, **kw):
        await _raise("Telegram API editMessageText returned HTTP 400: Bad Request: message is not modified")

    async def call_real_error(method, **kw):
        await _raise("Telegram API editMessageText returned HTTP 400: Bad Request: message to edit not found")

    client.call = call_not_modified
    assert asyncio.run(client.edit_message_text(42, 100, "x", parse_mode="")) is True
    client.call = call_real_error
    assert asyncio.run(client.edit_message_text(42, 100, "x", parse_mode="")) is False
