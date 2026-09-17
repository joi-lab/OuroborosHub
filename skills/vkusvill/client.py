"""Синхронный клиент для официального MCP API ВкусВилл (zero dependencies).

Официальный эндпоинт (mcp.vkusvill.ru) работает stateless — session-id
не требуется. Достаточно POST JSON-RPC на tools/call.
"""

import json
import urllib.request
import urllib.error
from typing import Any

# Официальный MCP-эндпоинт ВкусВилл (из статьи на Хабре)
MCP_URL = "https://mcp.vkusvill.ru/mcp"
_TIMEOUT = 15  # seconds per HTTP call

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

_next_id = 0


def _make_id() -> int:
    global _next_id
    _next_id += 1
    return _next_id


def _post(payload: dict) -> dict:
    """POST JSON-RPC to the MCP endpoint, return parsed body."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(MCP_URL, data=data, method="POST")
    for k, v in _HEADERS.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            ct = resp.headers.get("Content-Type", "")
            if "text/event-stream" in ct:
                return _parse_sse_json(raw) or {}
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8")[:500]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Сетевая ошибка: {exc.reason}") from exc


def _parse_sse_json(raw: str) -> dict | None:
    """Extract the last JSON object from an SSE stream."""
    last_data: str | None = None
    for line in raw.splitlines():
        if line.startswith("data:"):
            last_data = line[len("data:"):].strip()
    if not last_data:
        return None
    try:
        return json.loads(last_data)
    except json.JSONDecodeError:
        return None


def _call_tool(tool_name: str, arguments: dict[str, Any]) -> dict:
    """Вызвать инструмент MCP API (stateless, без сессии)."""
    body = _post({
        "jsonrpc": "2.0",
        "id": _make_id(),
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    })
    if "error" in body:
        err = body["error"]
        raise RuntimeError(f"MCP ошибка: {err.get('message', err)}")
    return body.get("result", body)


def _extract_text(result: dict) -> str:
    """Извлечь текст из MCP result.content."""
    content = result.get("content", [])
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item["text"])
    return "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------
# Публичный API (модульные функции вместо класса)
# ------------------------------------------------------------------


def check() -> str:
    """Быстрая проверка подключения."""
    result = _call_tool("vkusvill_products_search", {"q": "молоко", "page": 1})
    return _extract_text(result)


def search(query: str, page: int = 1, per_page: int = 10, sort: str = "popular") -> str:
    """Поиск товаров."""
    result = _call_tool("vkusvill_products_search", {
        "q": query,
        "page": page,
        "sort": sort,
    })
    return _extract_text(result)


def product_details(product_id: str) -> str:
    """Детали товара по ID."""
    result = _call_tool("vkusvill_product_details", {
        "id": int(product_id),
    })
    return _extract_text(result)


def create_cart(items: list[dict]) -> str:
    """Создать ссылку на корзину. items: [{"xml_id": "...", "quantity": N}, ...]"""
    # MCP сервер ожидает xml_id как integer, q как float (не quantity!)
    products = []
    for item in items:
        products.append({
            "xml_id": int(item["xml_id"]),
            "q": float(item.get("quantity", 1)),
        })
    result = _call_tool("vkusvill_cart_link_create", {
        "products": products,
    })
    return _extract_text(result)
