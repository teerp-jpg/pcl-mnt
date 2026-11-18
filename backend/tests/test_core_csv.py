"""Spreadsheet-safe UTF-8 export; no network or filesystem writes."""

import csv
import io

import pytest

from app.integrations.core import EXPORT_COLUMNS, CSVCoreChangeSink, safe_csv_cell


@pytest.mark.parametrize("value", ["=SUM(A1)", "+1", "-1", "@name", "  =1", "\t=1", "\n1"])
def test_formula_cells_are_escaped(value: str) -> None:
    assert safe_csv_cell(value) == "'" + value


def test_csv_roundtrip_handles_commas_quotes_newlines_and_thai() -> None:
    value = 'รถสมมติ, "ทดสอบ"\nบรรทัดสอง'
    data = CSVCoreChangeSink().export([{"plate_no": value, "brand": "=1+1", "year": None}])
    assert data.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    assert tuple(rows[0]) == EXPORT_COLUMNS
    assert rows[0]["plate_no"] == value
    assert rows[0]["brand"] == "'=1+1" and rows[0]["year"] == ""
    assert safe_csv_cell("70-1234") == "70-1234"
    assert (
        len(list(csv.reader(io.StringIO(CSVCoreChangeSink().export([]).decode("utf-8-sig"))))) == 1
    )
