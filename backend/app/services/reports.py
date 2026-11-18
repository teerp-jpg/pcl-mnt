"""Aggregate completed headers once; never multiply costs by joining lines."""

import csv
import io
from datetime import date
from decimal import Decimal, localcontext

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session

from app.integrations.core import safe_csv_cell
from app.models import Garage, MaintenanceRecord, Vehicle
from app.schemas.reports import CostGroup, CostGrouping, CostSummary, CostTotals

CSV_COLUMNS = (
    "group_by",
    "group_key",
    "group_label",
    "record_count",
    "subtotal",
    "vat_amount",
    "total_amount",
)


def cost_summary(
    session: Session,
    group_by: CostGrouping,
    date_from: date | None = None,
    date_to: date | None = None,
) -> CostSummary:
    record = MaintenanceRecord
    # Only these fixed expressions can become GROUP BY; request text is never SQL.
    dimensions = {
        "vehicle": (
            cast(record.vehicle_id, String),
            Vehicle.plate_no + " " + Vehicle.province,
        ),
        "month": (
            func.to_char(record.service_end_date, "YYYY-MM"),
            func.to_char(record.service_end_date, "YYYY-MM"),
        ),
        "type": (record.maintenance_type, record.maintenance_type),
        "garage": (cast(record.garage_id, String), func.coalesce(Garage.name, "ไม่ระบุอู่")),
    }
    key, label = dimensions[group_by]
    statement = select(
        key.label("group_key"),
        label.label("group_label"),
        func.count(record.id).label("record_count"),
        func.sum(record.subtotal).label("subtotal"),
        func.sum(record.vat_amount).label("vat_amount"),
        func.sum(record.total_amount).label("total_amount"),
    ).select_from(record)
    if group_by == "vehicle":
        statement = statement.join(Vehicle, Vehicle.id == record.vehicle_id)
    elif group_by == "garage":
        statement = statement.outerjoin(Garage, Garage.id == record.garage_id)
    statement = statement.where(record.status == "COMPLETED", record.service_end_date.is_not(None))
    if date_from is not None:
        statement = statement.where(record.service_end_date >= date_from)
    if date_to is not None:
        statement = statement.where(record.service_end_date <= date_to)
    statement = statement.group_by(key, label).order_by(key.asc().nulls_last())
    items = [CostGroup.model_validate(row) for row in session.execute(statement).mappings()]
    # Totals derive from the same SELECT snapshot, even if a correction commits
    # concurrently. Aggregated totals may exceed an individual NUMERIC(14,2).
    with localcontext() as context:
        context.prec = 50
        totals = CostTotals(
            record_count=sum(item.record_count for item in items),
            subtotal=sum((item.subtotal for item in items), Decimal("0.00")),
            vat_amount=sum((item.vat_amount for item in items), Decimal("0.00")),
            total_amount=sum((item.total_amount for item in items), Decimal("0.00")),
        )
    return CostSummary(
        group_by=group_by, date_from=date_from, date_to=date_to, items=items, totals=totals
    )


def cost_summary_csv(report: CostSummary) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(CSV_COLUMNS)
    for item in report.items:
        row = {"group_by": report.group_by, **item.model_dump(mode="json")}
        writer.writerow(safe_csv_cell(row[column]) for column in CSV_COLUMNS)
    # No synthetic total row: summing exported rows equals JSON totals exactly.
    return stream.getvalue().encode("utf-8-sig")
