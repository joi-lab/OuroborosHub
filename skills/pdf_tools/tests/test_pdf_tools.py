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


def test_password_protected_pdf_reports_encrypted_not_unreadable(tmp_path, capsys):
    pypdf = pytest.importorskip("pypdf")
    target = tmp_path / "locked.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("secret")
    with target.open("wb") as handle:
        writer.write(handle)

    read_pdf = _load("read_pdf")
    with pytest.raises(SystemExit):
        read_pdf.main([str(target)])
    assert json.loads(capsys.readouterr().out)["status"] == "encrypted"


def test_writes_are_confined_to_the_state_dir_outputs(tmp_path, monkeypatch, capsys):
    common = _load("pdf_common")
    data = tmp_path / "data"
    state = data / "state" / "skills" / "pdf_tools"
    monkeypatch.setenv("OUROBOROS_SKILL_STATE_DIR", str(state))

    # A bare file name lands under <state>/outputs/ and the dir is created.
    resolved = common.resolve_output_path("report.pdf", ".pdf")
    assert resolved == common.state_dir() / "outputs" / "report.pdf"
    assert resolved.parent.is_dir()

    # An absolute path in the system temp dir is refused for writing...
    with pytest.raises(SystemExit):
        common.resolve_output_path(str(tmp_path / "evil.pdf"), ".pdf")
    assert json.loads(capsys.readouterr().out)["status"] == "output_outside_state_dir"

    # ...and so is a task drive, even though it stays readable.
    drive = data / "task_drives" / "t1"
    with pytest.raises(SystemExit):
        common.resolve_output_path(str(drive / "out.pdf"), ".pdf")
    assert json.loads(capsys.readouterr().out)["status"] == "output_outside_state_dir"
    drive.mkdir(parents=True)
    readable = drive / "in.txt"
    readable.write_text("x", encoding="utf-8")
    assert common.resolve_path(str(readable), must_exist=True) == readable.resolve()

    # Traversal cannot escape outputs/: "--out .." would resolve to the state
    # dir itself and the suffix is applied BEFORE the containment check, so a
    # sibling "<state>.pdf" is refused instead of written.
    with pytest.raises(SystemExit):
        common.resolve_output_path("..", ".pdf")
    assert json.loads(capsys.readouterr().out)["status"] == "output_outside_state_dir"

    # "--out ." resolves to outputs/ itself and is refused, not written as
    # "<state>/outputs.pdf".
    with pytest.raises(SystemExit):
        common.resolve_output_path(".", ".pdf")
    assert json.loads(capsys.readouterr().out)["status"] == "output_outside_state_dir"

    # An absolute path inside the state dir but OUTSIDE outputs/ is refused:
    # the documented contract is "always under outputs/".
    with pytest.raises(SystemExit):
        common.resolve_output_path(str(common.state_dir() / "loose.pdf"), ".pdf")
    assert json.loads(capsys.readouterr().out)["status"] == "output_outside_state_dir"

    # A missing suffix is normalized under outputs/ rather than refused.
    assert (
        common.resolve_output_path("plain", ".pdf")
        == common.state_dir() / "outputs" / "plain.pdf"
    )

    # An uppercase suffix counts as the required suffix (kept verbatim) and
    # cannot smuggle the path outside outputs/ either.
    assert (
        common.resolve_output_path("REPORT.PDF", ".pdf")
        == common.state_dir() / "outputs" / "REPORT.PDF"
    )
    with pytest.raises(SystemExit):
        common.resolve_output_path("../EVIL.PDF", ".pdf")
    assert json.loads(capsys.readouterr().out)["status"] == "output_outside_state_dir"
