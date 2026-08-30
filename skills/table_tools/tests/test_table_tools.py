from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


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

    # An untruncated scan still reports an exact total.
    full = common.load_table(csv_path, max_rows=100)
    assert full["truncated"] is False
    assert full["total_rows"] == full["scanned_rows"] == 70
