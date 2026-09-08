---
name: pdf_tools
description: Read PDF documents page by page and build new PDF files from text or structured answers with Unicode-capable fonts.
version: 0.1.0
type: script
runtime: python3
permissions: [fs]
when_to_use: The user attached a PDF and asks to read, summarize, search, or compare it, or asks to produce or export a PDF.
timeout_sec: 120
scripts:
  - name: read_pdf.py
    description: Extract the readable text layer per page and report honestly when a document has no text layer.
  - name: make_pdf.py
    description: Build a PDF from plain or lightly structured text with Unicode-capable fonts.
install_specs:
  - kind: pip
    package: pypdf
  - kind: pip
    package: reportlab
---

# PDF tools

This skill can read PDFs and build new PDFs.

## Read a document

`skill_exec(skill="pdf_tools", script="scripts/read_pdf.py", args=["<file path>"])`

Useful arguments include `--pages 1-3,7` to select pages, `--max-chars N` to
bound extracted text, and `--meta-only` to return only the filename and page
count.

The script returns JSON containing the file name, page count, page-level text,
total extracted characters, and truncation status. Briefly confirm what was
read before answering from the document contents.

If a PDF has no text layer, the script returns `status: "no_text_layer"`.
Do not guess at missing contents. Explain that the document is probably a scan
and offer image inspection (`view_image` or `vlm_query`) or a dedicated OCR
skill if one is installed.

## Build a document

`skill_exec(skill="pdf_tools", script="scripts/make_pdf.py", args=["--in", "<text file>", "--out", "report.pdf", "--title", "Report title"])`

Use `--text` instead of `--in` to pass text directly. Supported light markup
includes `#`/`##`/`###` headings, blank-line paragraphs, `- ` bullets, `1. `
numbered items, `---` rules, `**bold**`, and `*italic*`. Unicode TrueType fonts
support both Cyrillic and Latin text.

Ask a clarifying question only when the requested structure or scope is
ambiguous; otherwise create the file directly.

## Output and delivery convention

Generated files are always written inside the skill state directory under
`outputs/`. Pass a bare file name as `--out` (e.g. `--out report.pdf`); a path
outside the state directory is refused with `status: "output_outside_state_dir"`.
The returned JSON carries the absolute path of the created file in `file`.
After the file is created, deliver it to the user yourself with `send_file`
(host tooling) — the skill only creates the file. NEVER construct download URLs.

## Boundaries

- The skill reads and creates PDFs. It does not edit existing PDF forms,
  annotations, complex layouts, or embedded content in place.
- Reading is allowed from Ouroboros working directories: uploads, task results
  and drives, Deliverables, the skill state directory, and the system temporary
  directory. Writing is confined to the skill state directory (`outputs/`).
- If no Unicode font is available, the script reports that explicitly instead
  of producing missing-glyph boxes. Use `--font` to provide a font explicitly.
