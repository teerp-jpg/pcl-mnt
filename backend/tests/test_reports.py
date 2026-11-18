"""Independent DB reconciliation, report boundaries and safe CSV downloads."""

import csv
import io
import uuid
from datetime import date
from decimal import Decimal

import pytest
from pydantic import TypeAdapter

from app.schemas.reports import CostSummary, ReportMoney

BASE = "/api/v1/reports/cost-summary"


def test_money_serialization():
    # Aggregate values are not limited to one record's NUMERIC(14,2) precision.
    adapter = TypeAdapter(ReportMoney)
    for value in ("0", "50", "1605.00", "1999999999999.98"):
        assert adapter.dump_python(Decimal(value), mode="json") == format(Decimal(value), ".2f")


@pytest.fixture
def report_data(db_session):
    from app.models import Garage, MaintenanceCategory, MaintenanceLine, MaintenanceRecord, Vehicle

    vehicles = [
        Vehicle(
            plate_no=plate,
            plate_no_norm=uuid.uuid4().hex,
            province="ทดสอบ",
            origin="MTN",
            core_sync_status="PENDING_CREATE",
        )
        for plate in ("=1+1", "รถสมมติ")
    ]
    garages = [
        Garage(name=name, name_norm=uuid.uuid4().hex, is_active=active)
        for name, active in [('=SUM(1,2)\nอู่"สมมติ', False), ("อู่สมมติสอง", True)]
    ]
    db_session.add_all([*vehicles, *garages])
    db_session.flush()
    # Different start/end months and years prove the report uses completion date.
    specifications = [
        (0, 0, "PM", "COMPLETED", "2025-12-31", "50.00", "0.00"),
        (0, 0, "CM", "COMPLETED", "2026-01-01", "1500.00", "105.00"),
        (1, 1, "PM", "COMPLETED", "2026-01-31", "2400.00", "0.00"),
        (1, None, "CM", "COMPLETED", "2026-02-01", "0.01", "0.02"),
        (0, 0, "PM", "DRAFT", "2026-01-15", "9000.00", "0.00"),
        (0, 1, "CM", "IN_PROGRESS", "2026-01-16", "8000.00", "0.00"),
        (1, 1, "PM", "CANCELLED", "2026-01-17", "7000.00", "0.00"),
        (0, 0, "PM", "COMPLETED", None, "6000.00", "0.00"),
    ]
    records = []
    for vi, gi, kind, status, end, subtotal, vat in specifications:
        record = MaintenanceRecord(
            vehicle_id=vehicles[vi].id,
            garage_id=garages[gi].id if gi is not None else None,
            plate_no_snapshot="ทะเบียนเก่า",
            province_snapshot="จังหวัดเดิม",
            maintenance_type=kind,
            status=status,
            service_start_date=date(2025, 11, 1),
            service_end_date=date.fromisoformat(end) if end else None,
            subtotal=Decimal(subtotal),
            vat_amount=Decimal(vat),
            total_amount=Decimal(subtotal) + Decimal(vat),
        )
        records.append(record)
    db_session.add_all(records)
    db_session.flush()
    # Two lines on one header must not duplicate the header's total or count.
    category = MaintenanceCategory(code=uuid.uuid4().hex, name_th="หมวดสมมติ")
    db_session.add(category)
    db_session.flush()
    db_session.add_all(
        [
            MaintenanceLine(
                record_id=records[1].id,
                line_no=number,
                category_id=category.id,
                line_type="PART",
                description="บรรทัดสมมติ",
                qty=Decimal("1.00"),
                unit_price=Decimal("750.00"),
                amount=Decimal("750.00"),
            )
            for number in (1, 2)
        ]
    )
    db_session.flush()
    return records, vehicles, garages


def report_headers(client, users, role="viewer"):
    from maintenance_support import headers

    return headers(client, users[role])


@pytest.mark.postgresql
@pytest.mark.parametrize("grouping", ["vehicle", "month", "type", "garage"])
@pytest.mark.parametrize(
    "bounds", [{}, {"from": "2026-01-01", "to": "2026-01-31"}, {"from": "2026-02-01"}]
)
def test_json_csv_match_independent_db_totals(
    auth_client, auth_users, db_session, report_data, grouping, bounds
):
    from sqlalchemy import select

    from app.integrations.core import safe_csv_cell
    from app.models import MaintenanceRecord

    auth = report_headers(auth_client, auth_users)
    params = {"group_by": grouping, **bounds}
    response = auth_client.get(BASE, headers=auth, params=params)
    assert response.status_code == 200, response.text
    report = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert report["date_from"] == bounds.get("from")
    assert report["date_to"] == bounds.get("to")
    # Reconcile independent raw DB headers with Python grouping (no SQL aggregates).
    expected = {}
    records = db_session.execute(select(MaintenanceRecord)).scalars().all()
    for record in records:
        end = record.service_end_date
        if record.status != "COMPLETED" or end is None:
            continue
        if bounds.get("from") and end < date.fromisoformat(bounds["from"]):
            continue
        if bounds.get("to") and end > date.fromisoformat(bounds["to"]):
            continue
        key = {
            "vehicle": str(record.vehicle_id),
            "month": end.strftime("%Y-%m"),
            "type": record.maintenance_type,
            "garage": str(record.garage_id) if record.garage_id else None,
        }[grouping]
        totals = expected.setdefault(key, [0, Decimal("0"), Decimal("0"), Decimal("0")])
        totals[0] += 1
        totals[1] += record.subtotal
        totals[2] += record.vat_amount
        totals[3] += record.total_amount
    assert len(report["items"]) == len(expected)
    fields = ("record_count", "subtotal", "vat_amount", "total_amount")
    for item in report["items"]:
        assert [Decimal(str(item[field])) for field in fields] == expected[item["group_key"]]
        for field in fields[1:]:
            assert isinstance(item[field], str) and len(item[field].split(".")[1]) == 2
    for index, field in enumerate(fields):
        assert Decimal(str(report["totals"][field])) == sum(row[index] for row in expected.values())
    download = auth_client.get(BASE, headers=auth, params={**params, "format": "csv"})
    assert download.status_code == 200
    assert download.content.startswith(b"\xef\xbb\xbf")
    assert download.headers["content-type"].startswith("text/csv")
    assert "attachment;" in download.headers["content-disposition"]
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "no-store"
    rows = list(csv.DictReader(io.StringIO(download.content.decode("utf-8-sig"), newline="")))
    assert len(rows) == len(report["items"])
    for row, item in zip(rows, report["items"], strict=True):
        assert row == {
            "group_by": grouping,
            **{field: safe_csv_cell(value) for field, value in item.items()},
        }
    assert sum(Decimal(row["total_amount"]) for row in rows) == Decimal(
        report["totals"]["total_amount"]
    )
    if grouping == "garage" and not bounds:
        assert any(item["group_key"] is None for item in report["items"])
        # Inactive garages are still included, and formula/newline text survives safely.
        assert any(row["group_label"].startswith("'=SUM") for row in rows)
    if grouping == "vehicle" and not bounds:
        assert any(row["group_label"].startswith("'=1+1") for row in rows)
        assert all("ทะเบียนเก่า" not in item["group_label"] for item in report["items"])


@pytest.mark.postgresql
@pytest.mark.parametrize("role", ["viewer", "clerk", "manager", "admin"])
def test_all_roles_can_read_both_formats(auth_client, auth_users, report_data, role):
    for output in ("json", "csv"):
        params = {"format": output}
        assert auth_client.get(BASE, params=params).status_code == 401
        assert (
            auth_client.get(
                BASE, params=params, headers=report_headers(auth_client, auth_users, role)
            ).status_code
            == 200
        )


@pytest.mark.postgresql
@pytest.mark.parametrize(
    "params",
    [
        {"group_by": "vehicle; DROP TABLE vehicle"},
        {"format": "pdf"},
        {"from": "invalid"},
        {"to": "2026-02-30"},
        {"from": "2026-02-02", "to": "2026-02-01"},
    ],
)
def test_invalid_filters(auth_client, auth_users, params):
    response = auth_client.get(BASE, headers=report_headers(auth_client, auth_users), params=params)
    assert response.status_code == 422 and response.json()["code"] == "VALIDATION_FAILED"
    assert response.json()["details"]["errors"][0]["location"][0] == "query"


@pytest.mark.postgresql
def test_empty_report_and_single_day(auth_client, auth_users, report_data):
    auth = report_headers(auth_client, auth_users)
    params = {"from": "2026-01-01", "to": "2026-01-01", "group_by": "month"}
    report = auth_client.get(BASE, headers=auth, params=params).json()
    assert report["totals"] == {
        "record_count": 1,
        "subtotal": "1500.00",
        "vat_amount": "105.00",
        "total_amount": "1605.00",
    }
    for grouping in ("vehicle", "month", "type", "garage"):
        params = {"to": "2025-01-01", "group_by": grouping}
        report = auth_client.get(BASE, headers=auth, params=params).json()
        assert report["items"] == [] and report["totals"]["record_count"] == 0
        assert report["totals"]["total_amount"] == "0.00"
        download = auth_client.get(BASE, headers=auth, params={**params, "format": "csv"})
        assert len(list(csv.reader(io.StringIO(download.content.decode("utf-8-sig"))))) == 1


@pytest.mark.postgresql
def test_report_is_one_read_query_without_mutations(db_session, report_data):
    from sqlalchemy import event

    from app.services.reports import cost_summary

    queries = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", capture)
    try:
        result = cost_summary(db_session, "vehicle")
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert result.totals.record_count == 4
    assert len(queries) == 1 and queries[0].lstrip().upper().startswith("SELECT")
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_empty_json_money_contract():
    report = CostSummary.model_validate(
        {
            "group_by": "month",
            "date_from": None,
            "date_to": None,
            "items": [],
            "totals": {
                "record_count": 0,
                "subtotal": "0.00",
                "vat_amount": "0.00",
                "total_amount": "0.00",
            },
        }
    )
    assert report.model_dump(mode="json")["totals"] == {
        "record_count": 0,
        "subtotal": "0.00",
        "vat_amount": "0.00",
        "total_amount": "0.00",
    }


@pytest.mark.postgresql
def test_aggregate_can_exceed_individual_record_limit(
    auth_client, auth_users, db_session, report_data
):
    records, _, _ = report_data
    for record in records[:2]:
        record.subtotal = Decimal("999999999999.99")
        record.vat_amount = Decimal("0.00")
        record.total_amount = record.subtotal
    db_session.flush()
    auth = report_headers(auth_client, auth_users)
    params = {"to": "2026-01-01", "group_by": "vehicle"}
    report = auth_client.get(BASE, headers=auth, params=params).json()
    assert report["items"][0]["total_amount"] == "1999999999999.98"
    assert report["totals"]["total_amount"] == "1999999999999.98"


@pytest.mark.postgresql
def test_completed_correction_changes_report(auth_client, auth_users, db_session):
    from maintenance_support import create, setup_record

    auth, payload, _, _ = setup_record(db_session, auth_client, auth_users)
    item = create(auth_client, auth, payload)
    response = auth_client.post(
        f"/api/v1/maintenance-records/{item['id']}/complete",
        headers=auth,
        json={"version": 1},
    )
    assert response.status_code == 200, response.text
    before = auth_client.get(BASE, headers=auth).json()
    assert before["totals"]["total_amount"] == "1605.00"
    response = auth_client.post(
        f"/api/v1/maintenance-records/{item['id']}/corrections",
        headers=auth,
        json={
            "version": 2,
            "reason": "Synthetic invoice correction",
            "changes": {"vat_amount": "0.00"},
        },
    )
    assert response.status_code == 200, response.text
    after = auth_client.get(BASE, headers=auth).json()
    assert after["totals"]["record_count"] == 1
    assert after["totals"]["total_amount"] == "1500.00"
