from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


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


def test_writes_are_confined_to_the_state_dir_outputs(tmp_path, monkeypatch, capsys):
    common = _load("pdf_common")
    data = tmp_path / "data"
    state = data / "state" / "skills" / "pdf_tools"
    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(state))

    # A bare file name lands under <state>/outputs/ and the dir is created.
    resolved = common.resolve_output_path("report.pdf")
    assert resolved == common.state_dir() / "outputs" / "report.pdf"
    assert resolved.parent.is_dir()

    # An absolute path in the system temp dir is refused for writing...
    with pytest.raises(SystemExit):
        common.resolve_output_path(str(tmp_path / "evil.pdf"))
    assert json.loads(capsys.readouterr().out)["status"] == "output_outside_state_dir"

    # ...and so is a task drive, even though it stays readable.
    drive = data / "task_drives" / "t1"
    with pytest.raises(SystemExit):
        common.resolve_output_path(str(drive / "out.pdf"))
    assert json.loads(capsys.readouterr().out)["status"] == "output_outside_state_dir"
    drive.mkdir(parents=True)
    readable = drive / "in.txt"
    readable.write_text("x", encoding="utf-8")
    assert common.resolve_path(str(readable), must_exist=True) == readable.resolve()
