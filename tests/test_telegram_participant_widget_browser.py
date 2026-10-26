"""Telegram connector widget through the actual opaque module bridge and registered routes.

Opt in with OUROBOROS_THEME_CORE_ROOT. Synthetic private state only: no session, no
companion, no Telegram or Host call. Screenshots go to OUROBOROS_TELEGRAM_EVIDENCE_DIR.
"""
import asyncio
import importlib.util
import json
import mimetypes
import os
import shutil
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills' / 'telegram-user-session'
ACCOUNT = 8667525238
HOST = '''<!doctype html><html><head><script src="/static/theme.js"></script>
<style>body{margin:0;background:#0f0f11}.widgets-frame{width:100%;height:var(--widget-frame-height,320px);border:0;display:block}</style></head>
<body><section><div class="widgets-card-status"></div><div id="mount"></div></section>
<script type="module">
import {mountModuleWidget} from '/static/modules/widget_module.js';
ouroTheme.set('dark');
window.disposeWidget=await mountModuleWidget(document.getElementById('mount'),
 {skill:'telegram-user-session',ws_prefix:'ext:telegram-user-session:'},
 {entry:'widget.js',appearance:'host'},null,new Set());
window.ready=true;
</script></body></html>'''


class Request:
    def __init__(self, method, body, query):
        self.method, self._body, self.query_params = method, body, query

    async def json(self):
        return json.loads(self._body or b'null')

    async def body(self):
        return self._body


class API:
    def __init__(self, root):
        self.root, self.routes = root, {}

    def get_state_dir(self):
        return str(self.root)

    def register_route(self, name, handler, methods):
        self.routes[name] = handler

    def register_tool(self, *a, **kw):
        pass

    def register_companion_process(self, name):
        pass

    def register_settings_section(self, *a, **kw):
        pass

    def register_ui_tab(self, *a, **kw):
        pass


def synthetic_state(root):
    sys.path.insert(0, str(SKILL))
    from userbot.config import Config, save_config, _write_json
    from userbot.participant_state import Journal, save_policy
    save_config(root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT))
    save_policy(root, {'mode': 'participant', 'outgoing': True, 'blocked_peers': [-1003]})
    now = datetime.now(timezone.utc).isoformat()
    _write_json(root, 'companion-health.json', {'category': 'running', 'at': now})
    _write_json(root, 'place-state.json', {'state': 'active', 'label': 'Telegram', 'at': now})
    _write_json(root, 'account-profile.json', {'id': str(ACCOUNT), 'username': 'ouro_demo', 'first_name': 'Ouroboros', 'last_name': 'AI'})
    _write_json(root, 'host-capabilities.json', {'at': now, 'places_version': 1, 'presence_delivery_version': 1})
    _write_json(root, 'chats-refresh-status.json', {'status': 'done', 'at': now, 'count': 4, 'complete': True})
    with Journal(root) as journal:
        journal.remember_chat(ACCOUNT, -1001, 'Board games on Fridays', 'supergroup', 'dialogs', members=24)
        journal.remember_chat(ACCOUNT, 4242, 'Mira', 'private', 'incoming')
        journal.remember_chat(ACCOUNT, -1002, 'Open workshop: interfaces and agents with a deliberately long title for wrapping', 'forum', 'dialogs', members=184)
        journal.remember_chat(ACCOUNT, -1003, 'Old announcements channel', 'channel', 'dialogs')
        journal.enqueue(str(ACCOUNT), 'held-reply', 'send_text', {'peer': '-1001', 'text': 'held'})
        row = dict(journal.db.execute("SELECT * FROM jobs WHERE id='held-reply'").fetchone())
        journal.hold(row, 'destination_inactive')


def load_plugin(api):
    name = 'telegram_widget_browser_test'
    package = ModuleType(name); package.__path__ = [str(SKILL)]
    sys.modules[name] = package
    spec = importlib.util.spec_from_file_location(name + '.plugin', SKILL / 'plugin.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.register(api)


@pytest.fixture
def widget_server():
    configured = os.environ.get('OUROBOROS_THEME_CORE_ROOT')
    if not configured:
        pytest.skip('set OUROBOROS_THEME_CORE_ROOT for the real host bridge')
    core = Path(configured).resolve()
    assert (core / 'web/modules/widget_module.js').is_file()
    root = Path(tempfile.mkdtemp(prefix='tg-widget-'))
    synthetic_state(root)
    api = API(root)
    load_plugin(api)
    state = {'posts': [], 'errors': [], 'faults': []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, mime='application/json', status=200):
            if not isinstance(body, bytes):
                body = (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def dispatch(self):
            url = urlsplit(self.path)
            payload = self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
            if url.path == '/':
                return self.send(HOST, 'text/html')
            if url.path == '/api/widgets/fault':
                state['faults'].append(payload.decode('utf-8', 'replace'))
                return self.send({'ok': True})
            if url.path.startswith('/static/'):
                web = core / 'web'
                source = (web / url.path[len('/static/'):]).resolve()
                if source.is_relative_to(web) and source.is_file():
                    return self.send(source.read_bytes(), mimetypes.guess_type(str(source))[0] or 'text/javascript')
            prefix = '/api/extensions/telegram-user-session/'
            if url.path == prefix + 'module/widget.js':
                return self.send((SKILL / 'widget.js').read_bytes(), 'text/javascript')
            name = url.path.removeprefix(prefix)
            if name in api.routes:
                if self.command == 'POST':
                    state['posts'].append((name, json.loads(payload or b'null')))
                try:
                    query = {k: v[-1] for k, v in parse_qs(url.query).items()}
                    result = asyncio.run(api.routes[name](Request(self.command, payload, query)))
                    return self.send(result.body, status=result.status_code)
                except Exception as exc:
                    state['errors'].append(repr(exc))
                    return self.send({'error': str(exc)}, status=500)
            return self.send({'error': 'not found'}, status=404)
        do_GET = dispatch
        do_POST = dispatch

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield root, state, f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(root)
        assert not thread.is_alive()
        assert not state['errors'], state['errors']


def settle(page, frame):
    """The host bridge grows the frame after a paint; measure only once it has.

    The frame is an opaque origin, so its height is read inside it and the
    iframe box from the host page.
    """
    for _ in range(60):
        content = frame.evaluate('document.documentElement.scrollHeight')
        box = page.locator('iframe').bounding_box()
        if abs(box['height'] - content) <= 3:
            break
        page.wait_for_timeout(50)
    assert box['height'] + 3 >= content, (box, content)
    assert frame.evaluate('document.documentElement.scrollWidth <= innerWidth')


def read_policy(root):
    from userbot.participant_state import policy
    return policy(root)


@pytest.mark.parametrize('engine,width', [('chromium', 1100), ('webkit', 390), ('webkit', 1100)])
def test_telegram_widget_paths(widget_server, tmp_path, engine, width):
    from playwright.sync_api import sync_playwright, expect
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    messages = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={'width': width, 'height': 900})
        page.on('console', lambda m: messages.append((m.type, m.text)) if m.type == 'error' else None)
        page.on('pageerror', lambda e: messages.append(('pageerror', str(e))))
        try:
            page.goto(url)
            page.wait_for_function('window.ready === true')
            frame_node = page.locator('iframe')
            assert 'allow-forms' not in frame_node.get_attribute('sandbox')
            frame = frame_node.element_handle().content_frame()
            # A verified account opens on what it hears; names, never bare IDs, are the rows.
            expect(frame.get_by_role('button', name='2 · What I hear')).to_have_attribute('aria-pressed', 'true')
            expect(frame.locator('.check', has_text='Board games on Fridays')).to_have_count(1)
            excluded = frame.locator('.check', has_text='Old announcements channel').locator('input')
            expect(excluded).not_to_be_checked()
            expect(frame.locator('#scope-count')).to_have_text('Receiving 3 of 4 listed chats.')
            # No current Host can record imported history; the note says so instead of offering it.
            before = frame.locator('.tray', has_text='What about messages from before?')
            expect(before).to_contain_text('planned and not available yet')
            expect(before).not_to_contain_text('can be imported')
            frame.locator('.check', has_text='Mira').locator('input').uncheck()
            expect(frame.locator('#scope-count')).to_have_text('Receiving 2 of 4 listed chats.')
            frame.get_by_role('button', name='Save what I hear').click()
            expect(frame.locator('[data-result="scope"]')).to_contain_text('Participant controls saved')
            assert sorted(read_policy(root)['blocked_peers']) == [-1003, 4242]
            # The narrower option: only chosen chats are received.
            frame.get_by_role('button', name='Selected chats').click()
            expect(frame.locator('#scope-count')).to_have_text('Receiving 0 of 4 listed chats.')
            frame.locator('.check', has_text='Board games on Fridays').locator('input').check()
            frame.get_by_role('button', name='Save what I hear').click()
            expect(frame.locator('[data-result="scope"]')).to_contain_text('Participant controls saved')
            saved = read_policy(root)
            assert (saved['scope'], saved['selected_peers']) == ('selected', [-1001])
            frame.get_by_role('button', name='Refresh chat list from Telegram').click()
            expect(frame.locator('.meta', has_text='Refresh requested')).to_have_count(1)
            assert (root / 'chats-refresh.json').is_file()
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-{width}-scope-dark.png'), full_page=True)

            # Two clearly different ways in; the bot path never asks for a token here.
            frame.get_by_role('button', name='1 · Connect').click()
            expect(frame.get_by_role('button', name='Dedicated account')).to_have_attribute('aria-pressed', 'true')
            expect(frame.locator('.profile')).to_contain_text('@ouro_demo')
            expect(frame.locator('#heartbeat')).to_have_text('Running')
            # Nothing reads the bio, so the AI disclosure is stated as a setting to keep, not as observed.
            appear = frame.locator('.capline', has_text='How I appear')
            expect(appear).to_contain_text('should say I am an AI')
            expect(appear).to_contain_text('does not check')
            expect(frame.locator('.secondary', has_text='An account created for Ouroboros')).to_contain_text('should say openly')
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-{width}-connect-account-dark.png'), full_page=True)
            frame.get_by_role('button', name='Telegram bot').click()
            expect(frame.locator('.capline', has_text='What reaches me')).to_contain_text('cannot read earlier history')
            expect(frame.locator('input[data-draft="api_hash"]')).to_have_count(0)
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-{width}-connect-bot-dark.png'), full_page=True)
            frame.get_by_role('button', name='Dedicated account').click()
            frame.get_by_role('button', name='Request login code').click()
            expect(frame.locator('[data-result="code"]')).to_contain_text('delivery is not confirmed')
            assert (root / 'login-request.json').is_file()
            frame.get_by_role('button', name='Request login code').click()
            expect(frame.locator('[data-result="code"]')).to_contain_text('already pending')
            frame.locator('input[data-draft="api_id"]').fill('123')
            frame.get_by_role('button', name='Save account setup').click()
            expect(frame.locator('[data-result="setup"]')).to_contain_text('No Telegram login was performed')

            # Participation: the local switch and held work, with Places as the behaviour editor.
            frame.get_by_role('button', name='3 · Participation').click()
            expect(frame.locator('#place-chip')).to_contain_text('Active')
            expect(frame.locator('#queue-line')).to_contain_text('1 held')
            frame.get_by_role('button', name='Discard 1 held action').click()
            expect(frame.locator('[data-result="held"]')).to_contain_text('1 held operation(s) discarded')
            frame.locator('[data-control="outgoing"]').uncheck()
            expect(frame.locator('.tray', has_text='ones already queued wait as held')).to_have_count(0)
            frame.get_by_role('button', name='Save participation').click()
            expect(frame.locator('[data-result="controls"]')).to_contain_text('Participant controls saved')
            assert read_policy(root)['outgoing'] is False
            expect(frame.locator('.tray', has_text='ones already queued wait as held')).to_have_count(1)
            note = frame.locator('textarea[data-draft="note"]')
            expect(note).to_contain_text('ask me in Main and wait')
            expect(note).to_contain_text('Telegram admin rights alone do not make you a moderator')
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-{width}-participation-dark.png'), full_page=True)

            # History: an honest unavailable state, a saved plan, and the Bot API limit stated.
            frame.get_by_role('button', name='4 · History').click()
            expect(frame.locator('.warning').first).to_contain_text('Not available yet')
            expect(frame.locator('.capline', has_text='Telegram bot')).to_contain_text('cannot read earlier chat history')
            frame.get_by_role('button', name='Chosen chats').click()
            frame.locator('.check', has_text='Board games on Fridays').locator('input').check()
            frame.get_by_role('button', name='Save import plan').click()
            expect(frame.locator('[data-result="import"]')).to_contain_text('Nothing is read from Telegram')
            expect(frame.locator('.row', has_text='Saved · not started')).to_have_count(1)
            plan_posts = [body for name, body in state['posts'] if name == 'participant/import']
            assert plan_posts[-1]['chats'] == ['-1001'] and plan_posts[-1]['exclude'] == []
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-{width}-history-dark.png'), full_page=True)
            frame.get_by_role('button', name='Cancel').click()
            expect(frame.locator('.row', has_text='Cancelled')).to_have_count(1)

            for theme in ('light', 'dark'):
                page.evaluate(f"ouroTheme.set('{theme}')")
                expect(frame.locator('html')).to_have_attribute('data-theme', theme)
            page.evaluate("ouroTheme.set('light')")
            for scene in ('1 · Connect', '2 · What I hear', '3 · Participation', '4 · History'):
                frame.get_by_role('button', name=scene).click()
                settle(page, frame)
                page.screenshot(path=str(evidence / f"telegram-{engine}-{width}-{scene[4:].replace(' ', '-').lower()}-light.png"), full_page=True)
            page.evaluate('async () => { await disposeWidget(); }')
            expect(page.locator('iframe')).to_have_count(0)
        finally:
            browser.close()
    # The second code request is refused with 409 by design; browsers log that response, not a script error.
    refused = [m for m in messages if m == ('error', 'Failed to load resource: the server responded with a status of 409 (Conflict)')]
    assert len(refused) <= 1 and [m for m in messages if m not in refused] == [], messages
    assert not state['faults'], state['faults']
    # The widget only ever called its own skill routes, and never a Telegram or Host action.
    assert {name for name, _ in state['posts']} <= {'participant/policy', 'participant/chats', 'participant/held',
                                                     'participant/import', 'setup/config', 'setup/request_code'}


def open_widget(browser, url, width, messages, clock=False):
    page = browser.new_page(viewport={'width': width, 'height': 900})
    page.on('console', lambda m: messages.append((m.type, m.text)) if m.type == 'error' else None)
    page.on('pageerror', lambda e: messages.append(('pageerror', str(e))))
    if clock:
        page.clock.install()  # One clock for the host page and the opaque widget frame.
    page.goto(url)
    page.wait_for_function('window.ready === true')
    return page, page.locator('iframe').element_handle().content_frame()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_outgoing_draft_is_not_reported_as_saved(widget_server, tmp_path, engine):
    from playwright.sync_api import sync_playwright, expect
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    messages = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page, frame = open_widget(browser, url, 1100, messages)
            frame.get_by_role('button', name='3 · Participation').click()
            tray = frame.locator('section', has_text='Outgoing actions').locator('.tray')
            save = frame.get_by_role('button', name='Save participation')
            expect(tray).to_contain_text('On. Every action')
            # Unticking is a draft: the companion keeps the saved switch until Save.
            frame.locator('[data-control="outgoing"]').uncheck()
            expect(tray).to_contain_text('On. Every action')
            expect(tray).to_contain_text('Not saved yet. Saving turns outgoing actions off.')
            expect(save).to_be_enabled()
            assert read_policy(root)['outgoing'] is True
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-1100-participation-unsaved-dark.png'), full_page=True)
            save.click()
            expect(frame.locator('[data-result="controls"]')).to_contain_text('Participant controls saved')
            assert read_policy(root)['outgoing'] is False
            expect(tray).to_contain_text('Off. I cannot start Telegram actions')
            expect(tray).not_to_contain_text('Not saved')
            expect(save).to_be_disabled()
            frame.locator('[data-control="outgoing"]').check()
            expect(tray).to_contain_text('Off. I cannot start Telegram actions')
            expect(tray).to_contain_text('Saving turns outgoing actions on.')
            assert read_policy(root)['outgoing'] is False
        finally:
            browser.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert {name for name, _ in state['posts']} == {'participant/policy'}


def discard_held(root):
    from userbot.participant_state import Journal
    with Journal(root) as journal:
        return journal.discard_held(str(ACCOUNT))


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_status_poll_keeps_open_details_focus_and_draft(widget_server, tmp_path, engine):
    """A changed held count makes the 20-second status poll re-render the scene."""
    from playwright.sync_api import sync_playwright, expect
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    messages = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page, frame = open_widget(browser, url, 1100, messages, clock=True)
            frame.get_by_role('button', name='3 · Participation').click()
            expect(frame.locator('#queue-line')).to_contain_text('1 held')
            details = frame.locator('details', has_text='Identity, pacing and transport mode')
            details.locator('summary').click()
            expect(details).to_have_js_property('open', True)
            account = frame.locator('[data-control="expected_account_id"]')
            account.fill('86675')
            account.press('ArrowLeft'); account.press('ArrowLeft')
            assert discard_held(root) == 1  # Behind the widget, as the companion or another tab would.
            page.clock.run_for(21000)
            expect(frame.locator('#queue-line')).to_contain_text('0 held')
            expect(details).to_have_js_property('open', True)
            assert frame.evaluate('document.activeElement && document.activeElement.dataset.control') == 'expected_account_id'
            assert frame.evaluate('[document.activeElement.selectionStart, document.activeElement.selectionEnd]') == [3, 3]
            expect(account).to_have_value('86675')
            page.keyboard.type('2')
            expect(account).to_have_value('866275')  # Typed at the kept caret, not at an end.
            # A field that nothing reads is not offered as owner recognition.
            expect(frame.locator('[data-control="owner_id"]')).to_have_count(0)
            expect(frame.locator('#controls-unsaved')).to_be_visible()
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-1100-participation-details-after-poll-dark.png'), full_page=True)
        finally:
            browser.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert state['posts'] == []


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_completed_chat_refresh_appears_without_losing_unsaved_scope(widget_server, tmp_path, engine):
    """The companion finishes a requested dialog read later; the open widget shows it by itself."""
    from playwright.sync_api import sync_playwright, expect
    from userbot.config import _write_json
    from userbot.participant_state import Journal
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    messages = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page, frame = open_widget(browser, url, 1100, messages, clock=True)
            refresh = frame.get_by_role('button', name='Refresh chat list from Telegram')
            refresh.click()
            expect(frame.locator('.meta', has_text='Refresh requested')).to_have_count(1)
            expect(refresh).to_be_disabled()
            # An unsaved choice and a half-typed ID with the caret inside it.
            frame.locator('.check', has_text='Mira').locator('input').uncheck()
            expect(frame.locator('#scope-count')).to_have_text('Receiving 2 of 4 listed chats.')
            added = frame.locator('[data-draft="addPeer"]')
            added.fill('-100777'); added.press('ArrowLeft'); added.press('ArrowLeft')
            # Exactly what the companion writes when its one dialog read completes.
            with Journal(root) as journal:
                journal.remember_chat(ACCOUNT, 4243, 'Newly fetched chat', 'private', 'dialogs')
            (root / 'chats-refresh.json').unlink()
            _write_json(root, 'chats-refresh-status.json', {'status': 'done', 'count': 5, 'complete': True,
                                                            'at': datetime.now(timezone.utc).isoformat()})
            page.clock.run_for(6000)
            expect(frame.locator('.check', has_text='Newly fetched chat')).to_have_count(1)
            expect(frame.locator('.meta', has_text='Read from Telegram')).to_have_count(1)
            expect(refresh).to_be_enabled()
            expect(frame.locator('#scope-count')).to_have_text('Receiving 3 of 5 listed chats.')
            expect(frame.locator('.check', has_text='Mira').locator('input')).not_to_be_checked()
            expect(frame.get_by_role('button', name='Save what I hear')).to_be_enabled()
            assert frame.evaluate('document.activeElement && document.activeElement.dataset.draft') == 'addPeer'
            assert frame.evaluate('[document.activeElement.selectionStart, document.activeElement.selectionEnd]') == [5, 5]
            expect(added).to_have_value('-100777')
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-1100-scope-after-refresh-dark.png'), full_page=True)
        finally:
            browser.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert state['posts'] == [('participant/chats', {'refresh': True})]
    assert read_policy(root)['blocked_peers'] == [-1003]  # nothing was saved behind the owner's back


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_fresh_legacy_install_offers_participant_as_its_first_step(widget_server, tmp_path, engine):
    from playwright.sync_api import sync_playwright, expect
    from userbot.config import Config, save_config
    from userbot.participant_state import save_policy
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    save_policy(root, {'mode': 'legacy'})  # a fresh install's default, with nothing enrolled for it
    messages = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page, frame = open_widget(browser, url, 1100, messages)
            step = frame.locator('.first-step')
            expect(step).to_contain_text('First step: choose Free participant')
            expect(step).to_contain_text('none are enrolled')
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-1100-first-step-dark.png'), full_page=True)
            frame.get_by_role('button', name='Choose Free participant').click()
            expect(frame.locator('[data-result="first-step"]')).to_contain_text('Participant controls saved')
            expect(step).to_contain_text('Free participant chosen')
            expect(step).to_contain_text('turned off and on')
            assert read_policy(root)['mode'] == 'participant'
            frame.get_by_role('button', name='3 · Participation').click()
            expect(frame.locator('select[data-control="mode"]')).to_have_value('participant')
            # An enrolled legacy install keeps its mode and is not asked to switch.
            save_config(root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT,
                                     binding_id='b' * 32, allowed_peer_ids=(-1001,)))
            save_policy(root, {'mode': 'legacy'})
            _, frame = open_widget(browser, url, 1100, messages)
            frame.get_by_role('button', name='3 · Participation').click()
            expect(frame.locator('#queue-line')).to_contain_text('held')  # status has been read
            expect(frame.locator('.first-step')).to_have_count(0)
        finally:
            browser.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert state['posts'] == [('participant/policy', {'mode': 'participant'})]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_saving_legacy_again_in_the_same_widget_offers_the_first_step_again(widget_server, tmp_path, engine):
    from playwright.sync_api import sync_playwright, expect
    from userbot.participant_state import save_policy
    root, state, url = widget_server
    evidence = Path(os.environ.get('OUROBOROS_TELEGRAM_EVIDENCE_DIR', str(tmp_path)))
    save_policy(root, {'mode': 'legacy'})  # a fresh install's default, with nothing enrolled for it
    messages = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            page, frame = open_widget(browser, url, 1100, messages)
            step = frame.locator('.first-step')
            frame.get_by_role('button', name='Choose Free participant').click()
            expect(step).to_contain_text('Free participant chosen')
            # The owner goes back to Legacy in this same mount, from Participation.
            frame.get_by_role('button', name='3 · Participation').click()
            frame.locator('details', has_text='Identity, pacing and transport mode').locator('summary').click()
            frame.locator('select[data-control="mode"]').select_option('legacy')
            frame.get_by_role('button', name='Save participation').click()
            expect(frame.locator('[data-result="controls"]')).to_contain_text('turned off and on')
            assert read_policy(root)['mode'] == 'legacy'
            expect(step).to_contain_text('First step: choose Free participant')
            expect(step).not_to_contain_text('Free participant chosen')
            expect(frame.locator('[data-result="first-step"]')).to_have_text('')  # no receipt of the undone choice
            expect(frame.get_by_role('button', name='Choose Free participant')).to_be_enabled()
            settle(page, frame); page.screenshot(path=str(evidence / f'telegram-{engine}-1100-first-step-after-legacy-saved-dark.png'), full_page=True)
        finally:
            browser.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert [(name, body['mode']) for name, body in state['posts']] == [('participant/policy', 'participant'),
                                                                        ('participant/policy', 'legacy')]


def test_held_incoming_and_saved_plans_are_described_without_promises(widget_server):
    from playwright.sync_api import sync_playwright, expect
    from userbot.config import _write_json
    from userbot.participant_state import Journal
    from userbot.presence import build_event
    root, state, url = widget_server
    with Journal(root) as journal:
        journal.capture(build_event(ACCOUNT, 4242, 77, 4242, 'hello'))
        journal.inbox_update(journal.heads()[0]['seq'], 'held', [])
    _write_json(root, 'host-capabilities.json', {'at': datetime.now(timezone.utc).isoformat(), 'places_version': 1,
                                                 'presence_delivery_version': 1, 'presence_history_import_version': 1})
    messages = []
    with sync_playwright() as pw:
        browser = pw.webkit.launch(headless=True)
        try:
            page, frame = open_widget(browser, url, 1100, messages)
            frame.get_by_role('button', name='3 · Participation').click()
            expect(frame.locator('#queue-line')).to_contain_text('1 incoming held.')
            expect(frame.locator('#queue-line')).not_to_contain_text('by the host')
            frame.get_by_role('button', name='4 · History').click()
            availability = frame.locator('.tray .warning')
            expect(availability).to_contain_text('never starts by itself')
            expect(availability).to_contain_text('your explicit start')
            expect(frame.locator('.capline', has_text='Where to read it')).to_contain_text('Ouroboros versions with a Conversations view')
        finally:
            browser.close()
    assert messages == [], messages
    assert not state['faults'], state['faults']
    assert state['posts'] == []
