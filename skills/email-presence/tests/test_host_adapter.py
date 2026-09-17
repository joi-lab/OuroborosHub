from lib.host_adapter import email_presence_event
from lib.store import InboxItem


def test_event_is_provider_neutral():
    item=InboxItem(1,"lease","INBOX",4,"<m>","",("<root>",),"a@example.com","Subject","Text",("bot@example.com",),"",1)
    event=email_presence_event(item, account_id="bot@example.com")
    assert event["provider"]=="email"; assert event["source_event_id"]=="INBOX:0:4:<m>"; assert event["message"]["references"]==["<root>"]; assert "prompt" not in event

    assert "subject" not in event
    assert event["message"]["subject"] == "Subject"


def test_host_submission_matches_exact_event_contract_and_silence_has_no_duplicate(monkeypatch):
    import asyncio
    import httpx
    from lib.host_adapter import LoopbackPresenceHostAdapter
    monkeypatch.setenv("EMAIL_USER", "bot@example.com")
    item = InboxItem(1, "lease", "INBOX", 4, "<m>", "", (), "a@example.com", "Subject", "Text", ("bot@example.com",), "", 1)
    async def exercise():
        def handler(request):
            import json
            payload = json.loads(request.content)
            assert set(payload) == {"binding_id", "event"}
            assert set(payload["event"]) == {"source_event_id", "provider", "account_id", "conversation_id", "thread_id", "conversation_key", "actor", "conversation", "message", "text"}
            assert payload["event"]["account_id"] == "bot@example.com"
            return httpx.Response(200, json={"status": "completed", "outcome": "tool_delivered", "text": "Already sent using email_send"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            host = LoopbackPresenceHostAdapter("a" * 32, host_service_url="http://127.0.0.1:8767", skill_token="test", http_client=http)
            ref = await host.submit(item)
            assert (await host.deliver(ref)).texts == ()
    asyncio.run(exercise())
