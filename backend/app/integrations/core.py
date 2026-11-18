"""CORE change sink: generate UTF-8 CSV locally, never contact CORE."""

import csv
import io
from collections.abc import Iterable, Mapping
from typing import Protocol

CORE_COLUMNS = (
    "core_asset_id",
    "plate_no",
    "province",
    "vehicle_type",
    "brand",
    "model",
    "year",
    "chassis_no",
    "status",
    "disposed_at",
)
EXPORT_COLUMNS = (*CORE_COLUMNS, "mnt_vehicle_id", "change_type", "changed_fields")


def safe_csv_cell(value: object) -> str:
    cell = "" if value is None else str(value)
    # Quoting CSV is insufficient for spreadsheets. Also guard formula prefixes
    # behind leading whitespace/control characters, without changing stored data.
    leading = cell.lstrip("\ufeff \t\r\n\v\f")
    if leading.startswith(("=", "+", "-", "@")) or cell.startswith(("\t", "\r", "\n")):
        return "'" + cell
    return cell


class CoreChangeSink(Protocol):
    def export(self, rows: Iterable[Mapping[str, object]]) -> bytes: ...


class CSVCoreChangeSink:
    def export(self, rows: Iterable[Mapping[str, object]]) -> bytes:
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(EXPORT_COLUMNS)
        for row in rows:
            writer.writerow(safe_csv_cell(row.get(column)) for column in EXPORT_COLUMNS)
        return stream.getvalue().encode("utf-8-sig")
