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
