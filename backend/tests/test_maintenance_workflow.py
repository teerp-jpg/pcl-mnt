"""PostgreSQL workflow, validation, exact money round-trips and atomic audit."""

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from maintenance_support import create, headers, setup_record
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.maintenance_state import TRANSITIONS
from app.models import (
    AppUser,
    AuditLog,
    MaintenanceLine,
    MaintenanceRecord,
    OdometerReading,
    PMPlan,
)
from app.services import maintenance

pytestmark = pytest.mark.postgresql
BASE = "/api/v1/maintenance-records"


def test_pm_workflow_projections_and_event(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    auth, payload, vehicle, _ = setup_record(db_session, auth_client, auth_users)
    plan = PMPlan(vehicle_id=vehicle.id, name="Synthetic PM", interval_km=10000, interval_days=180)
    db_session.add(plan)
    db_session.flush()
    payload["pm_plan_id"] = str(plan.id)
    events = []
    db_session.info["record_status_changed_handlers"] = [
        lambda _session, event: events.append(event)
    ]
    item = create(auth_client, auth, payload)
    path = f"{BASE}/{item['id']}"
    assert item["subtotal"] == "1500.00" and item["total_amount"] == "1605.00"
    started = auth_client.post(path + "/start", headers=auth, json={"version": 1})
    assert started.status_code == 200, started.text
    completed = auth_client.post(path + "/complete", headers=auth, json={"version": 2})
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "COMPLETED"
    db_session.refresh(plan)
    assert plan.last_done_at == date(2026, 10, 10) and plan.last_done_km == 120500
    reading = db_session.execute(
        select(OdometerReading).where(OdometerReading.source_ref == item["id"])
    ).scalar_one()
    assert reading.reading_km == 120500
    assert [(event.before, event.after, event.name) for event in events] == [
        ("DRAFT", "IN_PROGRESS", "record_status_changed"),
        ("IN_PROGRESS", "COMPLETED", "record_status_changed"),
    ]
    assert (
        auth_client.patch(path, headers=auth, json={"version": 3, "notes": "blocked"}).status_code
        == 409
    )
    assert auth_client.post(path + "/start", headers=auth, json={"version": 3}).status_code == 409


def test_money_roundtrip_patch_correction_and_audit(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    auth, payload, _, _ = setup_record(db_session, auth_client, auth_users, vat_amount="0")
    payload["lines"][0].update(qty="1.50", unit_price="33.33", amount="500000")
    payload.update(subtotal="500000", total_amount="600000")
    item = create(auth_client, auth, payload)
    path = f"{BASE}/{item['id']}"
    assert item["lines"][0]["amount"] == item["total_amount"] == "50.00"
    data = auth_client.get(path, headers=auth).json()
    operands = {name: data["lines"][0][name] for name in ("qty", "unit_price")}
    payload["lines"][0].update(operands)
    patched = auth_client.patch(
        path, headers=auth, json={"version": 1, "lines": payload["lines"], "vat_amount": "1.00"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["total_amount"] == "51.00"
    complete = auth_client.post(path + "/complete", headers=auth, json={"version": 2})
    assert complete.status_code == 200, complete.text
    for reason in (None, "", "   "):
        rejected = auth_client.post(
            path + "/corrections",
            headers=auth,
            json={"version": 3, "reason": reason, "changes": {}},
        )
        assert (
            rejected.status_code == 422 and rejected.json()["code"] == "CORRECTION_REASON_REQUIRED"
        )
    corrected = auth_client.post(
        path + "/corrections",
        headers=auth,
        json={
            "version": 3,
            "reason": "Synthetic correction",
            "changes": {"lines": payload["lines"], "vat_amount": "0"},
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["total_amount"] == "50.00"
    audit = db_session.execute(
        select(AuditLog).where(AuditLog.entity_id == item["id"], AuditLog.action == "CORRECTION")
    ).scalar_one()
    assert (
        audit.before_json["total_amount"] == "51.00" and audit.after_json["total_amount"] == "50.00"
    )
    assert audit.actor == auth_users["manager"].id and audit.reason == "Synthetic correction"
    assert audit.at is not None
    stale = auth_client.post(
        path + "/corrections", headers=auth, json={"version": 3, "reason": "stale", "changes": {}}
    )
    assert stale.status_code == 409 and stale.json()["code"] == "VERSION_CONFLICT"


@pytest.mark.parametrize(
    "field,value", [("unit_price", "33.335"), ("qty", "1.505"), ("vat_amount", "1.005")]
)
def test_reject_excess_precision_without_any_writes(
    field: str,
    value: str,
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    auth, payload, _, _ = setup_record(db_session, auth_client, auth_users)
    if field == "vat_amount":
        payload[field] = value
    else:
        payload["lines"][0][field] = value
    response = auth_client.post(BASE, headers=auth, json=payload)
    assert response.status_code == 422 and response.json()["code"] == "VALIDATION_FAILED"
    assert response.json()["details"]["errors"][0]["location"][-1] == field
    assert db_session.scalar(select(func.count()).select_from(MaintenanceRecord)) == 0
    assert db_session.scalar(select(func.count()).select_from(MaintenanceLine)) == 0


@pytest.mark.parametrize("role", ["viewer", "clerk", "manager", "admin"])
def test_record_role_permissions(
    role: str,
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    auth, payload, _, _ = setup_record(db_session, auth_client, auth_users)
    item = create(auth_client, auth, payload)
    path = f"{BASE}/{item['id']}"
    role_auth = headers(auth_client, auth_users[role])
    assert auth_client.get(path, headers=role_auth).status_code == 200
    assert auth_client.get(path).status_code == 401
    assert auth_client.post(BASE, headers=role_auth, json=payload).status_code == (
        403 if role == "viewer" else 201
    )
    patched = auth_client.patch(path, headers=role_auth, json={"version": 1, "notes": "Synthetic"})
    assert patched.status_code == (403 if role == "viewer" else 200)
    version = 1 if role == "viewer" else 2
    stale = auth_client.patch(path, headers=auth, json={"version": version + 10, "notes": "stale"})
    assert stale.status_code == 409 and stale.json()["code"] == "VERSION_CONFLICT"
    if role in {"viewer", "clerk"}:
        for action, body in [
            ("cancel", {"reason": "Synthetic"}),
            ("corrections", {"changes": {}, "reason": "Synthetic"}),
        ]:
            assert (
                auth_client.post(
                    path + "/" + action, headers=role_auth, json={"version": version, **body}
                ).status_code
                == 403
            )
    else:
        cancelled = auth_client.post(
            path + "/cancel", headers=role_auth, json={"version": version, "reason": "Synthetic"}
        )
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
        assert (
            auth_client.post(
                path + "/complete", headers=role_auth, json={"version": version + 1}
            ).status_code
            == 409
        )


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"maintenance_type": "OTHER"}, "VALIDATION_FAILED"),
        ({"maintenance_type": "CM"}, "VALIDATION_FAILED"),
        ({"service_start_date": "2999-01-01", "service_end_date": None}, "VALIDATION_FAILED"),
        ({"service_end_date": "2020-01-01"}, "VALIDATION_FAILED"),
        ({"pm_plan_id": str(uuid.uuid4())}, "VALIDATION_FAILED"),
    ],
)
def test_business_validation(
    changes: dict,
    code: str,
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    auth, payload, _, _ = setup_record(db_session, auth_client, auth_users)
    payload.update(changes)
    response = auth_client.post(BASE, headers=auth, json=payload)
    assert response.status_code == 422 and response.json()["code"] == code


def test_incomplete_draft_cancel_and_completion_guards(
    auth_client: TestClient, auth_users: dict[str, AppUser], db_session: Session
) -> None:
    auth, _, vehicle, _ = setup_record(db_session, auth_client, auth_users)
    item = create(auth_client, auth, {"vehicle_id": str(vehicle.id)})
    path = f"{BASE}/{item['id']}"
    for action in ("start", "complete"):
        response = auth_client.post(path + "/" + action, headers=auth, json={"version": 1})
        assert response.status_code == 422
        assert auth_client.get(path, headers=auth).json()["status"] == "DRAFT"
    cancelled = auth_client.post(
        path + "/cancel", headers=auth, json={"version": 1, "reason": "Synthetic"}
    )
    assert cancelled.status_code == 200, cancelled.text


def test_cm_missing_odometer_and_invoice_warning(
    auth_client: TestClient, auth_users: dict[str, AppUser], db_session: Session
) -> None:
    auth, payload, _, _ = setup_record(
        db_session,
        auth_client,
        auth_users,
        maintenance_type="CM",
        failure_description="Synthetic brake fault",
        odometer_km=None,
        invoice_no="SYN-001",
    )
    item = create(auth_client, auth, payload)
    path = f"{BASE}/{item['id']}"
    assert (
        auth_client.post(path + "/complete", headers=auth, json={"version": 1}).status_code == 422
    )
    patched = auth_client.patch(
        path,
        headers=auth,
        json={"version": 1, "odometer_missing_reason": "Synthetic instrument fault"},
    )
    assert patched.status_code == 200
    assert (
        auth_client.post(path + "/complete", headers=auth, json={"version": 2}).status_code == 200
    )
    duplicate = create(auth_client, auth, payload)
    assert "POSSIBLE_DUPLICATE_INVOICE" in duplicate["warnings"]


def test_vehicle_disposal_inactive_and_garage_history(
    auth_client: TestClient, auth_users: dict[str, AppUser], db_session: Session
) -> None:
    auth, payload, vehicle, garage = setup_record(db_session, auth_client, auth_users)
    vehicle.status = "INACTIVE"
    db_session.flush()
    item = create(auth_client, auth, payload)
    assert "VEHICLE_INACTIVE" in item["warnings"]
    garage.is_active = False
    db_session.flush()
    assert (
        auth_client.get(f"{BASE}/{item['id']}", headers=auth).json()["garage_name"] == garage.name
    )
    assert auth_client.post(BASE, headers=auth, json=payload).json()["code"] == "GARAGE_INACTIVE"
    vehicle.status, vehicle.disposed_at = "DISPOSED", date(2026, 10, 1)
    db_session.flush()
    assert auth_client.post(BASE, headers=auth, json=payload).json()["code"] == "ASSET_DISPOSED"


def test_odometer_regression_requires_reason(
    auth_client: TestClient, auth_users: dict[str, AppUser], db_session: Session
) -> None:
    auth, payload, _, _ = setup_record(db_session, auth_client, auth_users)
    item = create(auth_client, auth, payload)
    assert (
        auth_client.post(
            f"{BASE}/{item['id']}/complete", headers=auth, json={"version": 1}
        ).status_code
        == 200
    )
    payload.update(
        service_start_date="2026-10-11", service_end_date="2026-10-11", odometer_km=119000
    )
    rejected = auth_client.post(BASE, headers=auth, json=payload)
    assert (
        rejected.status_code == 422
        and rejected.json()["code"] == "ODOMETER_REGRESSION_REASON_REQUIRED"
    )
    payload["odometer_regression_reason"] = "Synthetic odometer replacement"
    assert auth_client.post(BASE, headers=auth, json=payload).status_code == 201


def test_failed_audit_and_event_roll_back_whole_change(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth, payload, _, _ = setup_record(db_session, auth_client, auth_users)
    item = create(auth_client, auth, payload)
    path = f"{BASE}/{item['id']}"

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Synthetic failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(maintenance, "write_audit", fail)
        with pytest.raises(RuntimeError):
            auth_client.patch(
                path, headers=auth, json={"version": 1, "vat_amount": "50", "notes": "rollback"}
            )
    actual = auth_client.get(path, headers=auth).json()
    assert actual["version"] == 1 and actual["vat_amount"] == "105.00" and actual["notes"] is None
    db_session.info["record_status_changed_handlers"] = [fail]
    with pytest.raises(RuntimeError):
        auth_client.post(path + "/complete", headers=auth, json={"version": 1})
    assert auth_client.get(path, headers=auth).json()["status"] == "DRAFT"
    assert db_session.scalar(select(func.count()).select_from(OdometerReading)) == 0


def test_transition_table_and_openapi_have_only_current_states(auth_client: TestClient) -> None:
    assert set(TRANSITIONS) == {"DRAFT", "IN_PROGRESS", "COMPLETED", "CANCELLED"}
    schema = auth_client.get("/openapi.json").json()
    record_paths = [path for path in schema["paths"] if "maintenance-records" in path]
    assert record_paths and all("approval" not in path for path in record_paths)
    assert schema["components"]["schemas"]["RecordView"]["properties"]["status"]["enum"] == [
        "DRAFT",
        "IN_PROGRESS",
        "COMPLETED",
        "CANCELLED",
    ]


def test_pm_correction_recomputes_baseline_and_odometer(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    auth, payload, vehicle, _ = setup_record(db_session, auth_client, auth_users)
    plan = PMPlan(vehicle_id=vehicle.id, name="Synthetic PM", interval_days=180)
    db_session.add(plan)
    db_session.flush()
    payload["pm_plan_id"] = str(plan.id)
    item = create(auth_client, auth, payload)
    path = f"{BASE}/{item['id']}"
    assert (
        auth_client.post(path + "/complete", headers=auth, json={"version": 1}).status_code == 200
    )
    corrected = auth_client.post(
        path + "/corrections",
        headers=auth,
        json={
            "version": 2,
            "reason": "Synthetic corrected date and reading",
            "changes": {"service_end_date": "2026-10-12", "odometer_km": 120600},
        },
    )
    assert corrected.status_code == 200, corrected.text
    db_session.refresh(plan)
    assert plan.last_done_at == date(2026, 10, 12) and plan.last_done_km == 120600
    reading = db_session.execute(
        select(OdometerReading).where(OdometerReading.source_ref == item["id"])
    ).scalar_one()
    assert reading.reading_km == 120600
    detached = auth_client.post(
        path + "/corrections",
        headers=auth,
        json={
            "version": 3,
            "reason": "Synthetic detach plan",
            "changes": {"pm_plan_id": None},
        },
    )
    assert detached.status_code == 200, detached.text
    db_session.refresh(plan)
    assert plan.last_done_at is None and plan.last_done_km is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("unit_price", "33.335"),
        ("qty", "1.505"),
        ("vat_amount", "1.005"),
    ],
)
def test_precision_rejection_preserves_existing_record(
    field: str,
    value: str,
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    auth, payload, _, _ = setup_record(db_session, auth_client, auth_users)
    item = create(auth_client, auth, payload)
    path = f"{BASE}/{item['id']}"
    if field == "vat_amount":
        changes = {field: value}
    else:
        payload["lines"][0][field] = value
        changes = {"lines": payload["lines"]}
    response = auth_client.patch(path, headers=auth, json={"version": 1, **changes})
    assert response.status_code == 422 and response.json()["code"] == "VALIDATION_FAILED"
    assert auth_client.get(path, headers=auth).json() == item
    assert (
        auth_client.post(path + "/complete", headers=auth, json={"version": 1}).status_code == 200
    )
    before = auth_client.get(path, headers=auth).json()
    response = auth_client.post(
        path + "/corrections",
        headers=auth,
        json={
            "version": 2,
            "reason": "Synthetic invalid precision",
            "changes": changes,
        },
    )
    assert response.status_code == 422 and response.json()["code"] == "VALIDATION_FAILED"
    assert auth_client.get(path, headers=auth).json() == before
