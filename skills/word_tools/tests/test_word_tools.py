from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


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


def test_writes_are_confined_to_the_state_dir_outputs(tmp_path, monkeypatch, capsys):
    common = _load("docx_common")
    data = tmp_path / "data"
    state = data / "state" / "skills" / "word_tools"
    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(state))

    # A bare file name lands under <state>/outputs/ and the dir is created.
    resolved = common.resolve_output_path("report.docx", ".docx")
    assert resolved == common.state_dir() / "outputs" / "report.docx"
    assert resolved.parent.is_dir()

    # An absolute path in the system temp dir is refused for writing...
    with pytest.raises(SystemExit):
        common.resolve_output_path(str(tmp_path / "evil.docx"), ".docx")
    assert json.loads(capsys.readouterr().out)["status"] == "output_outside_state_dir"

    # ...and so is a task drive, even though it stays readable.
    drive = data / "task_drives" / "t1"
    with pytest.raises(SystemExit):
        common.resolve_output_path(str(drive / "out.docx"), ".docx")
    assert json.loads(capsys.readouterr().out)["status"] == "output_outside_state_dir"
    drive.mkdir(parents=True)
    readable = drive / "in.txt"
    readable.write_text("x", encoding="utf-8")
    assert common.resolve_path(str(readable), must_exist=True) == readable.resolve()

    # Traversal cannot escape outputs/: "--out .." would resolve to the state
    # dir itself and the suffix is applied BEFORE the containment check, so a
    # sibling "<state>.docx" is refused instead of written.
    with pytest.raises(SystemExit):
        common.resolve_output_path("..", ".docx")
    assert json.loads(capsys.readouterr().out)["status"] == "output_outside_state_dir"

    # "--out ." resolves to outputs/ itself and is refused, not written as
    # "<state>/outputs.docx".
    with pytest.raises(SystemExit):
        common.resolve_output_path(".", ".docx")
    assert json.loads(capsys.readouterr().out)["status"] == "output_outside_state_dir"

    # An absolute path inside the state dir but OUTSIDE outputs/ is refused:
    # the documented contract is "always under outputs/".
    with pytest.raises(SystemExit):
        common.resolve_output_path(str(common.state_dir() / "loose.docx"), ".docx")
    assert json.loads(capsys.readouterr().out)["status"] == "output_outside_state_dir"

    # A missing suffix is normalized under outputs/ rather than refused.
    assert (
        common.resolve_output_path("plain", ".docx")
        == common.state_dir() / "outputs" / "plain.docx"
    )

    # An uppercase suffix counts as the required suffix (kept verbatim) and
    # cannot smuggle the path outside outputs/ either.
    assert (
        common.resolve_output_path("REPORT.DOCX", ".docx")
        == common.state_dir() / "outputs" / "REPORT.DOCX"
    )
    with pytest.raises(SystemExit):
        common.resolve_output_path("../EVIL.DOCX", ".docx")
    assert json.loads(capsys.readouterr().out)["status"] == "output_outside_state_dir"
