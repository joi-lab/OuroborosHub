"""Model-selected Telegram actions using the existing durable outbox."""

from __future__ import annotations

import pathlib
import re
from typing import Any

from .custody import CustodyStore


MODERATION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["delete_message", "restrict_member", "ban_member", "unban_member"],
        },
        "chat_id": {
            "type": "string",
            "description": "Exact Telegram chat ID from the event facts or an explicitly selected target.",
        },
        "message_id": {
            "type": "integer",
            "description": "Exact message ID for delete_message.",
        },
        "user_id": {
            "type": "integer",
            "description": "Exact actor ID for member operations.",
        },
        "permissions": {
            "type": "object",
            "additionalProperties": {"type": "boolean"},
            "description": "Telegram ChatPermissions for restrict_member; true permissions can lift a restriction.",
        },
        "until_date": {
            "type": "integer",
            "description": "Optional Unix timestamp for ban/restrict expiry. Zero means indefinite; Telegram also treats durations below 30 seconds or over 366 days as indefinite.",
        },
        "revoke_messages": {
            "type": "boolean",
            "description": "Delete the banned member's messages; Telegram always does so in supergroups/channels.",
        },
        "request_id": {
            "type": "string",
            "description": "Stable caller ID; reuse for retries of the same operation.",
        },
    },
    "required": ["action", "chat_id", "request_id"],
}


def _positive_id(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    parsed = int(value)
    if parsed <= 0 or str(parsed) != str(value).strip():
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def make_moderation_tool(api: Any):
    def telegram_moderate(
        *,
        action: str,
        chat_id: str,
        request_id: str,
        message_id: int = 0,
        user_id: int = 0,
        permissions: dict[str, bool] | None = None,
        until_date: int = 0,
        revoke_messages: bool = False,
    ) -> dict[str, Any]:
        try:
            target = str(chat_id).strip()
            if re.fullmatch(r"-?[0-9]+", target) is None:
                raise ValueError("chat_id must be an exact numeric Telegram id")
            if not str(request_id).strip():
                raise ValueError("request_id is required for a retryable operation")
            parameters: dict[str, Any] = {"chat_id": target}
            if action == "delete_message":
                parameters["message_id"] = _positive_id(message_id, "message_id")
            elif action in {"restrict_member", "ban_member", "unban_member"}:
                parameters["user_id"] = _positive_id(user_id, "user_id")
                if action == "restrict_member":
                    if (
                        not isinstance(permissions, dict)
                        or not permissions
                        or any(
                            type(value) is not bool for value in permissions.values()
                        )
                    ):
                        raise ValueError(
                            "permissions must be a nonempty Telegram ChatPermissions object of boolean values"
                        )
                    parameters.update(
                        permissions=dict(permissions),
                        use_independent_chat_permissions=True,
                    )
                if action in {"restrict_member", "ban_member"}:
                    if isinstance(until_date, bool) or int(until_date) < 0:
                        raise ValueError(
                            "until_date must be a nonnegative Unix timestamp"
                        )
                    parameters["until_date"] = int(until_date)
                if action == "ban_member":
                    parameters["revoke_messages"] = bool(revoke_messages)
                if action == "unban_member":
                    parameters["only_if_banned"] = True
            else:
                raise ValueError("unsupported Telegram moderation action")
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        receipt = str(request_id).strip()
        inserted = CustodyStore(
            pathlib.Path(api.get_state_dir()) / "custody.sqlite3"
        ).enqueue_outbox(
            f"telegram-moderate:{receipt}",
            {
                "kind": "moderation",
                "chat_id": target,
                "action": action,
                "parameters": parameters,
            },
        )
        return {
            "ok": True,
            "state": "queued" if inserted else "already_queued",
            "request_id": receipt,
            "action": action,
        }

    return telegram_moderate


def make_receipt_tool(api: Any):
    def telegram_receipt(*, request_id: str, operation: str = "send") -> dict[str, Any]:
        if operation not in {"send", "moderate"}:
            return {"ok": False, "error": "operation must be send or moderate"}
        return CustodyStore(
            pathlib.Path(api.get_state_dir()) / "custody.sqlite3"
        ).delivery_receipt(f"telegram-{operation}:{str(request_id).strip()}")

    return telegram_receipt
