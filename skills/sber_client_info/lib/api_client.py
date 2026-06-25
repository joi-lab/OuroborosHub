"""Direct REST client for Sber API client-info.

Docs: https://developers.sber.ru/docs/ru/sber-api/specifications/client-info/get-client-info
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

_API_PROD_BASE = "https://fintech.sberbank.ru:9443/fintech/api"
_API_TEST_BASE = "https://iftfintech.testsbi.sberbank.ru:9443/fintech/api"

_ALLOWED_HOSTS = (
    "fintech.sberbank.ru",
    "iftfintech.testsbi.sberbank.ru",
)


def resolve_api_base(env: str = "") -> str:
    """Default to the integration test stand unless prod is explicitly selected."""
    cleaned = (env or "").strip().lower()
    if cleaned in {"prod", "production", "prom"}:
        return _API_PROD_BASE
    return _API_TEST_BASE


def client_info_url(env: str = "") -> str:
    return f"{resolve_api_base(env)}/v1/client-info"


def _validate_host(url: str) -> Optional[str]:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return "invalid API URL"
    if host not in _ALLOWED_HOSTS:
        return f"host not allowed: {host}"
    return None


def _server_verify(url: str) -> bool:
    """Test stand may use a chain that fails default verify on some hosts (e.g. Windows)."""
    if os.environ.get("SBER_API_INSECURE", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }:
        return True
    host = (urlparse(url).hostname or "").lower()
    return host != "iftfintech.testsbi.sberbank.ru"


def _build_tls(cert_path: str, key_path: str) -> Optional[Tuple[str, str]]:
    cert = (cert_path or "").strip()
    key = (key_path or "").strip()
    if not cert and not key:
        return None
    if not cert or not key:
        raise ValueError("both SBER_TLS_CERT_PATH and SBER_TLS_KEY_PATH are required for mTLS")
    return (cert, key)


def get_client_info(
    *,
    url: str,
    access_token: str,
    cert_path: str = "",
    key_path: str = "",
    timeout_sec: float = 30.0,
) -> Dict[str, Any]:
    import httpx

    host_error = _validate_host(url)
    if host_error:
        return {"error": host_error}

    token = (access_token or "").strip()
    if not token:
        return {"error": "SBER_ACCESS_TOKEN is not configured or not granted"}

    try:
        cert = _build_tls(cert_path, key_path)
    except ValueError as exc:
        return {"error": str(exc)}

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    try:
        with httpx.Client(
            cert=cert,
            timeout=timeout_sec,
            verify=_server_verify(url),
        ) as client:
            response = client.get(url, headers=headers)
    except Exception as exc:
        return {"error": f"API request failed: {type(exc).__name__}: {exc}"}

    if response.status_code >= 400:
        body = response.text[:500]
        return {"error": f"API HTTP {response.status_code}: {body}"}

    try:
        data = response.json()
    except Exception as exc:
        return {"error": f"invalid API JSON response: {exc}"}

    return data if isinstance(data, dict) else {"result": data}


def as_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
