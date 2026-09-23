"""The host tool ABI is ``str``; every tool here is registered through it.

``PluginAPI.register_tool`` declares handlers as ``Callable[..., str]`` and the
dispatcher falls back to ``str(result)`` for anything else, so a handler that
returns a ``dict`` reaches the model as a Python repr (single quotes, ``True``,
``None``) instead of JSON. Serializing once, at the registration boundary, keeps
every tool's result valid JSON without rewriting each handler body.
"""

from __future__ import annotations

import functools
import inspect
import json
from typing import Any, Callable


def to_tool_json(value: Any) -> str:
    """Serialize one tool result as compact, valid JSON text."""

    return json.dumps(value, ensure_ascii=False, default=str)


def json_tool(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Return ``handler`` with its result serialized as JSON text.

    ``functools.wraps`` keeps the original signature visible to
    ``inspect.signature``, which the host reads at registration time to decide
    whether a handler takes a leading ``ctx`` argument; an unwrapped
    ``(*args, **kwargs)`` wrapper would always be called ctx-first. Async
    handlers stay async so the host still awaits them under its own timeout.
    """

    if inspect.iscoroutinefunction(handler):

        @functools.wraps(handler)
        async def async_json_handler(*args: Any, **kwargs: Any) -> str:
            return to_tool_json(await handler(*args, **kwargs))

        return async_json_handler

    @functools.wraps(handler)
    def json_handler(*args: Any, **kwargs: Any) -> str:
        return to_tool_json(handler(*args, **kwargs))

    return json_handler


def register_json_tool(api: Any, name: str, handler: Callable[..., Any], **metadata: Any) -> None:
    """Register one tool whose handler result is serialized as JSON text."""

    api.register_tool(name, json_tool(handler), **metadata)
