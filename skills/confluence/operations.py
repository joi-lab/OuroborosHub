"""Small API operations; knowledge selection and authoring remain model decisions."""

from __future__ import annotations

import hashlib
import mimetypes
import re
import time
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urljoin

import httpx

from .client import ConfluenceClient, ConfluenceError, https_url, numeric_id, page_limit, storage

V2 = "/wiki/api/v2"
V1 = "/wiki/rest/api"


def _status(value: str) -> str:
    if value not in {"current", "draft"}:
        raise ConfluenceError("invalid_argument", "status must be current or draft.")
    return value


def _title(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfluenceError("invalid_argument", "title must be a nonempty string.")
    return value


class Operations:
    def __init__(self, client: ConfluenceClient, state_dir: Path):
        self.client = client
        self.state_dir = state_dir

    def _page_result(self, data: dict[str, Any], *, mutation: bool = False) -> dict[str, Any]:
        page_id = str(data.get("id") or "")
        if not page_id or not isinstance((data.get("version") or {}).get("number"), int):
            raise ConfluenceError("incomplete_response", "Provider omitted the page ID or version receipt.",
                                  outcome="unknown" if mutation else "not_applicable")
        # Canonical site URL avoids returning the gateway as a browser destination.
        url = f"{self.client.config.site_url}/wiki/pages/viewpage.action?pageId={page_id}" if page_id else None
        return {"ok": True, "page": data, "page_id": page_id,
                "version": (data.get("version") or {}).get("number"), "url": url}

    def test_connection(self, page_id: str = "") -> dict[str, Any]:
        """Identity and resource permissions are independently observed, never inferred."""
        checks: dict[str, Any] = {}
        try:
            actor = self.client.request("GET", V1 + "/user/current")
            identified = bool(actor.get("accountId")) and actor.get("type") != "anonymous"
            checks["identity"] = {"ok": identified, "status": "authenticated" if identified else "anonymous_or_unverified",
                                  "actor": {k: actor[k] for k in ("accountId", "displayName", "type") if k in actor}}
        except ConfluenceError as exc:
            checks["identity"] = exc.result()
        try:
            spaces = self.list_spaces(limit=1)
            checks["spaces"] = {"ok": True, "accessible_count_on_first_page": spaces["count"],
                                "complete": spaces["complete"]}
        except ConfluenceError as exc:
            checks["spaces"] = exc.result()
        if page_id:
            try:
                page = self.get_page(page_id)
                checks["page"] = {"ok": True, "page_id": page["page_id"], "version": page["version"],
                                   "title": page["page"].get("title"), "url": page["url"],
                                   "storage_available": isinstance(page["page"].get("body", {}).get("storage", {}).get("value"), str)}
            except ConfluenceError as exc:
                checks["page"] = exc.result()
        return {"ok": all(check.get("ok", False) for check in checks.values()),
                "auth_mode": self.client.config.mode, "site_url": self.client.config.site_url,
                "checks": checks, "writes_tested": False,
                "note": "A failed check can mean missing scope or resource permission, not an invalid token. No writes were attempted."}

    def list_spaces(self, space_key: str = "", limit: int = 25, next_url: str = "") -> dict[str, Any]:
        params: dict[str, Any] = {"limit": page_limit(limit)}
        if space_key:
            params["keys"] = space_key
        return self.client.collection(V2 + "/spaces", params, next_url)

    def list_pages(self, space_id: str = "", title: str = "", limit: int = 25,
                   next_url: str = "", status: str = "current") -> dict[str, Any]:
        params: dict[str, Any] = {"limit": page_limit(limit), "status": _status(status)}
        if space_id:
            params["space-id"] = numeric_id(space_id, "space_id")
        if title:
            params["title"] = title
        return self.client.collection(V2 + "/pages", params, next_url)

    def search(self, cql: str, limit: int = 25, next_url: str = "") -> dict[str, Any]:
        if not isinstance(cql, str) or not cql.strip():
            raise ConfluenceError("invalid_argument", "Supply a CQL expression.")
        return self.client.collection(V1 + "/search", {"cql": cql, "limit": page_limit(limit)}, next_url)

    def get_page(self, page_id: str, status: str = "current", version: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"body-format": "storage", "status": _status(status)}
        if status == "draft":
            params["get-draft"] = "true"
        if version is not None:
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise ConfluenceError("invalid_argument", "version must be a positive integer.")
            params["version"] = version
        data = self.client.request("GET", V2 + "/pages/" + self.client.page_id(page_id), params=params)
        if not isinstance(data.get("body", {}).get("storage", {}).get("value"), str):
            raise ConfluenceError("incomplete_response", "Page response omitted the requested full storage body.")
        return self._page_result(data)

    def create_page(self, space_id: str, title: str, body: str, parent_id: str = "",
                    status: str = "current") -> dict[str, Any]:
        payload: dict[str, Any] = {"spaceId": numeric_id(space_id, "space_id"), "title": _title(title),
                                   "status": _status(status), "body": storage(body)}
        if parent_id:
            payload["parentId"] = self.client.page_id(parent_id)
        return self._page_result(self.client.request("POST", V2 + "/pages", json=payload), mutation=True)

    def update_page(self, page_id: str, title: str, body: str, expected_version: int,
                    status: str = "current", version_message: str = "") -> dict[str, Any]:
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            raise ConfluenceError("invalid_argument", "expected_version must be the nonnegative integer read from this page.")
        page_id = self.client.page_id(page_id)
        payload = {"id": page_id, "title": _title(title), "status": _status(status), "body": storage(body),
                   "version": {"number": 1 if status == "draft" else expected_version + 1,
                               "message": version_message}}
        # Published pages have atomic version conflicts. Drafts stay at revision
        # 1, so this precheck cannot detect concurrent edits to the draft body.
        params = {"status": status, **({"get-draft": "true"} if status == "draft" else {})}
        current = self.client.request("GET", V2 + "/pages/" + page_id, params=params)
        observed = (current.get("version") or {}).get("number")
        if observed != expected_version:
            raise ConfluenceError("version_conflict", "Page changed; read it again before constructing an update.",
                                  expected_version=expected_version, current_version=observed, write_attempted=False)
        return self._page_result(self.client.request("PUT", V2 + "/pages/" + page_id, json=payload), mutation=True)

    def list_comments(self, page_id: str = "", parent_comment_id: str = "", kind: str = "footer",
                      limit: int = 25, next_url: str = "") -> dict[str, Any]:
        if kind not in {"footer", "inline"}:
            raise ConfluenceError("invalid_argument", "kind must be footer or inline.")
        if parent_comment_id:
            path = f"{V2}/{kind}-comments/{numeric_id(parent_comment_id, 'parent_comment_id')}/children"
        else:
            path = f"{V2}/pages/{self.client.page_id(page_id)}/{kind}-comments"
        return self.client.collection(path, {"limit": page_limit(limit), "body-format": "storage"}, next_url)

    def add_comment(self, body: str, page_id: str = "", parent_comment_id: str = "") -> dict[str, Any]:
        if bool(page_id) == bool(parent_comment_id):
            raise ConfluenceError("invalid_argument", "Supply exactly one of page_id or parent_comment_id.")
        payload: dict[str, Any] = {"body": storage(body)}
        if parent_comment_id:
            payload["parentCommentId"] = numeric_id(parent_comment_id, "parent_comment_id")
        else:
            payload["pageId"] = self.client.page_id(page_id)
        data = self.client.request("POST", V2 + "/footer-comments", json=payload)
        if not data.get("id"):
            raise ConfluenceError("incomplete_response", "Provider omitted the created comment ID.", outcome="unknown")
        return {"ok": True, "comment": data}

    def list_attachments(self, page_id: str, limit: int = 25, next_url: str = "") -> dict[str, Any]:
        return self.client.collection(f"{V2}/pages/{self.client.page_id(page_id)}/attachments",
                                      {"limit": page_limit(limit)}, next_url)

    def upload_attachment(self, page_id: str, file_path: str, comment: str = "",
                          minor_edit: bool = False) -> dict[str, Any]:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise ConfluenceError("local_file_error", "file_path must name an existing regular file.")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as file:
            data = self.client.request(
                "POST", f"{V1}/content/{self.client.page_id(page_id)}/child/attachment",
                headers={"X-Atlassian-Token": "nocheck"},
                files={"file": (path.name, file, mime),
                       "comment": (None, comment.encode("utf-8"), "text/plain; charset=utf-8"),
                       "minorEdit": (None, str(bool(minor_edit)).lower())},
            )
        results = data.get("results")
        if not isinstance(results, list) or not results or any(not isinstance(item, dict) or not item.get("id") for item in results):
            raise ConfluenceError("incomplete_response", "Provider omitted the uploaded attachment IDs.", outcome="unknown")
        return {"ok": True, "attachments": results, "response": data}

    def download_attachment(self, page_id: str, attachment_id: str, filename: str = "") -> dict[str, Any]:
        attachment_id = str(attachment_id).strip()
        if not re.fullmatch(r"(?:att)?[0-9]+", attachment_id):
            raise ConfluenceError("invalid_argument", "attachment_id must be a numeric ID, optionally prefixed by att.")
        filename = filename or f"attachment-{attachment_id}"
        windows_name = PureWindowsPath(filename)
        if (filename in {".", ".."} or windows_name.drive or windows_name.is_absolute()
                or any(c in filename for c in "/\\\0\r\n")):
            raise ConfluenceError("invalid_argument", "filename must be a plain file name, not a path.")
        url = self.client.api_url(f"{V1}/content/{self.client.page_id(page_id)}/child/attachment/{attachment_id}/download")
        authenticated = True
        output_dir = self.state_dir / "jobs" / uuid.uuid4().hex / "output"
        output_dir.mkdir(parents=True, exist_ok=False)
        output = output_dir / filename
        temporary = output_dir / (filename + ".partial")
        try:
            output.resolve().relative_to(output_dir.resolve())
            temporary.resolve().relative_to(output_dir.resolve())
        except ValueError as exc:
            raise ConfluenceError("invalid_argument", "filename resolves outside the attachment job directory.") from exc
        started = time.monotonic()
        try:
            for _ in range(6):
                if time.monotonic() - started > 90:
                    raise ConfluenceError("download_timeout", "Attachment download exceeded 90 seconds.")
                self.client.http.cookies.clear()
                headers = {"Authorization": self.client.authorization} if authenticated else {}
                with self.client.http.stream("GET", url, headers=headers) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise ConfluenceError("invalid_response", "Download redirect omitted Location.")
                        url = https_url(urljoin(url, location))
                        # Signed CDN URLs work, but auth can never return after an
                        # off-origin hop. Cookie jars are also cleared on each hop.
                        authenticated = authenticated and self.client.is_auth_destination(url)
                        continue
                    if response.status_code >= 300:
                        response.read()
                        self.client._error(response, "GET")
                    digest, count = hashlib.sha256(), 0
                    with temporary.open("xb") as file:
                        for chunk in response.iter_bytes():
                            if time.monotonic() - started > 90:
                                raise ConfluenceError("download_timeout", "Attachment download exceeded 90 seconds.")
                            file.write(chunk)
                            digest.update(chunk)
                            count += len(chunk)
                    temporary.replace(output)
                    return {"ok": True, "path": str(output), "bytes": count, "sha256": digest.hexdigest(),
                            "media_type": response.headers.get("Content-Type", ""), "attachment_id": attachment_id}
            raise ConfluenceError("too_many_redirects", "Attachment exceeded five redirects.")
        except httpx.HTTPError as exc:
            # A signed download URL may be credential-bearing; do not echo it.
            raise ConfluenceError("download_failed", "Attachment transfer failed.", exception_type=type(exc).__name__) from exc
        finally:
            temporary.unlink(missing_ok=True)
