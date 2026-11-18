"""Bounded, local CSV source; no file paths, XML, OCR or network operations."""

import csv
import io
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from pydantic import ValidationError

from app.errors import APIError
from app.integrations.core import CORE_COLUMNS
from app.schemas.vehicle_imports import CoreAssetValues

MAX_CSV_BYTES = 5 * 1024 * 1024
MAX_CSV_ROWS = 10000


def invalid_file() -> APIError:
    return APIError(422, "IMPORT_FILE_INVALID", "Invalid CSV file or import limit exceeded")


@dataclass(frozen=True)
class CoreAssetRow:
    row_no: int
    raw: dict[str, str]
    asset: CoreAssetValues | None
    errors: tuple[str, ...] = ()


class CoreAssetSource(Protocol):
    def iter_assets(self) -> Iterator[CoreAssetRow]: ...


def parse_asset(raw: Mapping[str, str]) -> CoreAssetValues:
    payload: dict[str, object] = dict(raw)
    for key in CORE_COLUMNS:
        if key not in {"core_asset_id", "plate_no", "province"}:
            payload[key] = raw.get(key) or ("ACTIVE" if key == "status" else None)
    year = payload.get("year")
    if year is not None:
        if not isinstance(year, str) or len(year) != 4 or not year.isascii() or not year.isdigit():
            raise ValueError("Invalid year")
        payload["year"] = int(year)
    disposed = payload.get("disposed_at")
    if disposed is not None:
        if not isinstance(disposed, str) or len(disposed) != 10:
            raise ValueError("Invalid date")
        payload["disposed_at"] = date.fromisoformat(disposed)
    asset = CoreAssetValues.model_validate(payload)
    if asset.status == "DISPOSED" and asset.disposed_at is None:
        raise ValueError("Missing disposal date")
    return asset


class CSVCoreAssetSource:
    def __init__(self, data: bytes, column_map: Mapping[str, str]) -> None:
        if len(data) > MAX_CSV_BYTES or not data or b"\x00" in data:
            raise invalid_file()
        self.data = data
        self.column_map = column_map

    def iter_assets(self) -> Iterator[CoreAssetRow]:
        try:
            stream = io.StringIO(self.data.decode("utf-8-sig"), newline="")
            reader = csv.reader(stream, strict=True)
            headers = next(reader)
            headers = [header.strip() for header in headers]
            if (
                len(set(headers)) != len(headers)
                or any(not header for header in headers)
                or len(headers) > 100
                or any(len(header) > 200 for header in headers)
                or not {
                    self.column_map[key] for key in ("core_asset_id", "plate_no", "province")
                }.issubset(headers)
            ):
                raise invalid_file()
            positions = {header: index for index, header in enumerate(headers)}
            row_no = 0
            for values in reader:
                if not values or all(not value.strip() for value in values):
                    continue
                row_no += 1
                if row_no > MAX_CSV_ROWS:
                    raise invalid_file()
                # Only canonical fields are retained; unrelated CORE columns may
                # contain data outside the approved registry scope.
                raw = {
                    key: values[positions[self.column_map[key]]].strip()
                    for key in CORE_COLUMNS
                    if self.column_map[key] in positions
                    and positions[self.column_map[key]] < len(values)
                }
                if len(values) != len(headers):
                    yield CoreAssetRow(row_no, raw, None, ("COLUMN_COUNT",))
                    continue
                try:
                    asset = parse_asset(raw)
                except (ValidationError, ValueError):
                    yield CoreAssetRow(row_no, raw, None, ("ROW_INVALID",))
                    continue
                yield CoreAssetRow(row_no, raw, asset)
            if row_no == 0:
                raise invalid_file()
        except (UnicodeError, csv.Error, StopIteration):
            raise invalid_file() from None
