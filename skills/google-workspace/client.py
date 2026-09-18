"""Google Workspace REST Client for Sheets, Docs, and Drive APIs."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote
import httpx

try:
    from .auth import get_access_token
except ImportError:
    from auth import get_access_token

SHEETS_BASE_URL = "https://sheets.googleapis.com/v4/spreadsheets"
DOCS_BASE_URL = "https://docs.googleapis.com/v1/documents"
DRIVE_BASE_URL = "https://www.googleapis.com/drive/v3/files"

DEFAULT_TIMEOUT = 30.0
MAX_TEXT_EXPORT_CHARS = 100_000
MAX_SHEETS_ROWS = 5_000
MAX_SHEETS_CELLS = 50_000
VALUE_RENDER_OPTIONS = ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA")
DRIVE_FILE_FIELDS = (
    "id,name,mimeType,size,modifiedTime,webViewLink,parents,owners,"
    "lastModifyingUser,version,capabilities"
)

# Pattern for valid Google Drive/Docs/Sheets resource IDs (alphanumeric, dashes, underscores)
RESOURCE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{4,128}$")

# MIME types supported for text extraction
SUPPORTED_TEXT_MIME_PREFIXES = ("text/",)
SUPPORTED_TEXT_MIMES = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/yaml",
    "application/sql",
    "application/csv",
}


def _validate_resource_id(val: str, name: str = "resource_id") -> str:
    """Validate that an ID conforms to standard Google Drive resource identifier patterns."""
    if not val or not str(val).strip():
        raise ValueError(f"{name} is required.")
    cleaned = str(val).strip()
    if cleaned == "root":
        return cleaned
    if not RESOURCE_ID_PATTERN.match(cleaned):
        raise ValueError(
            f"Invalid {name} format: '{val}'. Expected a valid Google Drive/Docs identifier (letters, numbers, underscores, dashes)."
        )
    return cleaned


def _file_context(item: Dict[str, Any], file_id: str) -> Dict[str, Any]:
    """Preserve provider metadata without inventing absent owners or permissions."""
    context = {"url": item.get("webViewLink") or f"https://drive.google.com/open?id={file_id}"}
    for source, target in (
        ("owners", "owners"), ("lastModifyingUser", "last_modifying_user"),
        ("version", "version"), ("modifiedTime", "modified_time"),
        ("capabilities", "capabilities"),
    ):
        if source in item:
            context[target] = item[source]
    return context


class GoogleWorkspaceClient:
    """Client for Google Sheets, Docs, and Drive REST APIs using Service Account auth."""

    def __init__(
        self,
        raw_sa_info: Optional[str] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.raw_sa_info = raw_sa_info
        self._external_client = http_client is not None
        self.client = http_client or httpx.Client(timeout=DEFAULT_TIMEOUT)

    def close(self) -> None:
        if not self._external_client:
            self.client.close()

    def __enter__(self) -> GoogleWorkspaceClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _get_headers(self, force_refresh: bool = False) -> Dict[str, str]:
        token = get_access_token(
            raw_info=self.raw_sa_info,
            http_client=self.client,
            force_refresh=force_refresh,
        )
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        content: Optional[bytes] = None,
    ) -> httpx.Response:
        """Execute an HTTP request with automatic 401 token refresh retry."""
        req_headers = self._get_headers(force_refresh=False)
        if headers:
            req_headers.update(headers)

        resp = self.client.request(
            method=method,
            url=url,
            headers=req_headers,
            params=params,
            json=json_body,
            content=content,
        )

        # Retry once on 401 Unauthorized with fresh token
        if resp.status_code == 401:
            req_headers = self._get_headers(force_refresh=True)
            if headers:
                req_headers.update(headers)
            resp = self.client.request(
                method=method,
                url=url,
                headers=req_headers,
                params=params,
                json=json_body,
                content=content,
            )

        if resp.status_code >= 400:
            error_detail = resp.text[:1000]
            try:
                err_json = resp.json()
                if "error" in err_json:
                    err_obj = err_json["error"]
                    if isinstance(err_obj, dict):
                        error_detail = str(err_obj.get("message", error_detail))[:1000]
                    elif isinstance(err_obj, str):
                        error_detail = str(err_obj)[:1000]
            except Exception:
                pass
            raise RuntimeError(f"Google API error ({resp.status_code} {resp.reason_phrase}): {error_detail}")

        return resp

    # --- Sheets API ---

    def sheets_info(self, spreadsheet_id: str) -> Dict[str, Any]:
        """Discover workbook and tab metadata without downloading cell data."""
        clean_sid = _validate_resource_id(spreadsheet_id, "spreadsheet_id")
        params = {"fields": (
            "spreadsheetId,spreadsheetUrl,properties(title,locale,timeZone),"
            "sheets(properties(sheetId,title,index,sheetType,gridProperties,hidden))"
        )}
        data = self._request("GET", f"{SHEETS_BASE_URL}/{clean_sid}", params=params).json()
        properties = data.get("properties", {})
        sheets = []
        for sheet in data.get("sheets", []):
            values = sheet.get("properties", {})
            sheets.append({target: values[source] for source, target in (
                ("sheetId", "sheet_id"), ("title", "title"), ("index", "index"),
                ("sheetType", "sheet_type"), ("gridProperties", "grid_properties"),
                ("hidden", "hidden"),
            ) if source in values})
        return {
            "spreadsheet_id": data.get("spreadsheetId", clean_sid),
            "title": properties.get("title"), "locale": properties.get("locale"),
            "time_zone": properties.get("timeZone"),
            "url": data.get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{clean_sid}/edit",
            "sheets": sheets,
        }

    def sheets_read(
        self,
        spreadsheet_id: str,
        range_name: str,
        max_rows: int = MAX_SHEETS_ROWS,
        value_render_option: str = "FORMATTED_VALUE",
    ) -> Dict[str, Any]:
        """Read values from a rectangular range in a Google Sheet with bounded rows and cell budget."""
        clean_sid = _validate_resource_id(spreadsheet_id, "spreadsheet_id")
        if not range_name or not range_name.strip():
            raise ValueError("range_name is required (e.g. 'Sheet1!A1:D10' or 'A1:B').")
        if value_render_option not in VALUE_RENDER_OPTIONS:
            raise ValueError("value_render_option must be FORMATTED_VALUE, UNFORMATTED_VALUE, or FORMULA.")

        effective_max_rows = max(1, min(int(max_rows), MAX_SHEETS_ROWS))

        # Percent-encode range name (spaces in sheet names etc.)
        encoded_range = quote(range_name.strip(), safe="!:")
        url = f"{SHEETS_BASE_URL}/{clean_sid}/values/{encoded_range}"
        resp = self._request("GET", url, params={"valueRenderOption": value_render_option})
        data = resp.json()
        all_values = data.get("values", [])
        original_row_count = len(all_values)

        # Calculate cell count and bound
        bounded_values = []
        total_cells = 0
        truncated = False

        for row in all_values[:effective_max_rows]:
            if not isinstance(row, list):
                row = [row]
            if total_cells + len(row) > MAX_SHEETS_CELLS:
                truncated = True
                break
            bounded_values.append(row)
            total_cells += len(row)

        if original_row_count > len(bounded_values):
            truncated = True

        return {
            "spreadsheet_id": clean_sid,
            "range": data.get("range", range_name),
            "major_dimension": data.get("majorDimension", "ROWS"),
            "value_render_option": value_render_option,
            "values": bounded_values,
            "row_count": len(bounded_values),
            "original_row_count": original_row_count,
            "cell_count": total_cells,
            "truncated": truncated,
        }

    def sheets_append(
        self,
        spreadsheet_id: str,
        range_name: str,
        rows: List[List[Any]],
        value_input_option: str = "USER_ENTERED",
    ) -> Dict[str, Any]:
        """Append rows to a spreadsheet table."""
        clean_sid = _validate_resource_id(spreadsheet_id, "spreadsheet_id")
        if not range_name or not range_name.strip():
            raise ValueError("range_name is required.")
        if not isinstance(rows, list):
            raise ValueError("rows must be a list of lists representing rows to append.")

        encoded_range = quote(range_name.strip(), safe="!:")
        url = f"{SHEETS_BASE_URL}/{clean_sid}/values/{encoded_range}:append"
        params = {"valueInputOption": value_input_option}
        payload = {"values": rows}

        resp = self._request("POST", url, params=params, json_body=payload)
        data = resp.json()
        updates = data.get("updates", {})
        return {
            "spreadsheet_id": clean_sid,
            "table_range": data.get("tableRange"),
            "updated_range": updates.get("updatedRange"),
            "updated_rows": updates.get("updatedRows", 0),
            "updated_columns": updates.get("updatedColumns", 0),
            "updated_cells": updates.get("updatedCells", 0),
        }

    # --- Docs API ---

    def docs_create(
        self,
        title: str,
        folder_id: Optional[str] = None,
        template_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new Google Document from scratch or by copying a template."""
        if not title or not title.strip():
            raise ValueError("title is required.")

        title = title.strip()
        clean_folder = _validate_resource_id(folder_id, "folder_id") if folder_id else None
        clean_template = _validate_resource_id(template_id, "template_id") if template_id else None

        if clean_template:
            # Copy template via Drive v3 API
            copy_url = f"{DRIVE_BASE_URL}/{clean_template}/copy"
            params = {"supportsAllDrives": "true"}
            body: Dict[str, Any] = {"name": title}
            if clean_folder:
                body["parents"] = [clean_folder]

            resp = self._request("POST", copy_url, params=params, json_body=body)
            data = resp.json()
            doc_id = data["id"]
        else:
            # Create blank doc via Docs v1 API
            resp = self._request("POST", DOCS_BASE_URL, json_body={"title": title})
            data = resp.json()
            doc_id = data["documentId"]

            # If folder_id specified, move the newly created doc to the folder via Drive v3
            if clean_folder:
                get_meta_url = f"{DRIVE_BASE_URL}/{doc_id}"
                get_resp = self._request("GET", get_meta_url, params={"fields": "parents", "supportsAllDrives": "true"})
                current_parents = get_resp.json().get("parents", [])

                update_url = f"{DRIVE_BASE_URL}/{doc_id}"
                update_params: Dict[str, Any] = {
                    "addParents": clean_folder,
                    "supportsAllDrives": "true",
                }
                if current_parents:
                    update_params["removeParents"] = ",".join(current_parents)

                self._request("PATCH", update_url, params=update_params, json_body={})

        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
        return {
            "document_id": doc_id,
            "title": title,
            "url": doc_url,
            "folder_id": clean_folder,
            "template_id": clean_template,
        }

    # --- Drive API ---

    def drive_list(
        self,
        folder_id: Optional[str] = None,
        page_size: int = 50,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List files and folders shared with the Service Account or inside a folder."""
        page_size = max(1, min(page_size, 100))
        clean_folder = _validate_resource_id(folder_id, "folder_id") if folder_id else None

        q_parts = ["trashed = false"]
        if clean_folder:
            q_parts.append(f"'{clean_folder}' in parents")

        params: Dict[str, Any] = {
            "q": " and ".join(q_parts),
            "pageSize": page_size,
            "fields": f"nextPageToken,files({DRIVE_FILE_FIELDS})",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token

        resp = self._request("GET", DRIVE_BASE_URL, params=params)
        data = resp.json()

        files_list = []
        for item in data.get("files", []):
            files_list.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "mime_type": item.get("mimeType"),
                "size": item.get("size"),
                "modified_time": item.get("modifiedTime"),
                "parents": item.get("parents", []),
                **_file_context(item, item.get("id")),
            })

        return {
            "folder_id": clean_folder,
            "count": len(files_list),
            "files": files_list,
            "next_page_token": data.get("nextPageToken"),
        }

    def drive_read_text(self, file_id: str, max_chars: int = MAX_TEXT_EXPORT_CHARS) -> Dict[str, Any]:
        """Read or export the plain text content of a Google Doc, Sheet, or text file with bounded stream reading."""
        clean_fid = _validate_resource_id(file_id, "file_id")
        effective_max = min(max(1, int(max_chars)), MAX_TEXT_EXPORT_CHARS)

        # 1. Fetch file metadata
        meta_url = f"{DRIVE_BASE_URL}/{clean_fid}"
        meta_params = {
            "fields": DRIVE_FILE_FIELDS,
            "supportsAllDrives": "true",
        }
        resp = self._request("GET", meta_url, params=meta_params)
        meta = resp.json()
        mime_type = meta.get("mimeType", "")
        file_name = meta.get("name", "")
        remote_file_size = int(meta["size"]) if meta.get("size") and str(meta["size"]).isdigit() else None

        # Check if MIME type is supported for text export/reading
        is_supported_text = (
            mime_type in SUPPORTED_TEXT_MIMES
            or any(mime_type.startswith(prefix) for prefix in SUPPORTED_TEXT_MIME_PREFIXES)
        )
        if not is_supported_text:
            raise ValueError(
                f"Unsupported file type for text export: '{mime_type}'. "
                "drive_read_text supports Google Docs, Google Sheets, plain text, JSON, CSV, and markdown files."
            )

        # 2. Determine export/download URL & params
        export_context = {}
        if mime_type == "application/vnd.google-apps.document":
            url = f"{DRIVE_BASE_URL}/{clean_fid}/export"
            params = {"mimeType": "text/plain"}
        elif mime_type == "application/vnd.google-apps.spreadsheet":
            url = f"{DRIVE_BASE_URL}/{clean_fid}/export"
            params = {"mimeType": "text/csv"}
            export_context = {
                "export_scope": "first_sheet_only",
                "export_note": (
                    "CSV export contains only the first sheet. Use sheets_info to discover tabs "
                    "and sheets_read for their ranges. truncated reports character clipping only."
                ),
            }
        else:
            url = f"{DRIVE_BASE_URL}/{clean_fid}"
            params = {"alt": "media", "supportsAllDrives": "true"}

        # Read streaming response bounded to effective_max chars (approx 4x bytes headroom)
        max_bytes_to_read = effective_max * 4 + 4096
        req_headers = self._get_headers(force_refresh=False)

        def _read_stream(r_headers: Dict[str, str]) -> bytes:
            with self.client.stream("GET", url, headers=r_headers, params=params) as stream_resp:
                if stream_resp.status_code == 401:
                    fresh_headers = self._get_headers(force_refresh=True)
                    with self.client.stream("GET", url, headers=fresh_headers, params=params) as retry_resp:
                        if retry_resp.status_code >= 400:
                            err_text = retry_resp.read().decode("utf-8", errors="replace")[:1000]
                            raise RuntimeError(f"Google API error ({retry_resp.status_code} {retry_resp.reason_phrase}): {err_text}")
                        chunks = []
                        total_bytes = 0
                        for chunk in retry_resp.iter_bytes(chunk_size=8192):
                            chunks.append(chunk)
                            total_bytes += len(chunk)
                            if total_bytes >= max_bytes_to_read:
                                break
                        return b"".join(chunks)
                elif stream_resp.status_code >= 400:
                    err_text = stream_resp.read().decode("utf-8", errors="replace")[:1000]
                    raise RuntimeError(f"Google API error ({stream_resp.status_code} {stream_resp.reason_phrase}): {err_text}")
                else:
                    chunks = []
                    total_bytes = 0
                    for chunk in stream_resp.iter_bytes(chunk_size=8192):
                        chunks.append(chunk)
                        total_bytes += len(chunk)
                        if total_bytes >= max_bytes_to_read:
                            break
                    return b"".join(chunks)

        raw_bytes = _read_stream(req_headers)
        text_content = raw_bytes.decode("utf-8", errors="replace")
        truncated = False
        if len(text_content) > effective_max:
            text_content = text_content[:effective_max] + f"\n\n[... Truncated at {effective_max} characters ...]"
            truncated = True

        return {
            "file_id": clean_fid,
            "name": file_name,
            "mime_type": mime_type,
            "text": text_content,
            "length": len(text_content),
            "remote_size_bytes": remote_file_size,
            "truncated": truncated,
            **_file_context(meta, clean_fid),
            **export_context,
        }
