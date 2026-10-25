"""The status route offers Connect rows only for an account it can name."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gh_presence.config import connection_account, validate, write_config

REPOSITORIES = [{"id": 303, "full_name": "example/project"}]


class RecordingAPI:
    def __init__(self, state_dir):
        self.state_dir, self.routes = state_dir, {}

    def get_runtime_info(self):
        return {"state_dir": str(self.state_dir)}

    def register_route(self, path, handler, methods):
        self.routes[path] = handler

    def __getattr__(self, name):
        # Tools, settings, the tab and the companion are not under test here.
        return lambda *args, **kwargs: None


class StatusRouteTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="github-status-")
        self.addCleanup(scratch.cleanup)
        self.state = Path(scratch.name)
        spec = importlib.util.spec_from_file_location("github_status_plugin", ROOT / "plugin.py")
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        self.api = RecordingAPI(self.state)
        plugin.register(self.api)

    def status(self, **config):
        write_config(self.state, config)
        response = asyncio.run(self.api.routes["status"](SimpleNamespace(query_params={})))
        return json.loads(response.body)

    def test_rows_wait_for_the_writer_and_never_carry_a_null_account(self):
        unknown = self.status(repositories=REPOSITORIES, notifications_user_id=202)
        self.assertIsNone(unknown["account_id"])
        self.assertEqual(unknown["connect"], [])

    def test_known_writer_offers_each_repository_and_account_wide_notifications(self):
        known = self.status(repositories=REPOSITORIES, notifications_user_id=202, expected_writer_id=101)
        account = connection_account(validate({"expected_writer_id": 101}))
        self.assertTrue(isinstance(account, str) and account)
        self.assertEqual(known["connect"], [
            {"account_id": account, "conversation_id": "repo.303", "label": "example/project"},
            {"account_id": account, "conversation_id": "", "label": "Account-wide notifications"}])
        without_notifications = self.status(repositories=REPOSITORIES, expected_writer_id=101)
        self.assertEqual([row["conversation_id"] for row in without_notifications["connect"]], ["repo.303"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
