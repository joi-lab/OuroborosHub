"""The authenticated bot identity uses the same gate on status and event wire."""
import asyncio
import json

import httpx
import pytest

from lib.host_adapter import create_host_adapter
from lib.presence import slack_identity
from lib.runtime import _discover_reporting
from lib.store import BridgeStore
from test_host_adapter import _item
from test_workers_plugin import _Api, _load_plugin


@pytest.mark.parametrize("advertised", [None, 0, 1])
def test_bot_identity_is_sent_only_when_advertised(tmp_path, monkeypatch, advertised):
    monkeypatch.setenv("HOST_SERVICE_TOKEN", "fixture")
    sent = []
    async def run():
        def handler(request):
            if request.url.path == "/identity":
                return httpx.Response(200, json={"places_version": 1,
                    **({"presence_identity": advertised} if advertised is not None else {})})
            sent.append(json.loads(request.content)["event"])
            return httpx.Response(200, json={"status": "completed", "outcome": "silent"})
        # These are Slack's auth.test field names, not the event sender or app id.
        identity = slack_identity({"user_id": "U-BOT", "user": "ouroboros", "bot_id": "B-APP", "team_id": "T1"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            host = create_host_adapter("", http_client=http, identity=identity)
            await _discover_reporting(host, BridgeStore(tmp_path))
            await host.submit(_item())
        plugin, api = _load_plugin(), _Api(tmp_path)
        plugin.register(api)
        status = json.loads((await api.routes["status"][0]()).body)
        assert "connector_identity" not in status
        if advertised == 1:
            assert status["identity"] == sent[0]["identity"] == {
                "sends_as": {"id": "U-BOT", "label": "ouroboros", "kind": "bot"}}
        else:
            assert "identity" not in status and "identity" not in sent[0]
        assert "authenticated" not in sent[0]["actor"]
    asyncio.run(run())
