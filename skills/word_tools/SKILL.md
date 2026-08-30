---
name: word_tools
description: Read structured DOCX documents and build new Word documents from lightly structured text.
version: 0.1.0
type: script
runtime: python3
permissions: [fs]
when_to_use: The user attached a Word document and asks to read, summarize, search, compare, or revise its contents, or asks to create a Word or DOCX file.
timeout_sec: 120
scripts:
  - name: read_docx.py
    description: Extract headings, paragraphs, lists, and simple tables from a DOCX document.
  - name: make_docx.py
    description: Build a DOCX document from plain or lightly structured text.
install_specs:
  - kind: pip
    package: python-docx
---

# Word tools

This skill can read Word documents and build new `.docx` files.

## Read a document

`skill_exec(skill="word_tools", script="scripts/read_docx.py", args=["<file path>"])`

Useful arguments include `--max-chars N`, `--sections 1-3`, `--outline`, and
`--no-tables`. The script returns JSON with file metadata, paragraph and table
counts, heading-delimited sections, simple tables, total characters, and a
`truncated` flag. Confirm the document and its structure briefly, then answer
from its meaning; do not dump all raw text unless explicitly requested.

Legacy binary `.doc` files are refused with `status: "legacy_doc_format"`.
Tell the user to re-save the document as `.docx`; do not treat an unsupported
file as an empty document.

## Build a document

`skill_exec(skill="word_tools", script="scripts/make_docx.py", args=["--in", "<text file>", "--out", "report.docx", "--title", "Report title"])`

Use `--text` instead of `--in` to pass text directly. Supported light markup
includes headings, paragraphs, bullet and numbered lists, simple pipe tables,
rules, bold, and italic text. Preserve the semantic structure of the answer:
headings remain headings, lists remain lists, and tables remain tables.

Ask a clarifying question only when the requested structure or scope is
ambiguous; otherwise create the file directly.

## Output and delivery convention

Generated files are always written inside the skill state directory under
`outputs/`. Pass a bare file name as `--out` (e.g. `--out report.docx`); a path
outside the state directory is refused with `status: "output_outside_state_dir"`.
The returned JSON carries the absolute path of the created file in `file`.
After the file is created, deliver it to the user yourself with `send_file`
(host tooling) — the skill only creates the file. NEVER construct download URLs.

## Boundaries

- A request for Word output must produce DOCX, not a substituted PDF.
- Macros, tracked changes, complex corporate templates, headers and footers,
  embedded images, and merging into an existing design are out of scope.
- Reading is allowed from Ouroboros working directories: uploads, task results
  and drives, Deliverables, the skill state directory, and the system temporary
  directory. Writing is confined to the skill state directory (`outputs/`).
