#!/usr/bin/env python3
"""Extract text, logical structure and simple tables from a .docx document.

The output is grouped into sections by heading so the agent can answer about the
document by meaning instead of dumping one flat blob. Legacy binary ``.doc`` is
refused explicitly (``status="legacy_doc_format"``) rather than returning an
empty result that could be mistaken for an empty document.
"""

from __future__ import annotations

import argparse
import sys

import docx_common as common

_HEADING_STYLES = ("heading", "\u0437\u0430\u0433\u043e\u043b\u043e\u0432\u043e\u043a")
_BULLET_STYLES = (
    "list bullet",
    "list paragraph",
    "\u043c\u0430\u0440\u043a\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u044b\u0439 \u0441\u043f\u0438\u0441\u043e\u043a",
)
_NUMBER_STYLES = (
    "list number",
    "\u043d\u0443\u043c\u0435\u0440\u043e\u0432\u0430\u043d\u043d\u044b\u0439 \u0441\u043f\u0438\u0441\u043e\u043a",
)


def parse_selection(spec: str, total: int) -> list[int]:
    """Parse a 1-based selection such as ``1-3,7`` into sorted indexes."""
    if not spec.strip():
        return list(range(1, total + 1))
    wanted: set[int] = set()
    for chunk in spec.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            start_raw, _, end_raw = chunk.partition("-")
            try:
                start, end = int(start_raw), int(end_raw)
            except ValueError:
                common.fail(f"bad section range: {chunk}")
            if start > end:
                start, end = end, start
            wanted.update(range(start, end + 1))
        else:
            try:
                wanted.add(int(chunk))
            except ValueError:
                common.fail(f"bad section number: {chunk}")
    picked = sorted(index for index in wanted if 1 <= index <= total)
    if not picked:
        common.fail(f"section selection is empty for a document with {total} section(s)")
    return picked


def heading_level(style_name: str) -> int | None:
    """Return 1..6 for a heading-like paragraph style, else None."""
    low = style_name.strip().lower()
    if low in ("title", "\u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435"):
        return 1
    for prefix in _HEADING_STYLES:
        if low.startswith(prefix):
            tail = low[len(prefix):].strip()
            if not tail:
                return 1
            try:
                return max(1, min(6, int(tail)))
            except ValueError:
                return 1
    return None


def classify_list(style_name: str) -> str | None:
    low = style_name.strip().lower()
    if any(low.startswith(prefix) for prefix in _NUMBER_STYLES):
        return "numbered"
    if any(low.startswith(prefix) for prefix in _BULLET_STYLES):
        return "bullet"
    return None


def read_blocks(document, include_tables: bool) -> tuple[list[dict], int, int]:
    """Walk the document body in reading order and return (blocks, paras, tables)."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    blocks: list[dict] = []
    paragraph_count = 0
    table_count = 0
    for child in document.element.body.iterchildren():
        tag = str(child.tag)
        if tag.endswith("}p"):
            paragraph = Paragraph(child, document)
            text = (paragraph.text or "").replace("\r\n", "\n").strip()
            paragraph_count += 1
            if not text:
                continue
            try:
                style_name = paragraph.style.name or ""
            except Exception:  # noqa: BLE001 — a broken style must not stop the read
                style_name = ""
            level = heading_level(style_name)
            if level is not None:
                blocks.append({"kind": "heading", "level": level, "text": text})
                continue
            list_kind = classify_list(style_name)
            if list_kind is not None:
                blocks.append({"kind": "list_item", "list": list_kind, "text": text})
                continue
            blocks.append({"kind": "paragraph", "text": text})
        elif tag.endswith("}tbl"):
            table_count += 1
            if not include_tables:
                continue
            table = Table(child, document)
            rows: list[list[str]] = []
            for row in table.rows:
                rows.append([(cell.text or "").strip() for cell in row.cells])
            if any(any(cell for cell in row) for row in rows):
                blocks.append({"kind": "table", "rows": rows})
    return blocks, paragraph_count, table_count


def block_chars(block: dict) -> int:
    if block["kind"] == "table":
        return sum(len(cell) for row in block["rows"] for cell in row)
    return len(block["text"])


def group_sections(blocks: list[dict]) -> list[dict]:
    """Split the flat block list into heading-delimited sections."""
    sections: list[dict] = []
    current = {"index": 1, "heading": "", "level": 0, "blocks": []}
    for block in blocks:
        if block["kind"] == "heading":
            if not current["heading"] and not current["blocks"]:
                # Leading heading of the very first (still empty) section.
                current["heading"] = block["text"]
                current["level"] = block["level"]
                continue
            # Any later heading starts a new section, even back-to-back headings.
            sections.append(current)
            current = {
                "index": len(sections) + 1,
                "heading": block["text"],
                "level": block["level"],
                "blocks": [],
            }
            continue
        current["blocks"].append(block)
    if current["blocks"] or current["heading"]:
        sections.append(current)
    for section in sections:
        section["chars"] = sum(block_chars(block) for block in section["blocks"])
    return sections


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Read a .docx document.")
    parser.add_argument("path", help=".docx file inside the Ouroboros working directories")
    parser.add_argument("--max-chars", type=int, default=200_000,
                        help="total character budget for extracted text (default 200000)")
    parser.add_argument("--sections", default="",
                        help="section selection, e.g. 1-3,7 (default: all)")
    parser.add_argument("--outline", action="store_true",
                        help="report the section map and counts only, without body text")
    parser.add_argument("--no-tables", action="store_true", help="skip tables")
    args = parser.parse_args(argv)

    path = common.resolve_path(args.path, must_exist=True)
    common.guard_docx_container(path)
    common.require("docx", "python-docx")

    import docx

    try:
        document = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001 — surface any parser failure honestly
        common.fail(f"cannot read .docx: {exc}", status="unreadable")

    try:
        blocks, paragraph_count, table_count = read_blocks(document, not args.no_tables)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — a malformed body is a real failure
        common.fail(f"cannot walk document body: {exc}", status="unreadable")

    core = {}
    try:
        props = document.core_properties
        for key in ("title", "author", "subject"):
            value = getattr(props, key, None)
            if value:
                core[key] = str(value)
    except Exception:  # noqa: BLE001 — metadata is a nicety, never a failure
        core = {}

    sections = group_sections(blocks)
    outline = [
        {"index": section["index"], "heading": section["heading"],
         "level": section["level"], "chars": section["chars"],
         "blocks": len(section["blocks"])}
        for section in sections
    ]
    base = {
        "status": "ok" if blocks else "empty_document",
        "file": str(path),
        "name": path.name,
        "bytes": path.stat().st_size,
        "metadata": core,
        "paragraph_count": paragraph_count,
        "table_count": table_count,
        "section_count": len(sections),
        "outline": outline,
    }

    if not blocks:
        base["note"] = (
            "The document carries no readable paragraphs or tables. Do not invent its "
            "contents: confirm with the user that the file is the intended one, or ask "
            "for a version with text (content may live in images, text boxes, headers "
            "or footers, which this skill does not read)."
        )
        common.emit(base)
        return 0

    if args.outline:
        base["truncated"] = False
        base["outline_only"] = True
        common.emit(base)
        return 0

    picked = parse_selection(args.sections, len(sections)) if sections else []
    budget = max(1_000, int(args.max_chars))
    used = 0
    truncated = False
    payload_sections: list[dict] = []
    for index in picked:
        section = sections[index - 1]
        kept: list[dict] = []
        for block in section["blocks"]:
            remaining = budget - used
            if remaining <= 0:
                truncated = True
                break
            if block["kind"] == "table":
                size = block_chars(block)
                if size > remaining:
                    truncated = True
                    break
                used += size
                kept.append(block)
                continue
            text = block["text"]
            if len(text) > remaining:
                text = text[:remaining]
                truncated = True
            used += len(text)
            kept.append({**block, "text": text})
        payload_sections.append({
            "index": section["index"],
            "heading": section["heading"],
            "level": section["level"],
            "blocks": kept,
            "blocks_omitted": len(section["blocks"]) - len(kept),
        })
        if truncated:
            break

    base["sections_read"] = [section["index"] for section in payload_sections]
    base["sections_omitted"] = len(picked) - len(payload_sections)
    base["total_chars"] = used
    base["truncated"] = truncated
    base["sections"] = payload_sections
    if truncated:
        base["note"] = (
            "Output hit the character budget, so part of the document is missing. "
            "Read the remaining sections with --sections, or raise --max-chars."
        )
    common.emit(base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
