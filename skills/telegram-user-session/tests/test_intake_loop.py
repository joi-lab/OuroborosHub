"""Actual companion orchestration with a held Host turn; no Telegram/network effects."""
import asyncio
import contextlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from userbot import participant as module
from userbot.config import Config, save_config


class IntakeLoopTests(unittest.IsolatedAsyncioTestCase):
    async def check_loop(self, fail=False, hold_media=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_config(root, Config(123, 'a' * 32, '+1234567890'))
            started, release, sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
            state = {'calls': 0, 'reports': 0, 'job': False, 'finished': False,
                     'disconnected': False}

            class Host:
                def __init__(self, *args): pass
                def identity(self): pass

            class Client:
                def add_event_handler(self, *args): pass
                async def catch_up(self): pass

            class Adapter:
                client = Client()
                async def connect(self): pass
                async def get_me(self): return 123
                async def disconnect(self): state['disconnected'] = True

            class Engine:
                next_send = 0
                def __init__(self, *args): pass
                def current(self): pass
                async def hydrate_once(self):
                    if hold_media:
                        state['hydrations']=state.get('hydrations',0)+1
                        started.set()
                        try: await release.wait()
                        finally: state['hydration_finished']=True
                async def incoming_once(self):
                    state['calls'] += 1
                    started.set()
                    try:
                        if fail: raise RuntimeError('held intake failed')
                        await release.wait()
                    finally:
                        state['finished'] = True
                async def dispatch_one(self):
                    if state['job']: sent.set()
                async def reports_once(self): state['reports'] += 1

            async def login(*args): return True
            original_sleep = asyncio.sleep
            async def next_tick(_seconds): await original_sleep(0)
            with patch.object(module, 'Host', Host), patch.object(module, 'Participant', Engine), \
                 patch.object(module.asyncio, 'sleep', next_tick):
                task = asyncio.create_task(module.serve(root, 'http://fixture', None,
                    lambda *args: Adapter(), login, lambda root, cfg, account: cfg,
                    lambda *args: None))
                try:
                    await asyncio.wait_for(started.wait(), 3)
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, 'held intake failed'):
                            await asyncio.wait_for(task, 3)
                    else:
                        state['job'] = True
                        await asyncio.wait_for(sent.wait(), 3)
                        self.assertFalse(release.is_set(), 'tool progressed before Host replied')
                        self.assertEqual(state['calls'], 1, 'no duplicate concurrent intake')
                        self.assertGreater(state['reports'], 0)
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                        await task
                self.assertTrue(state['finished'], 'intake cancelled/joined before shutdown')
                self.assertTrue(state['disconnected'])
                if hold_media:
                    self.assertEqual(state['hydrations'],1)
                    self.assertTrue(state['hydration_finished'])
                self.assertFalse([t for t in asyncio.all_tasks()
                    if t is not asyncio.current_task() and t.get_name() in {'telegram-host-intake','telegram-media-hydration'}])

    async def test_tool_and_reports_progress_while_actual_serve_waits_on_host(self):
        await self.check_loop()

    async def test_outbound_progresses_while_media_hydration_is_held(self):
        await self.check_loop(hold_media=True)

    async def test_intake_failure_propagates_and_closes_companion(self):
        await self.check_loop(fail=True)


if __name__ == '__main__': unittest.main()
