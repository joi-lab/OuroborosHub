"""GDELT Cloud MCP extension — geopolitical intelligence platform.

Connects to the GDELT Cloud MCP server (Streamable HTTP) and exposes
Progressive Discovery tools for GDELT Events/Stories/Entities/Energy,
macro-finance data, prediction markets, and web research.

Requires: GDELT_API_KEY (get from gdeltcloud.com/dashboard → API Keys).
Zero external dependencies — uses only Python stdlib.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse


# ── Constants ─────────────────────────────────────────────────────────────

_MCP_URL = "https://gdelt-cloud-mcp.fastmcp.app/mcp"
_MCP_CLIENT_INFO = {"name": "ouroboros-gdelt-mcp", "version": "0.1.0"}
_MCP_PROTOCOL_VERSION = "2025-03-26"
_TIMEOUT_SEC = 120
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # 8 MB

# Known nested tool → category mapping for auto-routing
_TOOL_CATEGORY_MAP: Dict[str, str] = {
    # GDELT Cloud v2
    "search_events": "gdelt_cloud",
    "summarize_events": "gdelt_cloud",
    "get_event": "gdelt_cloud",
    "search_stories": "gdelt_cloud",
    "summarize_stories": "gdelt_cloud",
    "get_story": "gdelt_cloud",
    "get_story_articles": "gdelt_cloud",
    "search_entities": "gdelt_cloud",
    "get_entity": "gdelt_cloud",
    "list_admin1": "gdelt_cloud",
    "energy_search_assets": "gdelt_cloud",
    "energy_summarize_assets": "gdelt_cloud",
    "energy_get_asset": "gdelt_cloud",
    "energy_search_owners": "gdelt_cloud",
    "energy_get_owner": "gdelt_cloud",
    "energy_assets_by_owner": "gdelt_cloud",
    # Prediction Markets
    "SEARCH_RELEVANT_MARKETS": "prediction_market",
    "SEARCH_EVENTS": "prediction_market",
    "SEARCH_MARKETS": "prediction_market",
    "GET_MARKET": "prediction_market",
    # Web Research
    "SEARCH_WEB": "web_research",
    "EXTRACT_WEB_PAGES": "web_research",
}

_CATEGORY_LABELS: Dict[str, str] = {
    "gdelt_cloud": "GDELT Cloud (Events, Stories, Entities, Energy)",
    "macro_finance": "Macro Finance (quotes, FX, commodities, rates)",
    "prediction_market": "Prediction Markets (Kalshi contracts)",
    "web_research": "Web Research (search, page extraction)",
}

_plugin_api_ref: Any = None
_id_counter: int = 0


# ── Minimal MCP Streamable HTTP Client ────────────────────────────────────

class _MCPError(Exception):
    """Raised on MCP communication errors."""


def _next_id() -> int:
    global _id_counter
    _id_counter += 1
    return _id_counter


def _parse_sse(raw: str) -> Optional[dict]:
    """Parse an SSE stream. Return the last valid JSON-RPC message."""
    result = None
    for line in raw.split("\n"):
        stripped = line.strip()
        if stripped.startswith("data:"):
            payload = stripped[5:].strip()
            if payload:
                try:
                    result = json.loads(payload)
                except ValueError:
                    pass
    return result


def _mcp_post(
    api_key: str,
    body: dict,
    session_id: Optional[str] = None,
) -> tuple:
    """Send a JSON-RPC 2.0 message to the GDELT MCP endpoint.

    Returns (parsed_response_or_None, session_id).
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {api_key}",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(_MCP_URL, data=data, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
            new_session = resp.headers.get("Mcp-Session-Id") or session_id
            raw = resp.read(_MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
            content_type = resp.headers.get("Content-Type", "")

            if "text/event-stream" in content_type:
                return _parse_sse(raw), new_session
            if raw.strip():
                try:
                    return json.loads(raw), new_session
                except ValueError:
                    raise _MCPError(
                        f"Non-JSON response (Content-Type: {content_type}): "
                        f"{raw[:200]}"
                    )
            return None, new_session
    except urllib.error.HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            pass
        raise _MCPError(f"HTTP {exc.code}: {err_body or exc.reason}")
    except urllib.error.URLError as exc:
        raise _MCPError(f"Network error: {exc.reason!r}")
    except TimeoutError:
        raise _MCPError(f"Timed out after {_TIMEOUT_SEC}s")


def _extract_tool_result(resp: Optional[dict]) -> Any:
    """Extract usable data from a JSON-RPC tools/call response.

    MCP responses come as: {"result": {"content": [{"type":"text","text":"..."}]}}
    The inner text is often a JSON string itself.
    """
    if resp is None:
        return {"error": "Empty response from MCP server"}

    if "error" in resp:
        err = resp["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        return {"error": f"MCP error: {msg}"}

    result = resp.get("result", resp)

    # Unwrap MCP content array
    if isinstance(result, dict) and "content" in result:
        contents = result["content"]
        if isinstance(contents, list):
            texts = []
            for item in contents:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_val = item.get("text", "")
                    try:
                        texts.append(json.loads(text_val))
                    except (ValueError, TypeError):
                        texts.append(text_val)
            if len(texts) == 1:
                return texts[0]
            return {"results": texts} if texts else result

    return result


def _mcp_call_tool(api_key: str, tool_name: str, arguments: Optional[dict] = None) -> Any:
    """Full MCP handshake + tools/call. Stateless per invocation."""
    # Step 1: Initialize session
    init_resp, session_id = _mcp_post(api_key, {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "clientInfo": _MCP_CLIENT_INFO,
            "capabilities": {},
        },
        "id": _next_id(),
    })

    # Step 2: Send initialized notification (best-effort)
    try:
        _mcp_post(api_key, {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }, session_id=session_id)
    except _MCPError:
        pass  # Notifications may return 202 or fail silently

    # Step 3: Call the tool
    resp, _ = _mcp_post(api_key, {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments or {},
        },
        "id": _next_id(),
    }, session_id=session_id)

    return _extract_tool_result(resp)


# ── Helpers ───────────────────────────────────────────────────────────────

def _get_api_key() -> str:
    """Retrieve the GDELT API key through the PluginAPI grant path."""
    if _plugin_api_ref is not None:
        settings = _plugin_api_ref.get_settings(["GDELT_API_KEY"])
        return settings.get("GDELT_API_KEY", "")
    return ""


def _resolve_category(tool_name: str) -> str:
    """Resolve which wrapper category a nested tool belongs to."""
    if tool_name in _TOOL_CATEGORY_MAP:
        return _TOOL_CATEGORY_MAP[tool_name]
    # Heuristic: if starts with energy_, it's gdelt_cloud
    if tool_name.startswith("energy_"):
        return "gdelt_cloud"
    # Uppercase names are typically external
    if tool_name.isupper():
        return "web_research"
    return "gdelt_cloud"


def _key_check() -> Optional[str]:
    """Return an error string if the API key is missing, else None."""
    if not _get_api_key():
        return (
            "GDELT_API_KEY not configured. Setup:\n"
            "1. Sign up at gdeltcloud.com\n"
            "2. Dashboard → API Keys → Create New Key\n"
            "3. Add key in Ouroboros: Settings → Secrets → GDELT_API_KEY\n"
            "4. Grant it to this skill"
        )
    return None


# ── Agent-callable tools ──────────────────────────────────────────────────

def _tool_discover(*, category: str = "all") -> str:
    """List available GDELT tools across categories."""
    err = _key_check()
    if err:
        return json.dumps({"error": err}, ensure_ascii=False)

    api_key = _get_api_key()
    cats = (
        list(_CATEGORY_LABELS.keys())
        if category == "all"
        else [category]
    )

    catalog: Dict[str, Any] = {}
    for cat in cats:
        try:
            result = _mcp_call_tool(api_key, f"{cat}_tool_list")
            catalog[cat] = {
                "label": _CATEGORY_LABELS.get(cat, cat),
                "tools": result,
            }
        except _MCPError as e:
            catalog[cat] = {"label": _CATEGORY_LABELS.get(cat, cat), "error": str(e)}

    return json.dumps(catalog, ensure_ascii=False, indent=2)


def _tool_inspect(*, tool_name: str, category: str = "") -> str:
    """Get exact schema for a GDELT tool."""
    err = _key_check()
    if err:
        return json.dumps({"error": err}, ensure_ascii=False)

    api_key = _get_api_key()
    cat = category if category in _CATEGORY_LABELS else _resolve_category(tool_name)

    try:
        result = _mcp_call_tool(api_key, f"{cat}_tool_get", {"tool_name": tool_name})
        return json.dumps(result, ensure_ascii=False, indent=2)
    except _MCPError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _tool_query(*, tool_name: str, arguments: str = "{}", category: str = "") -> str:
    """Execute any GDELT tool with JSON arguments."""
    err = _key_check()
    if err:
        return json.dumps({"error": err}, ensure_ascii=False)

    # Parse arguments
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(args, dict):
            args = {}
    except (ValueError, TypeError):
        return json.dumps({"error": f"Invalid JSON in arguments: {arguments!r}"}, ensure_ascii=False)

    api_key = _get_api_key()
    cat = category if category in _CATEGORY_LABELS else _resolve_category(tool_name)

    try:
        result = _mcp_call_tool(api_key, f"{cat}_tool_call", {
            "tool_name": tool_name,
            "tool_arguments": args,
        })
        return json.dumps(result, ensure_ascii=False, indent=2)
    except _MCPError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ── Route handlers ────────────────────────────────────────────────────────

async def _route_query(request: Request) -> JSONResponse:
    """POST /api/extensions/gdelt-mcp/query

    Body: {"tool_name": "search_events", "arguments": {"category": "Battles", ...}}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    tool_name = str(body.get("tool_name", "")).strip()
    raw_args = body.get("arguments", {})
    explicit_cat = str(body.get("category", "")).strip()

    if not tool_name:
        return JSONResponse({"error": "tool_name is required"}, status_code=400)

    err = _key_check()
    if err:
        return JSONResponse({"error": err}, status_code=502)

    # Parse arguments — accept both dict and JSON string
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except ValueError:
            return JSONResponse({"error": "Invalid JSON arguments"}, status_code=400)
    else:
        args = raw_args if isinstance(raw_args, dict) else {}

    api_key = _get_api_key()
    cat = (
        explicit_cat
        if explicit_cat in _CATEGORY_LABELS
        else _resolve_category(tool_name)
    )

    try:
        result = await asyncio.to_thread(
            _mcp_call_tool, api_key, f"{cat}_tool_call",
            {"tool_name": tool_name, "tool_arguments": args},
        )
        status = 200 if not (isinstance(result, dict) and "error" in result) else 502
        return JSONResponse(result, status_code=status)
    except _MCPError as e:
        return JSONResponse({"error": str(e)}, status_code=502)


async def _route_discover(request: Request) -> JSONResponse:
    """GET /api/extensions/gdelt-mcp/discover"""
    err = _key_check()
    if err:
        return JSONResponse({"error": err}, status_code=502)

    try:
        raw = await asyncio.to_thread(_tool_discover)
        return JSONResponse(json.loads(raw))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


# ── Plugin entry point ────────────────────────────────────────────────────

def register(api: Any) -> None:
    """PluginAPI v1 entry point."""
    global _plugin_api_ref
    _plugin_api_ref = api

    # Tool 1: Discover catalog
    api.register_tool(
        "discover",
        _tool_discover,
        description=(
            "List available GDELT Cloud MCP tools. Returns the Progressive "
            "Discovery catalog covering: GDELT Cloud v2 (Events, Stories, "
            "Entities, Energy infrastructure), Macro Finance, Prediction "
            "Markets, and Web Research. Call this first to see what tools exist."
        ),
        schema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": (
                        "Filter by category. Default: all."
                    ),
                    "enum": [
                        "all", "gdelt_cloud", "macro_finance",
                        "prediction_market", "web_research",
                    ],
                },
            },
        },
        timeout_sec=60,
    )

    # Tool 2: Inspect tool schema
    api.register_tool(
        "inspect",
        _tool_inspect,
        description=(
            "Get exact schema, parameter descriptions, enum values, and usage "
            "guidance for a specific GDELT tool. Call before first use. "
            "Examples: search_events, search_stories, search_entities, "
            "summarize_events, energy_search_assets, SEARCH_WEB, "
            "SEARCH_RELEVANT_MARKETS. For macro_finance tools, pass "
            "category='macro_finance' explicitly since their names are dynamic."
        ),
        schema={
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": (
                        "Nested tool name to inspect, e.g. search_events, "
                        "summarize_stories, SEARCH_WEB."
                    ),
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Explicit category override for routing. Required for "
                        "macro_finance tools (names are dynamic). Auto-detected "
                        "for gdelt_cloud, prediction_market, web_research."
                    ),
                    "enum": [
                        "gdelt_cloud", "macro_finance",
                        "prediction_market", "web_research",
                    ],
                },
            },
            "required": ["tool_name"],
        },
        timeout_sec=60,
    )

    # Tool 3: Execute a query
    api.register_tool(
        "query",
        _tool_query,
        description=(
            "Execute any GDELT tool by name with arguments. Auto-routes to "
            "the correct category (GDELT Cloud, Macro Finance, Prediction "
            "Markets, Web Research). For macro_finance tools, pass "
            "category='macro_finance' explicitly. "
            "GDELT Cloud: search_events, summarize_events, get_event, "
            "search_stories, summarize_stories, get_story, get_story_articles, "
            "search_entities, get_entity, list_admin1, energy_search_assets, "
            "energy_summarize_assets, energy_get_asset, energy_search_owners, "
            "energy_get_owner, energy_assets_by_owner. "
            "Prediction Markets: SEARCH_RELEVANT_MARKETS, SEARCH_MARKETS, "
            "GET_MARKET. Web Research: SEARCH_WEB, EXTRACT_WEB_PAGES."
        ),
        schema={
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "Name of the tool to execute.",
                },
                "arguments": {
                    "type": "string",
                    "description": (
                        "Tool arguments as a JSON string. Example: "
                        "'{\"category\":\"Battles\",\"country\":\"Lebanon\","
                        "\"has_fatalities\":true,\"limit\":5}'"
                    ),
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Explicit category override. Required for macro_finance "
                        "tools. Auto-detected for other categories."
                    ),
                    "enum": [
                        "gdelt_cloud", "macro_finance",
                        "prediction_market", "web_research",
                    ],
                },
            },
            "required": ["tool_name"],
        },
        timeout_sec=120,
    )

    # HTTP routes
    api.register_route("query", _route_query, methods=("POST",))
    api.register_route("discover", _route_discover, methods=("GET",))

    # Declarative widget
    api.register_ui_tab(
        "gdelt",
        "GDELT Intelligence",
        icon="globe",
        render={
            "kind": "declarative",
            "schema_version": 1,
            "components": [
                {
                    "type": "form",
                    "route": "query",
                    "method": "POST",
                    "target": "gdelt_results",
                    "fields": [
                        {
                            "name": "tool_name",
                            "label": "Tool name",
                            "type": "text",
                            "placeholder": "search_events",
                            "required": True,
                        },
                        {
                            "name": "arguments",
                            "label": "Arguments (JSON)",
                            "type": "text",
                            "placeholder": (
                                '{"category":"Battles","country":"Lebanon","limit":5}'
                            ),
                        },
                    ],
                    "submit_label": "Execute",
                },
                {
                    "type": "json",
                    "target": "gdelt_results",
                },
            ],
        },
    )

    def _cleanup() -> None:
        global _plugin_api_ref
        _plugin_api_ref = None

    api.on_unload(_cleanup)
    api.log("info", "gdelt-mcp: registered (3 tools, 2 routes, 1 widget)")


__all__ = ["register"]
