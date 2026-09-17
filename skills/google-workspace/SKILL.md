---
name: google-workspace
version: 0.1.1
type: extension
entry: plugin.py
runtime: python3
plugin_api: "2.0"
description: "Universal Google Workspace integration for Google Sheets, Docs, and Drive via Service Account authentication."
permissions:
  - tool
  - read_settings
  - widget
  - route
  - net
env_from_settings:
  - GOOGLE_SERVICE_ACCOUNT_JSON
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

Universal, standalone extension skill providing minimal Google Sheets, Google Docs, and Google Drive integration using Service Account credentials.

## Features
- **Google Sheets**: Read cell ranges and append rows.
- **Google Docs**: Create new blank documents or duplicate from template documents.
- **Google Drive**: List files in shared folders and export document text.
- **Settings & UI Tab**: Configure default working folders and template mappings.

## Authentication
Configure `GOOGLE_SERVICE_ACCOUNT_JSON` in Settings. Share specific Google Drive folders or files with the service account email.
