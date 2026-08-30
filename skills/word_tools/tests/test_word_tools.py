from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    if name != "docx_common" and "docx_common" not in sys.modules:
        _load("docx_common")
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_light_markup_preserves_document_structure():
    make_docx = _load("make_docx")
    blocks = make_docx.parse_blocks(
        "# Report\n\nParagraph text.\n\n- item\n\n"
        "| Name | Value |\n| --- | --- |\n| A | 1 |"
    )
    assert [block["kind"] for block in blocks] == [
        "heading",
        "paragraph",
        "list_item",
        "table",
    ]
    assert blocks[-1] == {
        "kind": "table",
        "rows": [["Name", "Value"], ["A", "1"]],
        "header": True,
    }


def test_section_selection_and_localized_heading_styles_remain_supported():
    read_docx = _load("read_docx")
    assert read_docx.parse_selection("3-1", 4) == [1, 2, 3]
    assert read_docx.heading_level("Heading 4") == 4
    assert read_docx.heading_level("\u0417\u0430\u0433\u043e\u043b\u043e\u0432\u043e\u043a 2") == 2
