---
name: google-workspace
version: 0.2.0
type: extension
entry: plugin.py
runtime: python3
plugin_api: "2.0"
description: "Universal Google Workspace integration for Google Sheets, Docs, and Drive via explicit OAuth or Service Account authentication."
permissions:
  - tool
  - read_settings
  - widget
  - route
  - net
env_from_settings:
  - GOOGLE_SERVICE_ACCOUNT_JSON
  - GOOGLE_OAUTH_ACCESS_TOKEN
requires:
  - "httpx>=0.24.0"
  - "cryptography>=41.0.0"
install:
  - "httpx>=0.24.0"
  - "cryptography>=41.0.0"
dependencies:
  - "httpx>=0.24.0"
  - "cryptography>=41.0.0"
timeout_sec: 60
---

# Google Workspace Skill

Universal, standalone extension skill providing Google Sheets, Docs, and Drive integration using explicit OAuth bearer or Service Account credentials.

## Features
- **Google Sheets**: Discover workbook metadata and tabs, read/update/append ranges, and apply structural batch updates.
- **Google Docs**: Structured read, batch update with readback, and create new blank/template documents.
- **Google Drive**: Search/list with pagination and Shared Drive options; upload/download/export bounded binary artifacts.
- **Settings & UI Tab**: Configure default working folders and template mappings.

Google Sheets CSV export in `drive_read_text` contains only the first tab;
`export_scope` reports that coverage independently from character `truncated`.
Use `sheets_info` to discover tabs and `sheets_read` for their explicit ranges.
Metadata omitted by Google (such as owners for shared-drive files) remains absent.

## Authentication
Configure either `GOOGLE_SERVICE_ACCOUNT_JSON` or an explicit `GOOGLE_OAUTH_ACCESS_TOKEN` in Settings. Share specific Google Drive folders or files with the service account email. The authentication status probe can verify an explicitly supplied delegation subject; operational tools use the explicitly selected OAuth or service-account route and never infer a subject.
