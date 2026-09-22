---
name: google-workspace
version: 0.2.1
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
  - GOOGLE_OAUTH_CLIENT_ID
  - GOOGLE_OAUTH_CLIENT_SECRET
  - GOOGLE_OAUTH_REFRESH_TOKEN
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

Universal, standalone extension skill providing Google Sheets, Docs, and Drive integration using renewable user OAuth or Service Account credentials.

## Features
- **Google Sheets**: Discover workbook metadata and tabs, read/update/append ranges, and apply structural batch updates.
- **Google Docs**: Structured read, batch update with readback, and create new blank/template documents.
- **Google Drive**: Search/list with pagination and Shared Drive options; upload/download/export bounded binary artifacts.
- **General API**: `workspace_request` accepts provider-shaped requests across Drive v3, Docs v1 and Sheets v4, sharing the existing client and credentials.
- **Settings & UI Tab**: Configure default working folders and template mappings.

Google Sheets CSV export in `drive_read_text` contains only the first tab;
`export_scope` reports that coverage independently from character `truncated`.
Use `sheets_info` to discover tabs and `sheets_read` for their explicit ranges.
Metadata omitted by Google (such as owners for shared-drive files) remains absent.

## Authentication
For persistent user access, configure and grant `GOOGLE_OAUTH_CLIENT_ID`,
`GOOGLE_OAUTH_CLIENT_SECRET` and `GOOGLE_OAUTH_REFRESH_TOKEN` in Settings → Secrets,
then pass `auth_mode="oauth"`. Obtain the refresh token through an owner-approved
Google OAuth consent flow with offline access; this skill does not start login.
`GOOGLE_OAUTH_ACCESS_TOKEN` remains optional for short-lived access and is reused
until Google rejects it or a cached refreshed token expires. Renewable tokens are
cached in memory; each isolated tool process can refresh from the granted secrets.

Alternatively grant `GOOGLE_SERVICE_ACCOUNT_JSON` and share resources with that
service account. The default remains `auth_mode="service_account"`; the skill
never silently changes identity or falls back between routes. An explicit
`subject` is supported by the authentication probe for domain-wide delegation.

`workspace_auth_status` performs a Drive user read and returns `configured`,
`verified` and the actual provider `actor`. Success verifies that request only,
not access to a particular file or write permission. Read the intended resource
with the same `auth_mode` to verify its access. Resource ID parameters accept
common Google Docs, Sheets and Drive links as well as bare IDs.
