# Google Workspace Skill (`google-workspace`)

A universal, standalone, public extension skill for Ouroboros providing Google Sheets, Google Docs, and Google Drive integration via Google Cloud Service Accounts.

---

## Features

- **Google Sheets (`sheets_info`, `sheets_read`, `sheets_append`)**: Discover workbook and tab metadata without reading cells; read displayed values, calculated values or formulas from selected ranges; append rows to tables.
- **Google Docs (`docs_create`)**: Create new blank documents or duplicate from template Google Docs into specified Drive folders.
- **Google Drive (`drive_list`, `drive_read_text`)**: List shared files and folders; export text from Google Docs (`text/plain`), the first tab of Google Sheets (`text/csv`), and supported plain-text files with bounded stream reading. Preserve available owners, last modifier, version, modified time, browser URL and capabilities.
- **Preflight Check (`workspace_auth_status`)**: Validate service account credentials and OAuth2 connectivity without side effects or leaking token material.
- **Settings & UI Tab**: Configure default working folders and template name mappings.

---

## Architecture & Dependency Rationale

This skill uses **pure REST via `httpx` and `cryptography`** rather than the official `google-api-python-client` / `google-auth` SDKs.

### Why Pure REST?
1. **Minimal Dependencies**: The official Google client libraries pull in 15+ transitive packages (`google-auth`, `google-api-python-client`, `google-auth-httplib2`, `uritemplate`, `cachetools`, `rsa`, `pyasn1`, `pyasn1-modules`, `httplib2`). Pure REST requires only `httpx` and `cryptography`.
2. **Deterministic & Auditable**: All HTTP endpoints, OAuth2 token exchanges, request parameters, and response parsers are explicit in readable Python code without dynamic discovery document overhead.
3. **Instant Startup**: Zero startup lag from dynamic API discovery on initialization.

---

## Setup & Configuration

1. **Create a Google Cloud Service Account**:
   - Enable **Google Sheets API**, **Google Docs API**, and **Google Drive API** in your Google Cloud Console.
   - Create a Service Account and generate/download a **JSON key file**.

2. **Add to Settings**:
   - In Ouroboros, navigate to **Settings → Secrets**.
   - Add a secret named `GOOGLE_SERVICE_ACCOUNT_JSON` with the entire JSON content of your key file.
   - Grant `GOOGLE_SERVICE_ACCOUNT_JSON` to the `google-workspace` skill.

3. **Share Files or Folders**:
   - Copy the `client_email` address from your Service Account JSON (e.g. `my-sa@my-project.iam.gserviceaccount.com`).
   - In Google Drive, share only the specific folders or documents you want the agent to access with this email.

---

## Tool Reference

### `workspace_auth_status()`
- **Description**: Verifies credential structure and performs a test OAuth2 token exchange with Google.
- **Returns**: `{"status": "ready", "client_email": "...", "project_id": "..."}`.

### `sheets_info(spreadsheet_id)`
- **Description**: Read spreadsheet title, locale, timezone, URL, and tab IDs, titles, order, type, visibility and grid properties. Does not fetch cell values or formatting.
- **Returns**: `spreadsheet_id`, `title`, `locale`, `time_zone`, `url`, and `sheets`. Each tab preserves the properties Google returned; non-grid tabs may omit `grid_properties`.
- **Use**: Choose a returned tab title for `sheets_read`. Grid dimensions are allocated rows/columns, not the count of populated records; frozen rows are provider facts, not an inferred header schema.

### `sheets_read(spreadsheet_id, range, max_rows=5000, value_render_option="FORMATTED_VALUE")`
- **Parameters**:
  - `spreadsheet_id` (string, required): Google Spreadsheet ID from URL.
  - `range` (string, required): A1 notation (e.g. `'Sheet1!A1:D50'`, `'Summary!A:C'`).
  - `max_rows` (integer, optional): Maximum rows to return (default: 5000).
  - `value_render_option` (string, optional): `FORMATTED_VALUE` (default, displayed calculated values), `UNFORMATTED_VALUE` (calculated values without display formatting), or `FORMULA` (formulas instead of calculated results).
- **Returns**: JSON object with `range`, `major_dimension`, `value_render_option`, `values`, `row_count`, `original_row_count`, `truncated`.

### `sheets_append(spreadsheet_id, range, rows, value_input_option="USER_ENTERED")`
- **Parameters**:
  - `spreadsheet_id` (string, required): Google Spreadsheet ID.
  - `range` (string, required): A1 notation or table name.
  - `rows` (list of lists, required): Rows of cell data to append.
  - `value_input_option` (string, optional): `'USER_ENTERED'` or `'RAW'`.
- **Returns**: JSON object with `updated_rows`, `updated_columns`, `updated_cells`.

### `docs_create(title, folder_id=None, template_id=None)`
- **Parameters**:
  - `title` (string, required): Document title.
  - `folder_id` (string, optional): Target Google Drive folder ID (or uses configured default).
  - `template_id` (string, optional): Template Document ID or template alias name to copy from.
- **Returns**: JSON object with `document_id`, `title`, `url`, `folder_id`.

### `drive_list(folder_id=None, page_size=50, page_token=None)`
- **Parameters**:
  - `folder_id` (string, optional): Folder ID to list from (or uses configured default).
  - `page_size` (integer, optional): Number of files to return (1-100, default: 50).
  - `page_token` (string, optional): Continuation token.
- **Returns**: JSON object with `files` list containing `id`, `name`, `mime_type`, `url`, `modified_time`.
  Available provider fields also include `owners`, `last_modifying_user`, `version` and `capabilities`. Follow `next_page_token` until it is absent to finish a listing.

### `drive_read_text(file_id, max_chars=100000)`
- **Parameters**:
  - `file_id` (string, required): Google Drive file ID.
  - `max_chars` (integer, optional): Max text character limit (default: 100000).
- **Returns**: JSON object with `file_id`, `name`, `mime_type`, `text`, `length`, `truncated`, `url`, and the same available provider metadata as `drive_list`.
- **Sheets coverage**: CSV contains only the first sheet. `export_scope="first_sheet_only"` and `export_note` disclose this even when `truncated=false`; that flag describes character clipping only. Use `sheets_info` and explicit `sheets_read` ranges for other tabs.

Ownership and capability fields are returned only when Google supplies them.
Shared-drive files have no `owners` field; an absent owner or last modifier is
not an anonymous person or a permission denial. `version` is Google's file
change counter, not a Docs revision ID or an edit precondition. Capability
booleans describe the calling account's access, not a promise that a later write
will succeed.

---

## Known Constraints & Disclosures

1. **Sheets API Range Bounding**:
   - Google Sheets API v4 `spreadsheets.values.get` returns all rows within the requested A1 range in a single response payload (the upstream REST endpoint does not accept a `maxRows` query parameter).
   - In `sheets_read`, client-side row truncation (`max_rows`) is applied immediately upon receiving the response. For very large sheets, callers should supply explicit bounded ranges (e.g. `'Sheet1!A1:Z500'`) rather than unbounded column ranges (`'A:Z'`) to minimize upstream transfer size.
2. **Verification**:
   - Tests use disposable RSA keys and mocked Google responses. They do not certify permissions or API enablement in a particular Google project; run `workspace_auth_status` after setup.
   - Native signing is imported during execution, after extension registration. Loading native cryptography during registration and retaining its classes after isolated-dependency cleanup can cause `Expected instance of hashes.HashAlgorithm` on the first tool call, even when the host and isolated package versions match.
   - Run `python -m pytest -q skills/google-workspace/tests` from the Hub checkout. Set `OUROBOROS_SOURCE_DIR` to an Ouroboros source checkout to include repeated authentication and document reads through the real isolated child loader. This integration test uses temporary state, local test grants, mocked HTTP, and signature verification; it does not change a live installation.

---

## Security Boundaries

- **Strict Access Control**: Service accounts can only view or modify items explicitly shared with their email address.
- **Credential Protection**: The granted `GOOGLE_SERVICE_ACCOUNT_JSON` is never returned in tool responses, logged, or shared.
- **Endpoint Protection**: OAuth2 token exchanges are strictly pinned to verified Google OAuth domains.
- **Zero Core Mutation**: Confined to `data/skills/external/google-workspace/` and its state directory.

## API references

- [Drive file metadata](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)
- [Sheets metadata without grid data](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/get)
- [Values rendering options](https://developers.google.com/workspace/sheets/api/reference/rest/v4/ValueRenderOption)
- [Export formats and first-sheet CSV scope](https://developers.google.com/workspace/drive/api/guides/ref-export-formats)
