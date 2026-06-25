"""Sber API client-info extension — organization and accounts via direct REST API.

Exposes client info tool that calls GET /fintech/api/v1/client-info documented at:
https://developers.sber.ru/docs/ru/sber-api/specifications/client-info/get-client-info
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

_SKILL_ROOT = Path(__file__).resolve().parent
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from starlette.requests import Request
from starlette.responses import JSONResponse

from lib.api_client import as_json, client_info_url, get_client_info
from lib.tls_store import ensure_p12_tls, install_p12_from_path, tls_is_installed

_SETTINGS_KEYS = (
    "SBER_ACCESS_TOKEN",
    "SBER_TLS_P12_PASSWORD",
    "SBER_TLS_CERT_PATH",
    "SBER_TLS_KEY_PATH",
    "SBER_API_ENV",
    "SBER_API_INSECURE",
)

_UI_RENDER = {
    "kind": "declarative",
    "schema_version": 1,
    "components": [
        {
            "type": "form",
            "route": "info",
            "method": "POST",
            "target": "info_result",
            "fields": [
                {
                    "name": "api_env",
                    "label": "Контур API",
                    "type": "text",
                    "placeholder": "prod или пусто для теста",
                    "required": False,
                },
            ],
            "submit_label": "Получить информацию о клиенте",
        },
        {
            "type": "json",
            "target": "info_result",
        },
    ],
}


def _read_settings(api: Any) -> Dict[str, str]:
    try:
        raw = api.get_settings(list(_SETTINGS_KEYS)) or {}
    except Exception:
        raw = {}
    return {key: str(raw.get(key) or "").strip() for key in _SETTINGS_KEYS}


def _state_dir(api: Any) -> str:
    try:
        raw = api.get_state_dir()
        return str(raw or "").strip()
    except Exception:
        return ""


def _apply_insecure_flag(settings: Dict[str, str]) -> None:
    import os

    insecure = settings.get("SBER_API_INSECURE", "").lower()
    if insecure in {"1", "true", "yes"}:
        os.environ["SBER_API_INSECURE"] = "1"
    elif "SBER_API_INSECURE" in os.environ:
        del os.environ["SBER_API_INSECURE"]


def _resolve_tls_paths(api: Any, settings: Dict[str, str]) -> Tuple[str, str]:
    cert_path = settings.get("SBER_TLS_CERT_PATH", "")
    key_path = settings.get("SBER_TLS_KEY_PATH", "")
    if cert_path and key_path:
        return cert_path, key_path

    p12_password = settings.get("SBER_TLS_P12_PASSWORD", "")
    if not p12_password:
        return "", ""

    state_dir = _state_dir(api)
    if not state_dir:
        return "", ""
    try:
        return ensure_p12_tls(state_dir, p12_password)
    except Exception as exc:
        api.log("warning", f"sber_client_info: P12 TLS conversion failed: {exc}")
        return "", ""


def _runtime_config(api: Any, settings_override: Dict[str, str] | None = None) -> Dict[str, str]:
    settings = {**_read_settings(api), **(settings_override or {})}
    _apply_insecure_flag(settings)
    cert_path, key_path = _resolve_tls_paths(api, settings)
    env = settings.get("SBER_API_ENV", "")
    return {
        "url": client_info_url(env),
        "access_token": settings.get("SBER_ACCESS_TOKEN", ""),
        "cert_path": cert_path,
        "key_path": key_path,
    }


def _missing_token_payload() -> Dict[str, str]:
    return {
        "error": (
            "SBER_ACCESS_TOKEN is not granted. Add the token in Ouroboros Settings "
            "and approve the skill grant after review PASS."
        )
    }


def _missing_tls_payload(state_dir: str) -> Dict[str, str]:
    if state_dir and tls_is_installed(state_dir):
        return {
            "error": (
                "TLS-сертификат установлен, но SBER_TLS_P12_PASSWORD не задан или "
                "пароль неверный. Проверьте секрет в Settings и grant."
            )
        }
    return {
        "error": (
            "TLS не настроен. Прикрепите .p12/.pfx в чат и попросите агента вызвать "
            "install_tls_certificate(source_path=...), предварительно задав "
            "SBER_TLS_P12_PASSWORD в Settings. Альтернатива: готовые PEM через "
            "SBER_TLS_CERT_PATH и SBER_TLS_KEY_PATH."
        )
    }


def _invoke(
    api: Any,
    fn: Callable[..., Dict[str, Any]],
    *,
    settings_override: Dict[str, str] | None = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    cfg = _runtime_config(api, settings_override)
    if not cfg["access_token"]:
        return _missing_token_payload()
    if not cfg["cert_path"] or not cfg["key_path"]:
        return _missing_tls_payload(_state_dir(api))
    return fn(
        url=cfg["url"],
        access_token=cfg["access_token"],
        cert_path=cfg["cert_path"],
        key_path=cfg["key_path"],
        **kwargs,
    )


def _make_tool_handler(api: Any, fn: Callable[..., Dict[str, Any]]):
    def _handler() -> str:
        return as_json(_invoke(api, fn))

    return _handler


def register(api: Any) -> None:
    """PluginAPI v1 entry point."""

    async def _route_info(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            body = {}

        api_env = str(body.get("api_env", "")).strip()
        override = {"SBER_API_ENV": api_env} if api_env else None
        payload = await asyncio.to_thread(
            _invoke,
            api,
            get_client_info,
            settings_override=override,
        )
        status = 200 if "error" not in payload else 502
        return JSONResponse(payload, status_code=status)

    def _install_tls(source_path: str) -> str:
        settings = _read_settings(api)
        password = settings.get("SBER_TLS_P12_PASSWORD", "")
        state_dir = _state_dir(api)
        if not state_dir:
            return as_json({"error": "skill state directory is unavailable"})
        if not password:
            return as_json(
                {
                    "error": (
                        "Задайте SBER_TLS_P12_PASSWORD в Ouroboros Settings "
                        "и выдайте grant скиллу перед установкой сертификата."
                    )
                }
            )
        try:
            return as_json(install_p12_from_path(state_dir, source_path, password))
        except Exception as exc:
            return as_json({"error": str(exc)})

    api.register_tool(
        "install_tls_certificate",
        _install_tls,
        description=(
            "Установить TLS-сертификат Sber API из прикреплённого в чате .p12/.pfx. "
            "Передайте source_path — абсолютный путь к файлу из data/uploads/ после "
            "загрузки пользователем. Требует grant на SBER_TLS_P12_PASSWORD."
        ),
        schema={
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "Абсолютный путь к загруженному .p12 или .pfx файлу.",
                },
            },
            "required": ["source_path"],
        },
        timeout_sec=30,
    )

    api.register_tool(
        "get_client_info",
        _make_tool_handler(api, get_client_info),
        description=(
            "Получить расширенную информацию об организации пользователя и её счетах "
            "через Sber API REST (GET /v1/client-info). "
            "Требует SBER_ACCESS_TOKEN (scope GET_CLIENT_ACCOUNTS) и установленный TLS "
            "(install_tls_certificate + SBER_TLS_P12_PASSWORD)."
        ),
        schema={
            "type": "object",
            "properties": {},
        },
        timeout_sec=30,
    )

    api.register_route("info", _route_info, methods=("POST",))
    api.register_ui_tab(
        "client_info",
        "Клиент Сбербанк",
        icon="business",
        render=_UI_RENDER,
    )

    api.log(
        "info",
        "sber_client_info: extension registered (2 tools, 1 route, ui_tab)",
    )


__all__ = ["register"]
