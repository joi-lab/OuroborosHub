from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    if name != "pdf_common" and "pdf_common" not in sys.modules:
        _load("pdf_common")
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_page_selection_is_sorted_bounded_and_accepts_reverse_ranges():
    read_pdf = _load("read_pdf")
    assert read_pdf.parse_pages("3-1,5", 4) == [1, 2, 3]
    assert read_pdf.parse_pages("", 3) == [1, 2, 3]


def test_inline_markup_is_escaped_before_reportlab_tags_are_added():
    make_pdf = _load("make_pdf")
    assert make_pdf.inline("<tag> **bold** and *italic*") == (
        "&lt;tag&gt; <b>bold</b> and <i>italic</i>"
    )
