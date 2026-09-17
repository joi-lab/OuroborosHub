---
name: confluence
description: Read, search and author Confluence Cloud pages, comments and attachments using an existing personal or scoped API token.
version: 0.1.1
type: extension
entry: plugin.py
plugin_api: "2.0"
runtime: python3
permissions: [net, read_settings, tool]
env_from_settings: [CONFLUENCE_SITE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN, CONFLUENCE_AUTH_MODE, CONFLUENCE_CLOUD_ID]
dependencies: ["httpx>=0.27.0"]
timeout_sec: 180
when_to_use: Read or maintain documents in Confluence Cloud, find knowledge with CQL, inspect discussion threads, or transfer attachments.
model_experience:
  what_model_sees: Twelve ordinary tools for connection checks, spaces, pages, CQL, comments and attachments. Full page bodies use Confluence storage XHTML.
  token_effect: Fixed tool schemas while enabled; requested page bodies and result pages add their actual content, without silent local truncation.
---

# Confluence Cloud

A normal extension over Atlassian's APIs. It has no background process,
separate memory, automatic content selection, login flow or custom dashboard.
The existing account's permissions and token scopes determine access.

## Configure

Add the following settings in the host's Secrets/settings surface and grant
them to the reviewed skill before enabling it:

| Setting | Value |
| --- | --- |
| `CONFLUENCE_SITE_URL` | Site origin, for example `https://example.atlassian.net`; optional trailing `/wiki` is accepted. |
| `CONFLUENCE_EMAIL` | Atlassian account email for Basic authentication. Required in `personal` and `scoped` modes. |
| `CONFLUENCE_API_TOKEN` | Existing Atlassian API token; never place it in a skill file or tool argument. |
| `CONFLUENCE_AUTH_MODE` | `personal` (default), `scoped`, or `scoped_bearer`. Choose explicitly; the skill does not infer a mode from the token prefix. |
| `CONFLUENCE_CLOUD_ID` | Optional Cloud ID in scoped modes. When absent, resolve it without credentials from the site's `/_edge/tenant_info`. |

`personal` uses Basic email/token authentication directly against the configured
site. `scoped` uses Basic email/token authentication at
`https://api.atlassian.com/ex/confluence/<cloudId>`. `scoped_bearer` sends the
token as Bearer at that same gateway for an existing credential issued for that
authentication method; email is not required. A token valid for another
Atlassian product or a different scope set is not necessarily valid for
Confluence. The extension never creates accounts, grants scopes or exchanges
credentials between products.

Run `test_connection` with an optional known `page_id`. Its `checks.identity`
reports the returned actor when available; `checks.spaces` and `checks.page`
test resource access separately. A 401/403 can mean missing scopes or permission,
so the provider's message is retained instead of declaring every failure an
invalid token. An empty list only proves that the request worked, not that a
particular page is accessible. Writes are never implied by a read probe.

## Use

- `list_spaces` returns numeric space IDs and keys; `list_pages` and
  `create_page` take the numeric ID, not the key. CQL can use the key, such as
  `type=page AND space="DOCS" AND title~"guide"`.
- `search`, `list_spaces`, `list_pages`, `list_comments`, and `list_attachments`
  fetch one API page per call. `complete=false` means more results remain;
  repeat the same tool with its `next_url` and the original arguments.
  Pagination links cannot change the configured site, tenant or resource.
- `get_page` returns the full provider page record and unmodified
  `body.storage.value`, version, ID and browser URL. Search excerpts and listing
  metadata are not a substitute for reading the page. Numeric page IDs and
  normal same-site `/spaces/KEY/pages/ID/...` or `viewpage.action?pageId=ID`
  URLs are accepted. Tiny links must first be resolved to a numeric page ID.
- `create_page` and `update_page` accept complete storage XHTML, for example
  `<h2>Overview</h2><p>A short explanation.</p>`, rather than Markdown. Both
  support `status="draft"` as well as `current`. Preserve existing macros and
  meaningful content when constructing a replacement body. `update_page`
  requires the version actually read as `expected_version` and checks it before
  writing. Published pages send the next version and detect concurrent version
  conflicts. Drafts must send fixed revision `1`: their version does not advance,
  so this precheck cannot provide atomic protection against concurrent draft
  edits. A conflict requires a fresh read and deliberate reconciliation; no
  update is blindly retried. Confluence's own current/draft reconciliation still
  applies when publishing or editing a published page.
- `list_comments` returns root footer or inline comments. To read replies,
  call it with each `parent_comment_id`. `add_comment` creates a footer comment
  on a page or replies to one footer comment; specify exactly one target.
- `upload_attachment` creates an attachment from an explicitly supplied local
  file; it does not replace same-named attachments. `download_attachment` stores
  a complete file in a unique `state_dir/jobs/<id>/output/` directory and returns
  its path, byte count and SHA-256. Pass the original filename if needed.
  Download redirects may reach a signed HTTPS CDN URL, but credentials and
  cookies never follow an off-origin hop. Partial files are removed on failure.

All actions are synchronous, bounded by the tool timeout, and return JSON with
`ok` or a structured `error` including the HTTP status and `Retry-After` when
available. There are no automatic HTTP retries. After a write returns
`outcome="unknown"` (for example a disconnect after submission), inspect the
page/comment/attachment list before deciding whether to send a new write.
Credentials and their Basic/Bearer encodings are redacted from error messages.

## API references

- [Personal Basic authentication](https://developer.atlassian.com/cloud/confluence/basic-auth-for-rest-apis/)
- [Scoped API tokens](https://support.atlassian.com/confluence/kb/scoped-api-tokens-in-confluence-cloud/)
- [Pages, versions and storage bodies](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/)
- [CQL search](https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-search/)
- [Comments](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-comment/)
- [Attachment upload and download](https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-content---attachments/)
