#!/usr/bin/env python3
"""Build a PDF from plain or lightly structured text.

Supported light markup: ``#``/``##``/``###`` headings, blank-line separated
paragraphs, ``- ``/``* `` bullets, ``1. `` numbered items, ``---`` rule, plus
inline ``**bold**`` and ``*italic*``. Cyrillic and Latin text both render
correctly because a Unicode TrueType font is registered explicitly.
"""

from __future__ import annotations

import argparse
import html
import re
import sys

import pdf_common as common

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.DOTALL)
_ORDERED_RE = re.compile(r"^(\d+)[.)]\s+(.*)$")


def inline(text: str) -> str:
    """Escape text for reportlab and translate inline emphasis markers."""
    out = html.escape(text, quote=False)
    out = _BOLD_RE.sub(r"<b>\1</b>", out)
    out = _ITALIC_RE.sub(r"<i>\1</i>", out)
    return out


def build_story(raw: str, styles, title: str):
    from reportlab.platypus import HRFlowable, Paragraph, Spacer

    story = []
    if title.strip():
        story.append(Paragraph(inline(title.strip()), styles["PdfTitle"]))
        story.append(Spacer(1, 10))

    paragraph_lines: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph_lines:
            return
        text = " ".join(line.strip() for line in paragraph_lines if line.strip())
        paragraph_lines.clear()
        if text:
            story.append(Paragraph(inline(text), styles["PdfBody"]))

    for raw_line in raw.replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            continue
        if set(stripped) <= {"-", "_", "*"} and len(stripped) >= 3:
            flush_paragraph()
            story.append(Spacer(1, 6))
            story.append(HRFlowable(width="100%", thickness=0.6, color="#999999"))
            story.append(Spacer(1, 6))
            continue
        if stripped.startswith("#"):
            flush_paragraph()
            level = len(stripped) - len(stripped.lstrip("#"))
            heading = stripped[level:].strip()
            style = styles["PdfH" + str(min(max(level, 1), 3))]
            story.append(Paragraph(inline(heading), style))
            continue
        if stripped[:2] in ("- ", "* "):
            flush_paragraph()
            story.append(Paragraph(inline(stripped[2:].strip()),
                                   styles["PdfBullet"], bulletText="\u2022"))
            continue
        ordered = _ORDERED_RE.match(stripped)
        if ordered:
            flush_paragraph()
            story.append(Paragraph(inline(ordered.group(2).strip()),
                                   styles["PdfBullet"], bulletText=ordered.group(1) + "."))
            continue
        paragraph_lines.append(line)

    flush_paragraph()
    if len(story) <= (1 if title.strip() else 0):
        common.fail("nothing to render: the input text is empty")
    return story


def make_styles(font_name: str, bold_name: str):
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="PdfTitle", fontName=bold_name, fontSize=19,
                              leading=24, spaceAfter=8))
    styles.add(ParagraphStyle(name="PdfH1", fontName=bold_name, fontSize=16,
                              leading=21, spaceBefore=12, spaceAfter=6))
    styles.add(ParagraphStyle(name="PdfH2", fontName=bold_name, fontSize=13.5,
                              leading=18, spaceBefore=10, spaceAfter=5))
    styles.add(ParagraphStyle(name="PdfH3", fontName=bold_name, fontSize=11.5,
                              leading=16, spaceBefore=8, spaceAfter=4))
    styles.add(ParagraphStyle(name="PdfBody", fontName=font_name, fontSize=10.5,
                              leading=15, spaceAfter=6))
    styles.add(ParagraphStyle(name="PdfBullet", fontName=font_name, fontSize=10.5,
                              leading=15, leftIndent=16, bulletIndent=4, spaceAfter=3))
    return styles


def register_fonts(regular, bold):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font_name = "OuroborosPdfBody"
    bold_name = font_name
    try:
        pdfmetrics.registerFont(TTFont(font_name, str(regular)))
        if bold is not None:
            bold_name = "OuroborosPdfBold"
            pdfmetrics.registerFont(TTFont(bold_name, str(bold)))
    except Exception as exc:  # noqa: BLE001 — a broken font file must be explicit
        common.fail(f"cannot register font {regular}: {exc}", status="font_missing", code=4)
    from reportlab.pdfbase.pdfmetrics import registerFontFamily

    registerFontFamily(font_name, normal=font_name, bold=bold_name,
                       italic=font_name, boldItalic=bold_name)
    return font_name, bold_name


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build a PDF from text.")
    parser.add_argument("--out", required=True,
                        help="output PDF file name, created under the skill "
                             "state dir outputs/")
    parser.add_argument("--in", dest="src", default="", help="text/markdown source file")
    parser.add_argument("--text", default="", help="inline text (alternative to --in)")
    parser.add_argument("--title", default="", help="document title rendered on the first page")
    parser.add_argument("--font", default="", help="explicit Unicode TTF path")
    parser.add_argument("--page-size", default="a4", choices=["a4", "letter"])
    args = parser.parse_args(argv)

    if bool(args.src.strip()) == bool(args.text.strip()):
        common.fail("provide exactly one of --in <file> or --text <string>")

    if args.src.strip():
        source = common.resolve_path(args.src, must_exist=True)
        try:
            raw = source.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            common.fail(f"cannot read source text: {exc}")
    else:
        raw = args.text

    out_path = common.resolve_output_path(args.out, ".pdf")

    common.require("reportlab", "reportlab")
    regular, bold = common.find_font(args.font)
    font_name, bold_name = register_fonts(regular, bold)

    from reportlab.lib.pagesizes import A4, letter
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate

    styles = make_styles(font_name, bold_name)
    story = build_story(raw, styles, args.title)
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4 if args.page_size == "a4" else letter,
        title=args.title or out_path.stem,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
    )
    try:
        doc.build(story)
    except Exception as exc:  # noqa: BLE001 — report layout failures honestly
        common.fail(f"failed to build PDF: {exc}")

    common.emit({
        "status": "ok",
        "file": str(out_path),
        "name": out_path.name,
        "pages": getattr(doc, "page", 0),
        "bytes": out_path.stat().st_size,
        "font": str(regular),
        "bold_font": str(bold) if bold else None,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
