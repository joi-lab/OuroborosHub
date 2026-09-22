"""Exact provider-send observations for the existing outbox."""

from pathlib import Path


def tool_origin(ctx):
    origin = {"kind": "tool"}
    if getattr(ctx, "task_id", None):
        origin["task_id"] = str(ctx.task_id)
    metadata = getattr(ctx, "task_metadata", None) or {}
    event = (metadata.get("presence") or {}).get("event") or {}
    if event.get("source_event_id"):
        origin["source_event_id"] = str(event["source_event_id"])
    return origin


def delivery_report(delivery_id, payload, *, part_id, state, receipt=None, text="", fmt="", error=""):
    reporting = payload.get("_reporting") or {}
    if reporting.get("version") != 1:
        return None
    receipt = receipt or {}
    wire = receipt.get("_delivery") or {}
    message = {"kind": payload.get("kind", "message")}
    for key in ("message_id", "date", "entities", "caption_entities", "photo", "document"):
        if key in receipt:
            message[key] = receipt[key]
    if payload.get("file_path"):
        message["file_name"] = Path(payload["file_path"]).name
    if payload.get("reply_to_message_id"):
        message["reply_to_message_id"] = payload["reply_to_message_id"]
    if payload.get("kind") == "operation":
        message["method"] = str(payload.get("method") or "")
        message["target_message_id"] = (payload.get("parameters") or {}).get("message_id")
        if payload.get("method") == "editMessageText":
            message["replacement_text"] = str(payload.get("text") or "")
        if payload.get("method") == "setMessageReaction":
            message["reaction"] = (payload.get("parameters") or {}).get("reaction", [])
        if payload.get("original_delivery_id"):
            message["original_delivery_id"] = str(payload["original_delivery_id"])
    if error:
        message.update(error=error, confirmed_parts=len(payload.get("_sent_messages") or []))
    return {
        "schema_version": 1, "delivery_id": delivery_id, "part_id": str(part_id),
        "state": state, "provider": "telegram", "account_id": str(reporting.get("account_id") or ""),
        "conversation_id": str(payload["chat_id"]), "thread_id": str(payload.get("topic_id") or ""),
        "text": "" if payload.get("kind") == "operation" else wire.get("text", text),
        "format": "" if payload.get("kind") == "operation" else wire.get("format", fmt),
        "message": message, "origin": reporting.get("origin") or {"kind": "automatic"},
    }
