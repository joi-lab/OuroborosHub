"""On-demand Slack reads, independent of automatic message intake."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from .provider_context import lookup_error
from .slack_api import SlackApiError, SlackClient, SlackConfigurationError


def _handler(api: Any, kind: str):
    async def read(**params: Any) -> dict[str, Any]:
        try:
            settings = api.get_settings(["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"])
            async with SlackClient(settings.get("SLACK_BOT_TOKEN", ""), settings.get("SLACK_APP_TOKEN", "")) as slack:
                observed_at = datetime.now(timezone.utc).isoformat()
                if kind == "user":
                    data = await slack.user_info(params["user_id"])
                    return {"ok": True, "source": "users.info", "observed_at": observed_at, "user": data}
                if kind == "conversation":
                    data = await slack.conversation_info(params["channel_id"])
                    return {"ok": True, "source": "conversations.info", "observed_at": observed_at, "conversation": data}
                if kind == "thread" and not str(params.get("thread_ts") or "").strip():
                    raise SlackConfigurationError("thread_ts is required for slack_thread")
                return await slack.read_messages(**params)
        except SlackConfigurationError as exc:
            return {"ok": False, "error": {"code": "configuration_or_argument", "message": str(exc)}}
        except (SlackApiError, httpx.HTTPError, TimeoutError) as exc:
            return {"ok": False, "error": lookup_error(exc)}
    return read


def register_read_tools(api: Any) -> None:
    channel = {"type": "string", "description": "Exact Slack conversation ID, including D... for a DM."}
    page = {
        "channel_id": channel,
        "cursor": {"type": "string", "description": "next_cursor from the previous call; keep all other filters unchanged."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50,
                  "description": "Requested page size; Slack may return fewer due to provider limits."},
        "oldest": {"type": "string", "description": "Optional lower Slack timestamp boundary."},
        "latest": {"type": "string", "description": "Optional upper Slack timestamp boundary."},
        "inclusive": {"type": "boolean", "default": False,
                      "description": "Include messages exactly at oldest/latest when those boundaries are specified."},
    }
    descriptors = [
        ("slack_user_info", "user", "Read one Slack user's provider profile by exact ID, including available names, email, title and timezone. These are profile facts, not proof of cross-platform identity or system authority.",
         {"user_id": {"type": "string", "description": "Exact Slack user ID from a message or mention."}}, ["user_id"]),
        ("slack_conversation_info", "conversation", "Read provider metadata for one Slack conversation: name, type flags, topic, purpose and other available fields.",
         {"channel_id": channel}, ["channel_id"]),
        ("slack_history", "history", "Read one page of conversation history without silently trimming message text. Follow next_cursor while complete=false. Author IDs can be resolved with slack_user_info. Retrieval does not submit old messages to Presence.",
         page, ["channel_id"]),
        ("slack_thread", "thread", "Read one page of a Slack thread including the root message. Supply the root timestamp. Token/scopes/membership restrictions return explicit errors, never an invented empty thread. Follow next_cursor while complete=false.",
         {**page, "thread_ts": {"type": "string", "description": "Root Slack message timestamp (thread_ts or the root message's ts)."}},
         ["channel_id", "thread_ts"]),
    ]
    for name, kind, description, properties, required in descriptors:
        api.register_tool(name, _handler(api, kind), description=description,
                          schema={"type": "object", "properties": properties, "required": required,
                                  "additionalProperties": False}, timeout_sec=60)
