from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    if name != "table_common" and "table_common" not in sys.modules:
        _load("table_common")
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_header_normalization_and_numeric_classification_are_explicit():
    common = _load("table_common")
    names, notes = common.normalize_headers(["Name", "", "Name"])
    assert names == ["Name", "column_2", "Name_2"]
    assert len(notes) == 2
    assert common.to_number("1 234,56") == 1234.56
    assert common.classify("") == ("empty", None)


def test_filters_and_aggregates_report_excluded_values():
    query = _load("query_table")
    condition = query.parse_condition("Region:contains:west")
    assert query.matches({"Region": "Northwest"}, condition) is True
    result = query.aggregate(
        [{"Revenue": "10"}, {"Revenue": "bad"}, {"Revenue": ""}],
        ["count", "sum", "mean"],
        "Revenue",
    )
    assert result["count"] == 3
    assert result["sum"] == 10.0
    assert result["mean"] == 10.0
    assert result["non_numeric_cells_skipped"] == 1
    assert result["empty_cells"] == 1
    assert "partial" not in result
    partial = query.aggregate([{"Revenue": "10"}], ["sum"], "Revenue", partial=True)
    assert partial["partial"] is True


def test_bounded_scan_stops_reading_and_reports_truncation_honestly(tmp_path):
    common = _load("table_common")

    csv_path = tmp_path / "big.csv"
    lines = ["id,value"] + [f"{i},{i * 10}" for i in range(70)]
    csv_path.write_text("\n".join(lines), encoding="utf-8")

    table = common.load_table(csv_path, max_rows=1)
    assert table["truncated"] is True
    assert table["scanned_rows"] == 1
    assert table["total_rows"] is None
    assert len(table["rows"]) == 1

    # The row iterator is abandoned right after the truncation probe: at most
    # the header, the stored row and one probe row are ever pulled.
    pulled = 0

    def counting_rows():
        nonlocal pulled
        for index in range(70):
            pulled += 1
            yield ["head_a", "head_b"] if index == 0 else [str(index), "x"]

    scanned = common._scan_rows(counting_rows(), 1, "empty")
    assert scanned["truncated"] is True
    assert scanned["total_rows"] is None
    assert len(scanned["rows"]) == 1
    assert pulled <= 3

    # Blank rows cannot make the scan walk the whole file: physical pulls are
    # bounded by the fixed blank allowance even when every tail row is empty.
    blank_pulled = 0

    def blank_tail_rows():
        nonlocal blank_pulled
        blank_pulled += 1
        yield ["head_a", "head_b"]
        blank_pulled += 1
        yield ["1", "x"]
        for _ in range(5000):
            blank_pulled += 1
            yield ["", ""]

    blank = common._scan_rows(blank_tail_rows(), 1, "empty")
    assert blank["truncated"] is True
    assert blank["total_rows"] is None
    assert blank_pulled <= common.BLANK_SCAN_ALLOWANCE + 5

    # A modest blank PREFIX within the allowance still finds the header.
    def blank_prefix_rows():
        for _ in range(150):
            yield ["", ""]
        yield ["head_a", "head_b"]
        yield ["1", "x"]
        yield ["2", "y"]

    prefixed = common._scan_rows(blank_prefix_rows(), 10, "empty")
    assert prefixed["header_row"] == ["head_a", "head_b"]
    assert prefixed["truncated"] is False
    assert prefixed["total_rows"] == 2

    # An untruncated scan still reports an exact total.
    full = common.load_table(csv_path, max_rows=100)
    assert full["truncated"] is False
    assert full["total_rows"] == full["scanned_rows"] == 70


def test_header_beyond_scan_budget_is_reported_as_unscanned_not_empty(capsys):
    common = _load("table_common")

    # A header that sits past the physical budget is a refusal to guess, not
    # an "empty table": the scanner never saw the rest of the file.
    def late_header_rows():
        for _ in range(300):
            yield ["", ""]
        yield ["head_a", "head_b"]
        yield ["1", "x"]

    with pytest.raises(SystemExit):
        common._scan_rows(late_header_rows(), 10, "empty")
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "header_not_found_within_scan_budget"
    assert "not scanned" in payload["error"]
