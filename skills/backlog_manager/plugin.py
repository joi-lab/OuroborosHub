from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

_STATUS_VALUES = {"open", "in_progress", "deferred", "done", "wont_fix"}
_DEFAULT_STATUS = "open"
_ITEM_RE = re.compile(r"^###\s+(ibl-[A-Za-z0-9_-]+)\s*$")
_FIELD_RE = re.compile(r"^-\s+([a-zA-Z0-9_]+):\s*(.*)\s*$")
_STATE_DIR: Path | None = None
_DATA_DIR: Path | None = None
_API = None
_LAST_WARNING = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _warn(message: str) -> None:
    global _LAST_WARNING
    _LAST_WARNING = message
    try:
        if _API is not None:
            _API.log("warning", message)
    except Exception:
        pass


def _state() -> Path:
    if _STATE_DIR is None:
        raise RuntimeError("backlog_manager not registered")
    return _STATE_DIR


def _data_dir() -> Path:
    if _DATA_DIR is None:
        raise RuntimeError("backlog_manager data_dir unavailable")
    return _DATA_DIR


def _source_path() -> Path:
    return _data_dir() / "memory" / "knowledge" / "improvement-backlog.md"


def _overlay_path() -> Path:
    return _state() / "overlay.json"


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        handle.write(text)
        tmp = Path(handle.name)
    try:
        tmp.replace(path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        finally:
            raise


def _read_overlay() -> Dict[str, Any]:
    path = _overlay_path()
    if not path.exists():
        return {"schema_version": 1, "overrides": {}, "notes": {}, "local_items": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        corrupt = path.with_name(f"{path.name}.corrupt-{int(datetime.now(timezone.utc).timestamp())}.json")
        path.replace(corrupt)
        _warn(f"Backlog Manager overlay was corrupt and was reset; backup={corrupt.name}; error={exc}")
        return {"schema_version": 1, "overrides": {}, "notes": {}, "local_items": []}
    if not isinstance(data, dict):
        return {"schema_version": 1, "overrides": {}, "notes": {}, "local_items": []}
    data.setdefault("schema_version", 1)
    data.setdefault("overrides", {})
    data.setdefault("notes", {})
    data.setdefault("local_items", [])
    return data


def _save_overlay(data: Dict[str, Any]) -> None:
    data["schema_version"] = 1
    data["updated_at"] = _now()
    _atomic_write_json(_overlay_path(), data)


def _parse_source_items() -> List[Dict[str, Any]]:
    path = _source_path()
    if not path.exists():
        return []
    items: List[Dict[str, Any]] = []
    current: Dict[str, Any] | None = None
    body_lines: List[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _ITEM_RE.match(line)
        if match:
            if current:
                current["body"] = "\n".join(body_lines).strip()
                items.append(current)
            current = {"id": match.group(1), "origin": "source"}
            body_lines = []
            continue
        if current is None:
            continue
        body_lines.append(line)
        field_match = _FIELD_RE.match(line)
        if field_match:
            key, value = field_match.group(1), field_match.group(2)
            current[key] = value.strip()
    if current:
        current["body"] = "\n".join(body_lines).strip()
        items.append(current)
    return items


def _normalise_item(raw: Dict[str, Any]) -> Dict[str, Any]:
    item_id = str(raw.get("id") or "").strip()
    status = str(raw.get("status") or _DEFAULT_STATUS).strip() or _DEFAULT_STATUS
    if status not in _STATUS_VALUES:
        status = _DEFAULT_STATUS
    summary = str(raw.get("summary") or "").strip()
    return {
        "id": item_id,
        "status": status,
        "created_at": str(raw.get("created_at") or ""),
        "source": str(raw.get("source") or raw.get("origin") or ""),
        "category": str(raw.get("category") or "uncategorized"),
        "task_id": str(raw.get("task_id") or ""),
        "requires_plan_review": str(raw.get("requires_plan_review") or ""),
        "fingerprint": str(raw.get("fingerprint") or item_id.replace("ibl-", "")),
        "summary": summary,
        "evidence": str(raw.get("evidence") or ""),
        "proposed_next_step": str(raw.get("proposed_next_step") or ""),
        "body": str(raw.get("body") or ""),
        "origin": str(raw.get("origin") or "source"),
    }


def _make_local_id(summary: str) -> str:
    digest = hashlib.sha256((summary + _now()).encode("utf-8")).hexdigest()[:12]
    return f"ibl-local-{digest}"


def _merged_items() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    overlay = _read_overlay()
    overrides = overlay.get("overrides", {}) if isinstance(overlay.get("overrides"), dict) else {}
    notes = overlay.get("notes", {}) if isinstance(overlay.get("notes"), dict) else {}
    local_items = overlay.get("local_items", []) if isinstance(overlay.get("local_items"), list) else []
    merged = [_normalise_item(item) for item in _parse_source_items()]
    merged.extend(_normalise_item({**item, "origin": "local"}) for item in local_items if isinstance(item, dict))
    for item in merged:
        item_override = overrides.get(item["id"], {}) if isinstance(overrides.get(item["id"]), dict) else {}
        if item_override.get("status") in _STATUS_VALUES:
            item["status"] = item_override["status"]
        item_notes = notes.get(item["id"], []) if isinstance(notes.get(item["id"]), list) else []
        item["notes_count"] = len(item_notes)
        item["latest_note"] = str(item_notes[-1].get("text", "")) if item_notes and isinstance(item_notes[-1], dict) else ""
    merged.sort(key=lambda item: (item["status"] != "open", item.get("created_at", "")), reverse=False)
    return merged, overlay


def _stats(items: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"total": len(items), "open": 0, "in_progress": 0, "deferred": 0, "done": 0, "wont_fix": 0}
    for item in items:
        status = item.get("status", _DEFAULT_STATUS)
        if status in counts:
            counts[status] += 1
    return counts


def _source_status() -> Dict[str, Any]:
    path = _source_path()
    return {
        "source_path": str(path),
        "source_exists": path.exists(),
        "warning": _LAST_WARNING,
    }


def _as_response(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    visible = items[:250]
    source = _source_status()
    cards = [
        {
            "id": item["id"],
            "column": item["status"],
            "label": f"{item['id']} · {item['category']} · {item['summary'][:120]}",
        }
        for item in visible
    ]
    source_line = f"Source backlog: `{source['source_path']}` — {'found' if source['source_exists'] else 'missing'}."
    warning_line = f"\n\n⚠️ {source['warning']}" if source.get("warning") else ""
    help_text = (
        "Drag backlog cards between columns to change overlay status. "
        "Use **Update item / add note** with the item id for notes or precise status changes. "
        "New items are local overlay items; source backlog memory is read-only from this skill.\n\n"
        f"{source_line}{warning_line}"
    )
    return {"items": visible, "kanban_cards": cards, "stats": _stats(items), "source": source, "help": help_text}


def _json(payload: Dict[str, Any], status_code: int = 200):
    try:
        from starlette.responses import JSONResponse

        return JSONResponse(payload, status_code=status_code)
    except Exception:
        return payload


async def _route_list(request):
    del request
    items, _overlay = _merged_items()
    return _json(_as_response(items))


async def _request_json(request) -> Tuple[Dict[str, Any] | None, Any]:
    """Safely parse a JSON body. Returns (payload, None) on success or
    (None, error_response) when the body is missing or malformed."""
    try:
        payload = await request.json()
    except Exception:
        return None, _json({"error": "invalid JSON body"}, 400)
    if not isinstance(payload, dict):
        return None, _json({"error": "invalid JSON body"}, 400)
    return payload, None


async def _route_add(request):
    payload, error = await _request_json(request)
    if error is not None:
        return error
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        return _json({"error": "summary is required"}, 400)
    item = {
        "id": _make_local_id(summary),
        "status": _DEFAULT_STATUS,
        "created_at": _now(),
        "source": "widget",
        "category": str(payload.get("category") or "improvement").strip() or "improvement",
        "task_id": "widget",
        "requires_plan_review": "yes" if payload.get("requires_plan_review", True) else "no",
        "fingerprint": "",
        "summary": summary,
        "evidence": str(payload.get("evidence") or "").strip(),
        "proposed_next_step": str(payload.get("proposed_next_step") or "").strip(),
        "origin": "local",
    }
    item["fingerprint"] = item["id"].replace("ibl-local-", "")
    overlay = _read_overlay()
    overlay.setdefault("local_items", []).append(item)
    _save_overlay(overlay)
    items, _ = _merged_items()
    return _json(_as_response(items))


def _find_known_item(items: List[Dict[str, Any]], item_id: str) -> bool:
    return any(item.get("id") == item_id for item in items)


async def _route_update(request):
    payload, error = await _request_json(request)
    if error is not None:
        return error
    item_id = str(payload.get("item_id") or payload.get("card_id") or "").strip()
    if not item_id:
        return _json({"error": "item_id is required"}, 400)
    items, overlay = _merged_items()
    if not _find_known_item(items, item_id):
        return _json({"error": f"unknown backlog item: {item_id}"}, 404)
    status = str(payload.get("status") or "").strip()
    if status:
        if status not in _STATUS_VALUES:
            return _json({"error": f"invalid status: {status}"}, 400)
        overlay.setdefault("overrides", {}).setdefault(item_id, {})["status"] = status
    note = str(payload.get("note") or "").strip()
    if note:
        overlay.setdefault("notes", {}).setdefault(item_id, []).append({"ts": _now(), "text": note})
    _save_overlay(overlay)
    items, _ = _merged_items()
    return _json(_as_response(items))


async def _route_move(request):
    payload, error = await _request_json(request)
    if error is not None:
        return error
    item_id = str(payload.get("card_id") or "").strip()
    status = str(payload.get("column_id") or "").strip()
    if not item_id or status not in _STATUS_VALUES:
        return _json({"error": "card_id and valid column_id are required"}, 400)
    items, overlay = _merged_items()
    if not _find_known_item(items, item_id):
        return _json({"error": f"unknown backlog item: {item_id}"}, 404)
    overlay.setdefault("overrides", {}).setdefault(item_id, {})["status"] = status
    _save_overlay(overlay)
    items, _ = _merged_items()
    return _json(_as_response(items))


def _tool_summary(ctx=None) -> str:
    del ctx
    items, _ = _merged_items()
    stats = _stats(items)
    return (
        f"Backlog Manager: {stats['total']} visible items — "
        f"open={stats['open']}, in_progress={stats['in_progress']}, "
        f"deferred={stats['deferred']}, done={stats['done']}, wont_fix={stats['wont_fix']}."
    )


def register(api):
    global _API, _STATE_DIR, _DATA_DIR
    _API = api
    _STATE_DIR = Path(api.get_state_dir()).resolve()
    info = api.get_runtime_info()
    _DATA_DIR = Path(info.get("data_dir") or _STATE_DIR.parents[2]).resolve()
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not _DATA_DIR.exists():
        _warn(f"Backlog Manager data_dir does not exist: {_DATA_DIR}")
    elif not _source_path().exists():
        _warn(f"Backlog Manager source backlog not found: {_source_path()}")

    api.register_route("list", _route_list, methods=("GET",))
    api.register_route("add", _route_add, methods=("POST",))
    api.register_route("update", _route_update, methods=("POST",))
    api.register_route("move", _route_move, methods=("POST",))
    api.register_tool(
        "summary",
        handler=_tool_summary,
        description="Return compact Improvement Backlog status counts from the Backlog Manager overlay.",
        schema={"type": "object", "properties": {}},
        timeout_sec=10,
    )
    api.register_ui_tab(
        "backlog",
        title="Backlog Manager",
        render={
            "kind": "declarative",
            "schema_version": 1,
            "span": 2,
            "components": [
                {
                    "type": "poll",
                    "target": "backlog",
                    "route": "list",
                    "method": "GET",
                    "auto_start": True,
                    "interval_ms": 30000,
                    "max_ticks": 1000,
                    "label": "Refresh backlog",
                },
                {
                    "type": "kanban",
                    "target": "backlog",
                    "path": "kanban_cards",
                    "columns": [
                        {"id": "open", "label": "Open"},
                        {"id": "in_progress", "label": "In progress"},
                        {"id": "deferred", "label": "Deferred"},
                        {"id": "done", "label": "Done"},
                        {"id": "wont_fix", "label": "Wont fix"},
                    ],
                    "on_move": {"route": "move", "method": "POST"},
                },
                {
                    "type": "kv",
                    "target": "backlog",
                    "fields": [
                        {"label": "Total", "path": "stats.total"},
                        {"label": "Open", "path": "stats.open"},
                        {"label": "In progress", "path": "stats.in_progress"},
                        {"label": "Deferred", "path": "stats.deferred"},
                        {"label": "Done", "path": "stats.done"},
                        {"label": "Wont fix", "path": "stats.wont_fix"},
                    ],
                },
                {
                    "type": "form",
                    "route": "add",
                    "method": "POST",
                    "target": "backlog",
                    "label": "Add backlog item",
                    "fields": [
                        {"name": "summary", "label": "Summary", "required": True},
                        {"name": "category", "label": "Category"},
                        {"name": "proposed_next_step", "label": "Next step", "type": "textarea"},
                        {"name": "evidence", "label": "Evidence", "type": "textarea"},
                        {"name": "requires_plan_review", "label": "Requires plan review", "type": "checkbox"},
                    ],
                },
                {
                    "type": "form",
                    "route": "update",
                    "method": "POST",
                    "target": "backlog",
                    "label": "Update item / add note",
                    "fields": [
                        {"name": "item_id", "label": "Item id", "required": True},
                        {
                            "name": "status",
                            "label": "Status",
                            "type": "select",
                            "options": [
                                {"value": "", "label": "(no change)"},
                                {"value": "open", "label": "Open"},
                                {"value": "in_progress", "label": "In progress"},
                                {"value": "deferred", "label": "Deferred"},
                                {"value": "done", "label": "Done"},
                                {"value": "wont_fix", "label": "Wont fix"},
                            ],
                        },
                        {"name": "note", "label": "Note", "type": "textarea"},
                    ],
                },
            ],
        },
    )
