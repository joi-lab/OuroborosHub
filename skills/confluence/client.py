"""Confluence Cloud HTTP transport. No credential discovery or automatic retries."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import httpx

SETTINGS_KEYS = [
    "CONFLUENCE_SITE_URL", "CONFLUENCE_EMAIL", "CONFLUENCE_API_TOKEN",
    "CONFLUENCE_AUTH_MODE", "CONFLUENCE_CLOUD_ID",
]


class ConfluenceError(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.error = {"code": code, "message": message, **details}

    def result(self) -> dict[str, Any]:
        return {"ok": False, "error": self.error}


def origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or 443


def https_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and bool(parsed.hostname)
                 and not parsed.username and not parsed.password and not parsed.fragment)
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ConfluenceError("invalid_url", "Expected an HTTPS URL without credentials or fragment.")
    return url


def numeric_id(value: str, label: str = "id") -> str:
    value = str(value).strip()
    if not re.fullmatch(r"[0-9]+", value) or int(value) < 1:
        raise ConfluenceError("invalid_argument", f"{label} must be a positive numeric ID.")
    return value


@dataclass(frozen=True)
class Config:
    site_url: str
    token: str
    email: str
    mode: str
    cloud_id: str

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "Config":
        site = str(settings.get("CONFLUENCE_SITE_URL") or "").strip().rstrip("/")
        https_url(site)
        parsed = urlsplit(site)
        if parsed.query or parsed.path not in ("", "/wiki"):
            raise ConfluenceError("configuration", "CONFLUENCE_SITE_URL must be the site origin, optionally ending in /wiki.")
        site = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        mode = str(settings.get("CONFLUENCE_AUTH_MODE") or "personal").strip()
        if mode not in {"personal", "scoped", "scoped_bearer"}:
            raise ConfluenceError("configuration", "CONFLUENCE_AUTH_MODE must be personal, scoped, or scoped_bearer.")
        email = str(settings.get("CONFLUENCE_EMAIL") or "").strip()
        token = str(settings.get("CONFLUENCE_API_TOKEN") or "").strip()
        if not token or (mode != "scoped_bearer" and not email):
            raise ConfluenceError("configuration", "Configure and grant CONFLUENCE_API_TOKEN and, for Basic auth, CONFLUENCE_EMAIL.")
        if ":" in email or any(c in email + token for c in "\r\n"):
            raise ConfluenceError("configuration", "Credential fields contain unsupported separators.")
        cloud_id = str(settings.get("CONFLUENCE_CLOUD_ID") or "").strip()
        if cloud_id and not re.fullmatch(r"[A-Za-z0-9-]+", cloud_id):
            raise ConfluenceError("configuration", "CONFLUENCE_CLOUD_ID must be the site's cloud ID.")
        return cls(site, token, email, mode, cloud_id)


class ConfluenceClient:
    """One short-lived client per tool call, with explicit auth destinations."""

    def __init__(self, config: Config, *, transport: httpx.BaseTransport | None = None):
        self.config = config
        self.http = httpx.Client(transport=transport, timeout=httpx.Timeout(30, connect=10),
                                 follow_redirects=False)
        self.base = ""
        encoded = base64.b64encode(f"{config.email}:{config.token}".encode()).decode()
        self.authorization = (f"Bearer {config.token}" if config.mode == "scoped_bearer"
                              else f"Basic {encoded}")
        self._redactions = (self.authorization, encoded, config.token)

    def __enter__(self) -> "ConfluenceClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.http.close()

    def redact(self, text: str) -> str:
        for secret in self._redactions:
            text = text.replace(secret, "[redacted]")
        return text

    def _error(self, response: httpx.Response, method: str) -> None:
        if 200 <= response.status_code < 300:
            return
        try:
            body = response.json()
            if isinstance(body, dict):
                message = body.get("message") or body.get("errorMessages") or body.get("errors") or body.get("error") or body
                message = message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
            else:
                message = json.dumps(body, ensure_ascii=False)
        except ValueError:
            message = response.text or response.reason_phrase
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found",
                409: "version_conflict", 429: "rate_limited"}.get(response.status_code, "http_error")
        raise ConfluenceError(
            code, self.redact(message), http_status=response.status_code,
            retry_after=self.redact(response.headers.get("Retry-After", "")),
            outcome="unknown" if method != "GET" and response.status_code >= 500 else "rejected",
            automatic_retry=False,
        )

    def _json(self, response: httpx.Response, method: str) -> dict[str, Any]:
        self._error(response, method)
        try:
            value = response.json()
        except ValueError as exc:
            raise ConfluenceError("invalid_response", "Provider returned a non-JSON response.",
                                  http_status=response.status_code,
                                  outcome="unknown" if method != "GET" else "not_applicable") from exc
        if not isinstance(value, dict):
            raise ConfluenceError("invalid_response", "Provider response must be a JSON object.")
        return value

    def _send(self, method: str, url: str, *, authenticated: bool = True, **kwargs: Any) -> httpx.Response:
        # Cookies and client-level auth never travel between API/CDN destinations.
        self.http.cookies.clear()
        headers = {"Accept": "application/json", **kwargs.pop("headers", {})}
        if authenticated:
            headers["Authorization"] = self.authorization
        try:
            return self.http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ConfluenceError("transport_error", self.redact(str(exc)),
                                  outcome="unknown" if method != "GET" else "not_applicable",
                                  automatic_retry=False) from exc

    def api_url(self, path: str) -> str:
        if not self.base:
            if self.config.mode == "personal":
                self.base = self.config.site_url
            else:
                cloud_id = self.config.cloud_id
                if not cloud_id:
                    response = self._send("GET", self.config.site_url + "/_edge/tenant_info", authenticated=False)
                    cloud_id = str(self._json(response, "GET").get("cloudId") or "")
                    if not re.fullmatch(r"[A-Za-z0-9-]+", cloud_id):
                        raise ConfluenceError("configuration", "Site did not return cloudId; set CONFLUENCE_CLOUD_ID explicitly.")
                self.base = f"https://api.atlassian.com/ex/confluence/{cloud_id}"
        return self.base + path

    def is_auth_destination(self, url: str) -> bool:
        expected = urlsplit(self.api_url("/wiki/"))
        return origin(url) == origin(self.base) and urlsplit(url).path.startswith(expected.path)

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        return self._json(self._send(method, self.api_url(path), **kwargs), method)

    def page_id(self, value: str) -> str:
        value = str(value).strip()
        if value.isdecimal():
            return numeric_id(value, "page_id")
        parsed = urlsplit(value)
        if origin(value) != origin(self.config.site_url) or parsed.username or parsed.password:
            raise ConfluenceError("invalid_argument", "Use a page ID or a page URL on CONFLUENCE_SITE_URL.")
        query_id = parse_qs(parsed.query).get("pageId", [])
        if query_id and parsed.path.endswith("/pages/viewpage.action"):
            return numeric_id(query_id[0], "page_id")
        match = re.search(r"/pages/([0-9]+)(?:/|$)", parsed.path)
        if match:
            return numeric_id(match[1], "page_id")
        raise ConfluenceError("invalid_argument", "URL has no page ID. Use /spaces/KEY/pages/ID/... or ?pageId=ID.")

    def _page_url(self, supplied: str, endpoint: str, current: str) -> str:
        """Normalize next links without allowing a different site, tenant or API."""
        parsed = urlsplit(supplied)
        if not parsed.scheme and not parsed.netloc and parsed.path.startswith(("/wiki/", "/rest/api/")):
            # v1 CQL links omit the /wiki context; v2 links include it.
            path = "/wiki" + parsed.path if parsed.path.startswith("/rest/api/") else parsed.path
            candidate = self.api_url(path) + ("?" + parsed.query if parsed.query else "")
        else:
            candidate = urljoin(current, supplied)
        https_url(candidate)
        expected = self.api_url(endpoint)
        if origin(candidate) != origin(expected) or urlsplit(candidate).path != urlsplit(expected).path:
            raise ConfluenceError("invalid_pagination", "Pagination URL must stay on the same configured API resource.")
        return candidate

    def collection(self, path: str, params: dict[str, Any], next_url: str = "") -> dict[str, Any]:
        url = self.api_url(path)
        if next_url:
            url = self._page_url(next_url, path, url)
            params = {}
        response = self._send("GET", url, params=params or None)
        data = self._json(response, "GET")
        if not isinstance(data.get("results"), list):
            raise ConfluenceError("invalid_response", "Provider collection omitted its results array.")
        links = data.get("_links") or {}
        following = links.get("next") or response.links.get("next", {}).get("url") or ""
        following = self._page_url(str(following), path, str(response.url)) if following else ""
        return {"ok": True, "results": data["results"], "count": len(data["results"]),
                "complete": not bool(following), "next_url": following or None,
                "total_size": data.get("totalSize"), "limit": data.get("limit", params.get("limit"))}


def page_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 250:
        raise ConfluenceError("invalid_argument", "limit must be an integer between 1 and 250.")
    return value


def storage(value: str) -> dict[str, str]:
    if not isinstance(value, str):
        raise ConfluenceError("invalid_argument", "body must be a Confluence storage-format string.")
    return {"representation": "storage", "value": value}
