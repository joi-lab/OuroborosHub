"""Widget routes on isolated synthetic state: scope, chat list, held work, import plans.

No companion runs here, so nothing reaches Telegram or a Host.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import ModuleType, SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from userbot.config import Config, save_config, _write_json
from userbot.participant_state import Journal, save_policy, policy

ACCOUNT = 8667525238


def load_plugin(name):
    package = ModuleType(name); package.__path__ = [str(ROOT)]
    sys.modules[name] = package
    spec = importlib.util.spec_from_file_location(name + '.plugin', ROOT / 'plugin.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Request:
    def __init__(self, method='GET', body=None):
        self.method, self._body, self.query_params = method, body, {}
    async def json(self):
        return self._body


class API:
    def __init__(self, root):
        self.root, self.routes, self.tools, self.tabs = root, {}, {}, []
    def get_state_dir(self): return str(self.root)
    def register_tool(self, name, handler, **kw): self.tools[name] = kw.get('description', '')
    def register_route(self, name, handler, methods): self.routes[name] = handler
    def register_companion_process(self, name): pass
    def register_settings_section(self, *a, **kw): pass
    def register_ui_tab(self, tab_id, title, render): self.tabs.append(render)


class Routes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT))
        save_policy(self.root, {'mode': 'participant', 'outgoing': True})
        self.api = API(self.root)
        load_plugin('participant_routes_test').register(self.api)

    def tearDown(self):
        self.tmp.cleanup()

    async def call(self, name, method='GET', body=None):
        result = await self.api.routes[name](Request(method, body))
        return result.status_code, json.loads(result.body)

    async def test_registered_tab_is_wide_module_with_host_appearance(self):
        self.assertEqual(self.api.tabs, [{'kind': 'module', 'entry': 'widget.js', 'start': 'auto', 'appearance': 'host', 'span': 2}])
        for name in ('participant/chats', 'participant/held', 'participant/import', 'participant/policy', 'participant/status'):
            self.assertIn(name, self.api.routes)

    async def test_tool_descriptions_point_to_place_notes_without_rules(self):
        self.assertIn('ask in Main and wait', self.api.tools['tg_join'])
        self.assertIn('one short note to Main', self.api.tools['tg_join'])
        for name in ('tg_ban', 'tg_mute', 'tg_admin'):
            self.assertIn('do not make you a moderator', self.api.tools[name])
        self.assertIn('Your own messages are yours to delete', self.api.tools['tg_delete'])
        self.assertIn('held was never attempted', self.api.tools['tg_status'])
        self.assertNotIn('place note', self.api.tools['tg_history'])

    async def test_descriptions_name_every_receipt_state_and_lasting_effect(self):
        from userbot.participant_ops import SPECS
        for state in ('done', 'sent', 'failed', 'unknown', 'held', 'cancelled'):
            self.assertIn(state, self.api.tools['tg_status'])
        self.assertIn('cancelled', self.api.tools['tg_send_text'])
        # Any message-effect operation can be a first contact, not only text.
        for op, (effect, _, _) in SPECS.items():
            self.assertEqual('ask in Main and wait' in self.api.tools['tg_' + op], effect == 'message' or op == 'join', op)
        self.assertIn('request_submitted is not a join', self.api.tools['tg_join'])
        self.assertIn('for everyone in the chat', self.api.tools['tg_delete'])

    async def test_unused_owner_id_is_not_asked_for_and_stored_value_is_kept(self):
        from userbot.participant_plugin import SETTINGS
        fields = [field['name'] for part in SETTINGS['components'] for field in part.get('fields', [])]
        self.assertNotIn('owner_id', fields)
        self.assertNotIn('Recognize the configured owner by numeric ID', json.dumps(SETTINGS))
        _write_json(self.root, 'participant.json', {**policy(self.root), 'owner_id': '42'})
        code, body = await self.call('participant/policy', 'POST', {'interval': '5'})
        self.assertEqual(code, 200, body)
        self.assertEqual((policy(self.root)['owner_id'], policy(self.root)['interval']), ('42', 5))

    async def test_policy_round_trips_scope_and_selection(self):
        code, body = await self.call('participant/policy', 'POST', {'scope': 'selected', 'selected_peers': '-1001, 809, 809', 'blocked_peers': ''})
        self.assertEqual(code, 200, body)
        code, body = await self.call('participant/policy')
        self.assertEqual((body['scope'], body['selected_peers'], body['blocked_peers']), ('selected', '-1001,809', ''))
        code, body = await self.call('participant/policy', 'POST', {'scope': 'some'})
        self.assertEqual(code, 409)
        self.assertEqual(policy(self.root)['scope'], 'selected')

    async def test_chat_list_marks_scope_and_refresh_only_stages_one_request(self):
        with Journal(self.root) as journal:
            journal.remember_chat(ACCOUNT, -1001, 'Board games', 'supergroup', 'dialogs', members=24)
            journal.remember_chat(ACCOUNT, 809, 'Mira', 'private', 'incoming')
            journal.remember_chat(ACCOUNT, -1002, 'Old announcements', 'channel', 'dialogs')
        save_policy(self.root, {'blocked_peers': [-1002]})
        code, body = await self.call('participant/chats')
        self.assertEqual(code, 200)
        rows = {c['id']: (c['title'], c['receiving'], c['excluded']) for c in body['chats']}
        self.assertEqual(rows, {'-1001': ('Board games', True, False), '809': ('Mira', True, False),
                                '-1002': ('Old announcements', False, True)})
        self.assertFalse(body['refresh']['pending'])
        for _ in range(2):
            code, reply = await self.call('participant/chats', 'POST', {'refresh': True})
            self.assertEqual(code, 200)
        self.assertTrue((self.root / 'chats-refresh.json').is_file())
        self.assertTrue((await self.call('participant/chats'))[1]['refresh']['pending'])
        self.assertEqual((await self.call('participant/chats', 'POST', {'refresh': 'yes'}))[0], 409)

    async def test_stale_inflight_refresh_is_not_pending(self):
        import os, time
        marker = self.root / 'chats-refresh.inflight'
        marker.write_text('')
        self.assertTrue((await self.call('participant/chats'))[1]['refresh']['pending'])
        old = time.time() - 600
        os.utime(marker, (old, old))
        self.assertFalse((await self.call('participant/chats'))[1]['refresh']['pending'])

    async def test_chat_refresh_needs_a_verified_account(self):
        save_config(self.root, Config(123, 'a' * 32, '+1234567890'))
        code, body = await self.call('participant/chats', 'POST', {'refresh': True})
        self.assertEqual((code, body['code']), (409, 'account_not_verified'))
        self.assertFalse((self.root / 'chats-refresh.json').exists())

    async def test_discard_held_reports_count_and_never_touches_attempted(self):
        with Journal(self.root) as journal:
            journal.enqueue(str(ACCOUNT), 'held', 'send_text', {'peer': '1', 'text': 'x'})
            journal.enqueue(str(ACCOUNT), 'attempted', 'send_text', {'peer': '1', 'text': 'y'})
            rows = {r['id']: dict(r) for r in journal.db.execute('SELECT * FROM jobs')}
            journal.hold(rows['held'], 'destination_inactive')
            journal.set(rows['attempted']['seq'], 'attempted')
        code, body = await self.call('participant/held', 'POST', {'action': 'discard'})
        self.assertEqual((code, body['discarded']), (200, 1))
        with Journal(self.root) as journal:
            self.assertEqual(journal.get(str(ACCOUNT), 'attempted')['state'], 'attempted')
        self.assertEqual((await self.call('participant/held', 'POST', {'action': 'send'}))[0], 409)

    async def test_import_plan_is_saved_but_honestly_unavailable(self):
        code, body = await self.call('participant/import')
        self.assertEqual((body['available'], body['host_supports_import'], body['reason']), (False, False, 'host_support_pending'))
        plan = {'action': 'plan', 'chats': ['-1001'], 'exclude': [], 'from': '2026-09-26T00:00:00+03:00',
                'to': '2026-10-26T00:00:00+03:00', 'max_per_chat': 300}
        code, body = await self.call('participant/import', 'POST', plan)
        self.assertEqual(code, 200, body)
        self.assertIn('Nothing is read from Telegram', body['message'])
        self.assertIn('never starts by itself', body['message'])
        listed = (await self.call('participant/import'))[1]['plans']
        self.assertEqual([(p['state'], p['spec']['chats'], p['spec']['from']) for p in listed],
                         [('saved', [-1001], '2026-09-25T21:00:00+00:00')])
        self.assertEqual((await self.call('participant/import', 'POST', {'action': 'cancel', 'id': listed[0]['id']}))[0], 200)
        self.assertEqual((await self.call('participant/import'))[1]['plans'][0]['state'], 'cancelled')

    async def test_import_plan_validation_refuses_out_of_scope_and_bad_windows(self):
        save_policy(self.root, {'blocked_peers': [-1002]})
        base = {'action': 'plan', 'chats': 'receiving', 'from': '2026-09-26T00:00:00+00:00', 'to': '2026-10-26T00:00:00+00:00'}
        for change, words in (({'chats': ['-1002']}, 'outside the receiving scope'), ({'from': '2026-11-01T00:00:00+00:00'}, 'before'),
                              ({'to': '2026-10-26'}, 'time zone'), ({'max_per_chat': 0}, 'max_per_chat'), ({'chats': []}, 'at least one')):
            code, body = await self.call('participant/import', 'POST', {**base, **change})
            self.assertEqual(code, 400, change)
            self.assertIn(words, body['message'])
        code, body = await self.call('participant/import', 'POST', base)
        self.assertEqual(code, 200)

    async def test_host_advertised_import_still_reports_connector_pending(self):
        _write_json(self.root, 'host-capabilities.json', {'at': '2026-10-26T00:00:00+00:00', 'presence_history_import_version': 1})
        body = (await self.call('participant/import'))[1]
        self.assertEqual((body['available'], body['host_supports_import'], body['reason']), (False, True, 'connector_update_pending'))

    async def test_status_exposes_scope_place_and_profile_without_private_fields(self):
        _write_json(self.root, 'place-state.json', {'state': 'silent', 'label': 'Telegram', 'at': 'now'})
        _write_json(self.root, 'account-profile.json', {'id': str(ACCOUNT), 'username': 'ouro_demo', 'first_name': 'Ouroboros', 'last_name': 'AI'})
        save_policy(self.root, {'scope': 'selected', 'selected_peers': [1, 2], 'blocked_peers': [3]})
        code, body = await self.call('participant/status')
        self.assertEqual(body['scope'], {'mode': 'selected', 'excluded': 1, 'selected': 2})
        self.assertEqual(body['place']['state'], 'silent')
        self.assertEqual(body['profile']['username'], 'ouro_demo')
        self.assertNotIn('phone', json.dumps(body))
        _write_json(self.root, 'account-profile.json', {'id': '1', 'username': 'other'})
        self.assertEqual((await self.call('participant/status'))[1]['profile'], {})

    async def test_status_says_whether_legacy_mode_has_an_enrollment_to_serve(self):
        # The legacy companion waits for a binding and allowed chats; without them it serves nothing.
        self.assertIs((await self.call('participant/status'))[1]['legacy_enrolled'], False)
        save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT, allowed_peer_ids=(-1001,)))
        self.assertIs((await self.call('participant/status'))[1]['legacy_enrolled'], False)
        save_config(self.root, Config(123, 'a' * 32, '+1234567890', account_id=ACCOUNT,
                                      binding_id='b' * 32, allowed_peer_ids=(-1001,)))
        self.assertIs((await self.call('participant/status'))[1]['legacy_enrolled'], True)


if __name__ == '__main__':
    unittest.main()
