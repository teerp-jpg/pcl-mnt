"""Real CSV parsing/limits, configurable headers and canonical normalization."""

from pathlib import Path

import pytest

from app.config import ConfigurationError, Settings
from app.errors import APIError
from app.integrations.core import CORE_COLUMNS
from app.integrations.core_source import MAX_CSV_BYTES, CSVCoreAssetSource
from app.schemas.vehicle_imports import ImportResolutions

MAPPING = {key: key for key in CORE_COLUMNS}


def parse(data: bytes):
    return list(CSVCoreAssetSource(data, MAPPING).iter_assets())


def test_synthetic_fixture_and_bom() -> None:
    data = (Path(__file__).parent / "fixtures" / "core-assets-synthetic.csv").read_bytes()
    for encoded in (data, b"\xef\xbb\xbf" + data):
        rows = parse(encoded)
        assert len(rows) == 3 and all(row.asset is not None for row in rows)
        assert rows[0].asset.year == 2026
        assert rows[1].asset.status == "INACTIVE"
        assert rows[2].asset.disposed_at.isoformat() == "2026-10-01"


def test_csv_quotes_multiline_and_no_unrelated_columns() -> None:
    rows = parse(
        b'core_asset_id,plate_no,province,brand,unrelated\r\nA,70-1234,P,"one,\ntwo",excluded\r\n'
    )
    assert rows[0].asset.brand == "one,\ntwo"
    assert "unrelated" not in rows[0].raw
    assert rows[0].row_no == 1
    minimal = parse(b"core_asset_id,plate_no,province\nA,70-1234,P\n")[0].asset
    assert minimal.status == "ACTIVE" and minimal.year is None


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\xff",
        b"PK\x03\x04",
        b"<xml/>",
        b"core_asset_id,plate_no,province\n",
        b"plate_no,province\n1,P",
        b"core_asset_id,plate_no,province,province\nA,1,P,P",
        b"core_asset_id,plate_no,province\nA,\x00,P",
        b'core_asset_id,plate_no,province\nA,"unterminated,P',
        b"a" * (MAX_CSV_BYTES + 1),
    ],
    ids=lambda data: f"bytes-{len(data)}",
)
def test_structurally_invalid_files(data: bytes) -> None:
    with pytest.raises(APIError) as error:
        parse(data)
    assert error.value.status_code == 422 and error.value.code == "IMPORT_FILE_INVALID"


@pytest.mark.parametrize(
    "row",
    [
        "A,1",
        "A,1,P,extra",
        ",1,P",
        "A,--,P",
        "A,1,",
    ],
)
def test_bad_rows_are_reported_without_aborting_other_rows(row: str) -> None:
    rows = parse(f"core_asset_id,plate_no,province\n{row}\nB,2,P\n".encode())
    assert rows[0].asset is None and rows[0].errors
    assert rows[1].asset.core_asset_id == "B"


@pytest.mark.parametrize(
    "column,value",
    [
        ("year", "๒๕๖๙"),
        ("year", "2e03"),
        ("year", "20.5"),
        ("year", "999"),
        ("status", "APPROVED"),
        ("status", "DISPOSED"),
        ("disposed_at", "20261001"),
        ("disposed_at", "2026-02-30"),
        ("brand", "x" * 201),
    ],
)
def test_invalid_field_values(column: str, value: str) -> None:
    rows = parse(f"core_asset_id,plate_no,province,{column}\nA,1,P,{value}\n".encode())
    assert rows[0].errors == ("ROW_INVALID",)


def test_ten_thousand_row_limit() -> None:
    header = b"core_asset_id,plate_no,province\n"
    data = b"".join(f"A-{number},{number},P\n".encode() for number in range(10000))
    assert len(parse(header + data)) == 10000
    with pytest.raises(APIError):
        parse(header + data + b"last,last,P\n")


def test_configured_header_mapping(environment: dict[str, str]) -> None:
    environment["CORE_CSV_COLUMN_MAP"] = '{"core_asset_id":"รหัส", "plate_no":"ทะเบียน"}'
    settings = Settings.from_env(environment)
    rows = list(
        CSVCoreAssetSource(
            "รหัส,ทะเบียน,province\nA,1,P\n".encode(),
            settings.core_csv_column_map,
        ).iter_assets()
    )
    assert rows[0].asset.core_asset_id == "A"


@pytest.mark.parametrize(
    "value",
    [
        "[]",
        '"text"',
        "null",
        '{"unknown":"header"}',
        '{"plate_no":null}',
        '{"plate_no":""}',
        '{"plate_no":"province"}',
        '{"year":12}',
    ],
)
def test_bad_mapping_is_redacted(environment: dict[str, str], value: str) -> None:
    environment["CORE_CSV_COLUMN_MAP"] = value
    with pytest.raises(ConfigurationError) as error:
        Settings.from_env(environment)
    assert str(error.value) == "Invalid setting: CORE_CSV_COLUMN_MAP"


def test_resolution_request_rejects_duplicate_rows() -> None:
    with pytest.raises(ValueError):
        ImportResolutions.model_validate(
            {
                "resolutions": [
                    {"row_no": 1, "resolution": "USE_CORE"},
                    {"row_no": 1, "resolution": "KEEP_MNT"},
                ]
            }
        )
