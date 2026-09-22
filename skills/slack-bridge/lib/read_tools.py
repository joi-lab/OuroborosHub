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
                if kind == "conversations":
                    return await slack.list_conversations(**params)
                if kind == "users":
                    return await slack.list_users(**params)
                if kind == "lookup_email":
                    data = await slack.lookup_user_by_email(params["email"])
                    return {"ok": True, "source": "users.lookupByEmail", "observed_at": observed_at, "user": data}
                if kind == "members":
                    return await slack.conversation_members(**params)
                if kind == "join":
                    return await slack.join_conversation(params["channel_id"])
                if kind == "resolve":
                    query = str(params.get("query") or "").strip().casefold()
                    if not query:
                        raise SlackConfigurationError("query is required")
                    directory_kind = str(params.get("kind") or "channel")
                    page = await (slack.list_users(**{k: params[k] for k in ("cursor", "limit") if k in params})
                                  if directory_kind == "user" else slack.list_conversations(**{k: params[k] for k in ("cursor", "limit") if k in params}))
                    entries = page.get("members" if directory_kind == "user" else "channels", [])
                    candidates = []
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        profile = entry.get("profile") if isinstance(entry.get("profile"), dict) else {}
                        values = [entry.get("id"), entry.get("name"), entry.get("real_name"),
                                  entry.get("display_name"), entry.get("url"), profile.get("display_name"),
                                  profile.get("real_name"), profile.get("email")]
                        if any(query in str(value or "").casefold() for value in values):
                            candidates.append(entry)
                    return {"ok": True, "source": page.get("source"), "query": query,
                            "candidates": candidates, "next_cursor": page.get("next_cursor"),
                            "complete": page.get("complete", False)}
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
        ("slack_list_conversations", "conversations", "List one paginated Slack conversation directory page. Return exact IDs, provider names, URLs and completeness facts; private conversations the bot cannot see remain provider-invisible.",
         {"cursor": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
          "types": {"type": "array", "items": {"type": "string"}, "description": "Optional Slack conversation types."},
          "exclude_archived": {"type": "boolean", "default": True}}, []),
        ("slack_list_users", "users", "List one paginated Slack user directory page with exact IDs and available names/profile facts.",
         {"cursor": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
          "include_locale": {"type": "boolean", "default": True}}, []),
        ("slack_lookup_user_email", "lookup_email", "Resolve one email through Slack users.lookupByEmail and return the exact user ID/provider object.",
         {"email": {"type": "string"}}, ["email"]),
        ("slack_members", "members", "List one paginated member-ID page for an exact Slack conversation.",
         {"channel_id": channel, "cursor": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100}}, ["channel_id"]),
        ("slack_join", "join", "Explicitly join one public Slack conversation by exact ID. Joining is a provider mutation and is never automatic.",
         {"channel_id": channel}, ["channel_id"]),
        ("slack_resolve", "resolve", "Find exact-ID candidates for a name, email, URL or ID from one paginated Slack directory page. Ambiguous candidates are returned, never guessed.",
         {"query": {"type": "string"}, "kind": {"type": "string", "enum": ["channel", "user"], "default": "channel"},
          "cursor": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100}}, ["query"]),
    ]
    for name, kind, description, properties, required in descriptors:
        api.register_tool(name, _handler(api, kind), description=description,
                          schema={"type": "object", "properties": properties, "required": required,
                                  "additionalProperties": False}, timeout_sec=60)
