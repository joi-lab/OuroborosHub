"""ВкусВилл extension — поиск товаров, детали и корзина через официальный MCP API."""

from __future__ import annotations
from typing import Any

_api = None
_client = None  # module reference


def register(api: Any) -> None:
    global _api, _client
    _api = api

    from . import client as _mod
    _client = _mod

    api.register_tool(
        name="vkusvill_check",
        description="Быстрая проверка подключения к MCP API ВкусВилл.",
        schema={"type": "object", "properties": {}},
        handler=tool_check,
    )
    api.register_tool(
        name="vkusvill_search",
        description=(
            "Поиск товаров ВкусВилл по ключевым словам. "
            "Возвращает список с названиями, ценами, рейтингами и ID."
        ),
        schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос (напр. 'молоко 3.2%', 'хлеб бородинский')",
                },
                "page": {
                    "type": "integer",
                    "description": "Номер страницы (с 1, по умолчанию 1)",
                },
                "sort": {
                    "type": "string",
                    "description": "Сортировка: popularity, rating, price_asc, price_desc, new",
                    "enum": ["popularity", "rating", "price_asc", "price_desc", "new"],
                },
            },
            "required": ["query"],
        },
        handler=tool_search,
    )
    api.register_tool(
        name="vkusvill_product",
        description=(
            "Детальная информация о товаре ВкусВилл по ID: "
            "состав, КБЖУ, цена, рейтинг, условия хранения, срок годности."
        ),
        schema={
            "type": "object",
            "properties": {
                "product_id": {
                    "type": "string",
                    "description": "ID товара (из результатов поиска)",
                },
            },
            "required": ["product_id"],
        },
        handler=tool_product,
    )
    api.register_tool(
        name="vkusvill_cart",
        description=(
            "Создать ссылку на корзину ВкусВилл с выбранными товарами. "
            "Ссылка позволяет быстро добавить товары и оформить заказ."
        ),
        schema={
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Список товаров: [{\"xml_id\": \"...\", \"quantity\": 1}, ...]",
                    "items": {
                        "type": "object",
                        "properties": {
                            "xml_id": {"type": "string", "description": "XML ID товара"},
                            "quantity": {"type": "integer", "description": "Количество (по умолчанию 1)"},
                        },
                        "required": ["xml_id"],
                    },
                },
            },
            "required": ["items"],
        },
        handler=tool_cart,
    )
    api.log("info", "vkusvill: registered 4 tools (stateless MCP, mcp.vkusvill.ru)")


def tool_check(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    try:
        text = _client.check()
        return f"✅ Подключение к MCP API ВкусВилл работает.\n{text[:300]}"
    except Exception as exc:
        return f"❌ MCP API недоступен: {exc}"


def tool_search(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    if args is None:
        args = kwargs
    try:
        query = args.get("query", "")
        if not query:
            return "❌ Ошибка: укажите поисковый запрос (параметр query)"
        page = max(1, int(args.get("page", 1)))
        sort = args.get("sort", "popularity")
        if sort not in ("popularity", "rating", "price_asc", "price_desc", "new"):
            sort = "popularity"
        return _client.search(query=query, page=page, sort=sort)
    except (ValueError, TypeError, RuntimeError, AttributeError) as exc:
        return f"❌ Ошибка: {exc}"


def tool_product(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    if args is None:
        args = kwargs
    try:
        product_id = args.get("product_id", "")
        if not product_id:
            return "❌ Ошибка: укажите product_id"
        return _client.product_details(product_id=str(product_id))
    except (ValueError, TypeError, RuntimeError, AttributeError) as exc:
        return f"❌ Ошибка: {exc}"


def tool_cart(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    if args is None:
        args = kwargs
    try:
        items = args.get("items", [])
        if not items:
            return "❌ Ошибка: укажите массив items с xml_id товаров"
        normalized = []
        for item in items:
            xml_id = str(item.get("xml_id", ""))
            qty = max(1, int(item.get("quantity", 1)))
            if xml_id:
                normalized.append({"xml_id": xml_id, "quantity": qty})
        if not normalized:
            return "❌ Ошибка: ни один товар не содержит корректный xml_id"
        return _client.create_cart(normalized)
    except (ValueError, TypeError, RuntimeError, AttributeError) as exc:
        return f"❌ Ошибка: {exc}"
