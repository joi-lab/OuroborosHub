"""Google Workspace Extension Plugin for Ouroboros.

Exposes tools for Google Sheets, Docs, and Drive, settings configuration,
and a declarative status UI tab.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

try:
    from starlette.responses import JSONResponse as StarletteJSONResponse
except ImportError:
    StarletteJSONResponse = None

try:
    from .auth import parse_service_account_info, get_access_token
    from .client import GoogleWorkspaceClient
except ImportError:
    from auth import parse_service_account_info, get_access_token
    from client import GoogleWorkspaceClient

if TYPE_CHECKING:
    from contracts.plugin_api import PluginAPI

logger = logging.getLogger(__name__)


def _format_json(data: Any) -> str:
    """Format dictionary/list into compact JSON string."""
    return json.dumps(data, indent=2, ensure_ascii=False)


def _get_client_sa_json(api: PluginAPI) -> Optional[str]:
    """Retrieve the granted GOOGLE_SERVICE_ACCOUNT_JSON secret from host settings."""
    try:
        settings_dict = api.get_settings(["GOOGLE_SERVICE_ACCOUNT_JSON"])
        val = settings_dict.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if val and str(val).strip():
            return str(val).strip()
        return None
    except Exception as exc:
        try:
            api.log("error", f"Failed to retrieve settings/secrets from host: {exc}")
        except Exception:
            pass
        raise RuntimeError(f"Error reading GOOGLE_SERVICE_ACCOUNT_JSON from host settings: {exc}") from exc


def _get_local_settings(api: PluginAPI) -> Dict[str, Any]:
    """Read local settings from the skill state directory with strict type validation."""
    try:
        state_dir = Path(api.get_state_dir())
        settings_path = state_dir / "settings.json"
        if not settings_path.is_file():
            return {}
        parsed = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            try:
                api.log("warning", f"Local settings.json is not a JSON object: {type(parsed).__name__}")
            except Exception:
                pass
            return {}
        return parsed
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        try:
            api.log("warning", f"Failed to read local settings.json: {exc}")
        except Exception:
            pass
        return {}


def _resolve_folder_id(api: PluginAPI, passed_folder_id: Optional[str]) -> Optional[str]:
    """Resolve target folder ID from parameter or default setting."""
    if passed_folder_id and passed_folder_id.strip():
        return passed_folder_id.strip()
    local_cfg = _get_local_settings(api)
    return local_cfg.get("DEFAULT_FOLDER_ID") or None


def _resolve_template_id(api: PluginAPI, template_id: Optional[str]) -> Optional[str]:
    """Resolve template ID from raw ID or template map with typed error handling."""
    if not template_id or not template_id.strip():
        return None
    tid = template_id.strip()
    local_cfg = _get_local_settings(api)
    templates_raw = local_cfg.get("TEMPLATES_JSON")
    if templates_raw:
        try:
            template_map = json.loads(templates_raw) if isinstance(templates_raw, str) else templates_raw
            if isinstance(template_map, dict) and tid in template_map:
                return str(template_map[tid]).strip()
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            try:
                api.log("warning", f"Invalid TEMPLATES_JSON configuration mapping: {exc}")
            except Exception:
                pass
    return tid


# --- Tool Handlers ---


def _make_workspace_auth_status(api: PluginAPI):
    def workspace_auth_status() -> str:
        """Verify Google Service Account credentials and OAuth2 token connectivity."""
        sa_json = _get_client_sa_json(api)
        if not sa_json:
            raise RuntimeError(
                "Missing 'GOOGLE_SERVICE_ACCOUNT_JSON'. Please configure in Settings -> Secrets and grant it to 'google-workspace'."
            )
        try:
            sa_info = parse_service_account_info(sa_json)
            token = get_access_token(sa_json, force_refresh=True)
            if not token:
                raise RuntimeError("Failed to obtain OAuth2 access token from Google.")

            return _format_json({
                "status": "ready",
                "client_email": sa_info.get("client_email"),
                "project_id": sa_info.get("project_id"),
                "message": "Google Workspace Service Account authentication verified successfully.",
            })
        except Exception as exc:
            raise RuntimeError(f"Google Workspace authentication preflight failed: {exc}") from exc

    return workspace_auth_status


def _make_sheets_read(api: PluginAPI):
    def sheets_read(spreadsheet_id: str, range: str, max_rows: int = 5000) -> str:
        """Read cell values from a rectangular range in a Google Sheet."""
        sa_json = _get_client_sa_json(api)
        try:
            with GoogleWorkspaceClient(raw_sa_info=sa_json) as client:
                res = client.sheets_read(spreadsheet_id=spreadsheet_id, range_name=range, max_rows=max_rows)
                return _format_json(res)
        except Exception as exc:
            raise RuntimeError(f"sheets_read failed: {exc}") from exc

    return sheets_read


def _make_sheets_append(api: PluginAPI):
    def sheets_append(spreadsheet_id: str, range: str, rows: List[List[Any]], value_input_option: str = "USER_ENTERED") -> str:
        """Append rows of data to a spreadsheet table."""
        sa_json = _get_client_sa_json(api)
        try:
            with GoogleWorkspaceClient(raw_sa_info=sa_json) as client:
                res = client.sheets_append(
                    spreadsheet_id=spreadsheet_id,
                    range_name=range,
                    rows=rows,
                    value_input_option=value_input_option,
                )
                return _format_json(res)
        except Exception as exc:
            raise RuntimeError(f"sheets_append failed: {exc}") from exc

    return sheets_append


def _make_docs_create(api: PluginAPI):
    def docs_create(title: str, folder_id: Optional[str] = None, template_id: Optional[str] = None) -> str:
        """Create a new Google Document from scratch or by copying a template."""
        sa_json = _get_client_sa_json(api)
        effective_folder_id = _resolve_folder_id(api, folder_id)
        effective_template_id = _resolve_template_id(api, template_id)

        try:
            with GoogleWorkspaceClient(raw_sa_info=sa_json) as client:
                res = client.docs_create(
                    title=title,
                    folder_id=effective_folder_id,
                    template_id=effective_template_id,
                )
                return _format_json(res)
        except Exception as exc:
            raise RuntimeError(f"docs_create failed: {exc}") from exc

    return docs_create


def _make_drive_list(api: PluginAPI):
    def drive_list(folder_id: Optional[str] = None, page_size: int = 50, page_token: Optional[str] = None) -> str:
        """List files and folders shared with the Service Account or inside a folder."""
        sa_json = _get_client_sa_json(api)
        effective_folder_id = _resolve_folder_id(api, folder_id)

        try:
            with GoogleWorkspaceClient(raw_sa_info=sa_json) as client:
                res = client.drive_list(
                    folder_id=effective_folder_id,
                    page_size=page_size,
                    page_token=page_token,
                )
                return _format_json(res)
        except Exception as exc:
            raise RuntimeError(f"drive_list failed: {exc}") from exc

    return drive_list


def _make_drive_read_text(api: PluginAPI):
    def drive_read_text(file_id: str, max_chars: int = 100000) -> str:
        """Read or export the plain text content of a Google Doc, Sheet, or text file."""
        sa_json = _get_client_sa_json(api)
        try:
            with GoogleWorkspaceClient(raw_sa_info=sa_json) as client:
                res = client.drive_read_text(file_id=file_id, max_chars=max_chars)
                return _format_json(res)
        except Exception as exc:
            raise RuntimeError(f"drive_read_text failed: {exc}") from exc

    return drive_read_text


# --- Settings Save Route Handler ---


def _make_settings_save(api: PluginAPI):
    async def settings_save(request: Any) -> Any:
        try:
            payload = await request.json()
        except Exception as exc:
            err_data = {"status": "error", "message": f"Invalid JSON payload: {exc}"}
            if StarletteJSONResponse is not None:
                return StarletteJSONResponse(err_data, status_code=400)
            return err_data

        if not isinstance(payload, dict):
            err_data = {"status": "error", "message": "Expected JSON object."}
            if StarletteJSONResponse is not None:
                return StarletteJSONResponse(err_data, status_code=400)
            return err_data

        default_folder = str(payload.get("DEFAULT_FOLDER_ID", "")).strip()
        templates_json = str(payload.get("TEMPLATES_JSON", "")).strip()

        if templates_json:
            try:
                parsed_t = json.loads(templates_json)
                if not isinstance(parsed_t, dict):
                    err_data = {"status": "error", "message": "TEMPLATES_JSON must be a JSON object mapping alias -> doc_id."}
                    if StarletteJSONResponse is not None:
                        return StarletteJSONResponse(err_data, status_code=400)
                    return err_data
            except Exception as exc:
                err_data = {"status": "error", "message": f"Invalid JSON in TEMPLATES_JSON: {exc}"}
                if StarletteJSONResponse is not None:
                    return StarletteJSONResponse(err_data, status_code=400)
                return err_data

        state_dir = Path(api.get_state_dir())
        state_dir.mkdir(parents=True, exist_ok=True)
        settings_path = state_dir / "settings.json"

        settings_data = {
            "DEFAULT_FOLDER_ID": default_folder,
            "TEMPLATES_JSON": templates_json,
        }
        try:
            settings_path.write_text(json.dumps(settings_data, indent=2), encoding="utf-8")
        except Exception as exc:
            err_data = {"status": "error", "message": f"Failed to persist settings: {exc}"}
            if StarletteJSONResponse is not None:
                return StarletteJSONResponse(err_data, status_code=500)
            return err_data

        ok_data = {"status": "ok", "message": "Settings saved successfully."}
        if StarletteJSONResponse is not None:
            return StarletteJSONResponse(ok_data, status_code=200)
        return ok_data

    return settings_save


# --- Plugin Registration Entrypoint ---


def register(api: PluginAPI) -> None:
    """Register Google Workspace tools, settings sections, and declarative UI tab with Ouroboros."""

    # 1. Register Tools

    api.register_tool(
        name="workspace_auth_status",
        handler=_make_workspace_auth_status(api),
        description="Verify Google Service Account credentials and OAuth2 token connectivity without making changes.",
        schema={
            "type": "object",
            "properties": {},
        },
        timeout_sec=30,
    )

    api.register_tool(
        name="sheets_read",
        handler=_make_sheets_read(api),
        description="Read rectangular cell values from a Google Spreadsheet range (e.g. 'Sheet1!A1:D10' or 'A1:C').",
        schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "Google Spreadsheet ID (found in URL: https://docs.google.com/spreadsheets/d/<ID>/edit).",
                },
                "range": {
                    "type": "string",
                    "description": "A1 notation range to read (e.g. 'Sheet1!A1:E20', 'RawData!A:C').",
                },
                "max_rows": {
                    "type": "integer",
                    "description": "Maximum rows to return (default 5000).",
                    "default": 5000,
                },
            },
            "required": ["spreadsheet_id", "range"],
        },
        timeout_sec=60,
    )

    api.register_tool(
        name="sheets_append",
        handler=_make_sheets_append(api),
        description="Append rows of data to a table in a Google Spreadsheet.",
        schema={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "Google Spreadsheet ID.",
                },
                "range": {
                    "type": "string",
                    "description": "A1 range or sheet name to append to (e.g. 'Sheet1!A1' or 'Sheet1').",
                },
                "rows": {
                    "type": "array",
                    "description": "Array of rows (each row is an array of cell values) to append.",
                    "items": {"type": "array", "items": {}},
                },
                "value_input_option": {
                    "type": "string",
                    "description": "How input data should be interpreted: 'USER_ENTERED' (parses numbers/dates/formulas) or 'RAW'.",
                    "enum": ["USER_ENTERED", "RAW"],
                    "default": "USER_ENTERED",
                },
            },
            "required": ["spreadsheet_id", "range", "rows"],
        },
        timeout_sec=60,
    )

    api.register_tool(
        name="docs_create",
        handler=_make_docs_create(api),
        description="Create a new Google Document from scratch or by copying an existing Google Doc template. Returns the document ID and edit URL.",
        schema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Title of the new document.",
                },
                "folder_id": {
                    "type": "string",
                    "description": "Optional Google Drive folder ID where the document should be created (falls back to DEFAULT_FOLDER_ID setting).",
                },
                "template_id": {
                    "type": "string",
                    "description": "Optional Google Doc template ID or template alias name configured in settings.",
                },
            },
            "required": ["title"],
        },
        timeout_sec=60,
    )

    api.register_tool(
        name="drive_list",
        handler=_make_drive_list(api),
        description="List files and folders shared with the Google Service Account, or inside a specific parent folder.",
        schema={
            "type": "object",
            "properties": {
                "folder_id": {
                    "type": "string",
                    "description": "Optional parent folder ID to list files from (falls back to DEFAULT_FOLDER_ID setting).",
                },
                "page_size": {
                    "type": "integer",
                    "description": "Number of files to return (1-100, default 50).",
                    "default": 50,
                },
                "page_token": {
                    "type": "string",
                    "description": "Optional pagination token from previous list call.",
                },
            },
        },
        timeout_sec=60,
    )

    api.register_tool(
        name="drive_read_text",
        handler=_make_drive_read_text(api),
        description="Read or export plain text content from a Google Doc, Google Sheet (as CSV), or text file on Google Drive.",
        schema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "Google Drive file ID.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default 100000).",
                    "default": 100000,
                },
            },
            "required": ["file_id"],
        },
        timeout_sec=60,
    )

    # 2. Register HTTP Routes

    api.register_route(
        path="settings/save",
        handler=_make_settings_save(api),
        methods=("POST",),
    )

    # 3. Register Settings Section

    api.register_settings_section(
        section_id="google_workspace",
        title="Google Workspace",
        schema={
            "components": [
                {
                    "type": "markdown",
                    "text": "Configure default folder targets and template mappings for Google Workspace tools.",
                },
                {
                    "type": "form",
                    "route": "settings/save",
                    "method": "POST",
                    "submit_label": "Save Workspace Settings",
                    "fields": [
                        {
                            "name": "DEFAULT_FOLDER_ID",
                            "label": "Default Drive Folder ID",
                            "type": "text",
                            "placeholder": "e.g. 1aBcDeFgHiJkLmNoPqRsTuVwXyZ",
                            "help": "Default Google Drive folder used when no folder_id parameter is passed.",
                        },
                        {
                            "name": "TEMPLATES_JSON",
                            "label": "Document Templates Map (JSON)",
                            "type": "textarea",
                            "placeholder": '{\n  "weekly_report": "1xYzDocId...",\n  "meeting_notes": "1aBcDocId..."\n}',
                            "help": "Optional JSON dictionary mapping template alias names to template Google Doc IDs.",
                        },
                    ],
                },
            ]
        },
    )

    # 4. Register Declarative UI Tab

    api.register_ui_tab(
        tab_id="google_workspace",
        title="Google Workspace",
        render={
            "kind": "declarative",
            "schema_version": 1,
            "components": [
                {
                    "type": "callout",
                    "title": "Google Workspace Service Account Integration",
                    "tone": "info",
                    "text": "Access Google Sheets, Docs, and Drive via Service Account authentication. Only resources explicitly shared with the service account email are accessible.",
                },
                {
                    "type": "group",
                    "title": "Quick Start Guide",
                    "components": [
                        {
                            "type": "markdown",
                            "text": (
                                "1. **Configure Secret**: Add `GOOGLE_SERVICE_ACCOUNT_JSON` under **Settings → Secrets**.\n"
                                "2. **Share Folders**: In Google Drive, share your target folder or document with the service account's `client_email`.\n"
                                "3. **Set Defaults**: Use the settings form below to configure `DEFAULT_FOLDER_ID` and template mappings."
                            ),
                        }
                    ],
                },
                {
                    "type": "group",
                    "title": "Available Tools",
                    "components": [
                        {
                            "type": "markdown",
                            "text": (
                                "- `sheets_read(spreadsheet_id, range, max_rows=5000)`: Read tabular data from Google Sheets.\n"
                                "- `sheets_append(spreadsheet_id, range, rows)`: Append rows to Google Sheets.\n"
                                "- `docs_create(title, folder_id, template_id)`: Create or duplicate Google Docs.\n"
                                "- `drive_list(folder_id)`: List files in shared folders.\n"
                                "- `drive_read_text(file_id)`: Export text from Docs, Sheets, or plain text files.\n"
                                "- `workspace_auth_status()`: Verify service account setup."
                            ),
                        }
                    ],
                },
            ],
        },
    )

    def cleanup() -> None:
        pass

    api.on_unload(cleanup)
