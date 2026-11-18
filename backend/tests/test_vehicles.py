"""Registry history, versions, permissions and rollback on PostgreSQL."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.auth.dependencies import issue_token
from app.models import AppUser, AuditLog, MaintenanceRecord, PMPlan, Vehicle, VehicleChange
from app.services.audit import write_audit

pytestmark = pytest.mark.postgresql


def authorization(client: TestClient, user: AppUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(user, client.app.state.settings)}"}


def make_vehicle(
    client: TestClient, headers: dict[str, str], **fields: object
) -> dict[str, object]:
    response = client.post(
        "/api/v1/vehicles",
        headers=headers,
        json={
            "plate_no": uuid.uuid4().hex,
            "province": "จังหวัดสมมติ",
            **fields,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("role", ["viewer", "clerk", "manager", "admin"])
def test_registry_permissions(
    role: str, auth_client: TestClient, auth_users: dict[str, AppUser]
) -> None:
    admin = authorization(auth_client, auth_users["admin"])
    item = make_vehicle(auth_client, admin)
    headers = authorization(auth_client, auth_users[role])
    path = f"/api/v1/vehicles/{item['id']}"
    for suffix in ("", "/summary", "/maintenance-records"):
        assert auth_client.get(path + suffix, headers=headers).status_code == 200
    assert auth_client.get("/api/v1/vehicles", headers=headers).json()["total"] == 1
    allowed = role in {"manager", "admin"}
    assert auth_client.post(
        "/api/v1/vehicles",
        headers=headers,
        json={
            "plate_no": uuid.uuid4().hex,
            "province": "สมมติ",
        },
    ).status_code == (201 if allowed else 403)
    assert auth_client.patch(
        path, headers=headers, json={"version": 1, "brand": "Test"}
    ).status_code == (200 if allowed else 403)
    assert auth_client.get("/api/v1/vehicles/core-export", headers=headers).status_code == (
        200 if allowed else 403
    )
    assert auth_client.post(
        path + "/confirm-core-sync", headers=headers, json={"core_asset_id": "SYNTHETIC"}
    ).status_code == (200 if allowed else 403)
    disposable = make_vehicle(auth_client, admin)
    assert auth_client.delete(
        f"/api/v1/vehicles/{disposable['id']}?version=1", headers=headers
    ).status_code == (204 if allowed else 403)
    assert auth_client.get(path).status_code == 401


def test_plate_history_search_and_immutable_record_snapshot(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    headers = authorization(auth_client, auth_users["manager"])
    item = make_vehicle(auth_client, headers, plate_no="70-1234")
    vehicle_id = uuid.UUID(str(item["id"]))
    record = MaintenanceRecord(
        vehicle_id=vehicle_id,
        plate_no_snapshot="70-1234",
        province_snapshot="จังหวัดสมมติ",
        maintenance_type="PM",
    )
    db_session.add(record)
    db_session.flush()
    path = f"/api/v1/vehicles/{vehicle_id}"
    updated = auth_client.patch(
        path,
        headers=headers,
        json={
            "version": 1,
            "plate_no": "71-0001",
            "province": "จังหวัดใหม่",
        },
    )
    assert updated.status_code == 200, updated.text
    detail = auth_client.get(path, headers=headers).json()
    assert [row["plate_no"] for row in detail["plate_history"]] == ["70-1234", "71-0001"]
    assert detail["plate_history"][0]["valid_to"] is not None
    assert detail["plate_history"][1]["valid_to"] is None
    for query in (" 7 0 - 1 2 3 4 ", "71-0001"):
        result = auth_client.get("/api/v1/vehicles", headers=headers, params={"q": query}).json()
        assert result["total"] == 1 and result["items"][0]["id"] == str(vehicle_id)
    assert (
        auth_client.get(path + "/maintenance-records", headers=headers).json()[0][
            "plate_no_snapshot"
        ]
        == "70-1234"
    )
    assert (
        auth_client.patch(path, headers=headers, json={"version": 1, "brand": "stale"}).json()[
            "code"
        ]
        == "VERSION_CONFLICT"
    )
    assert (
        auth_client.delete(path + "?version=2", headers=headers).json()["code"]
        == "VEHICLE_DELETE_NOT_ALLOWED"
    )
    changes = (
        db_session.execute(select(VehicleChange).where(VehicleChange.vehicle_id == vehicle_id))
        .scalars()
        .all()
    )
    assert len(changes) == 2
    events = (
        db_session.execute(select(AuditLog).where(AuditLog.entity_id == str(vehicle_id)))
        .scalars()
        .all()
    )
    assert {event.action for event in events} == {"CREATE", "UPDATE"}
    assert all(event.actor == auth_users["manager"].id and event.at is not None for event in events)


def test_uniqueness_disposal_and_nullable_updates(
    auth_client: TestClient, auth_users: dict[str, AppUser], db_session: Session
) -> None:
    headers = authorization(auth_client, auth_users["admin"])
    first = make_vehicle(auth_client, headers, plate_no="70-1234", chassis_no="SYNTHETIC-CHASSIS")
    path = f"/api/v1/vehicles/{first['id']}"
    duplicate = auth_client.post(
        "/api/v1/vehicles",
        headers=headers,
        json={
            "plate_no": " 7 0 -1234 ",
            "province": "จังหวัดสมมติ",
        },
    )
    assert duplicate.json()["code"] == "DUPLICATE_PLATE"
    assert (
        auth_client.post(
            "/api/v1/vehicles",
            headers=headers,
            json={
                "plate_no": "OTHER",
                "province": "จังหวัดสมมติ",
                "chassis_no": "SYNTHETIC-CHASSIS",
            },
        ).json()["code"]
        == "DUPLICATE_CHASSIS"
    )
    assert (
        auth_client.patch(
            path, headers=headers, json={"version": 1, "status": "DISPOSED"}
        ).status_code
        == 422
    )
    assert auth_client.get(path, headers=headers).json()["status"] == "ACTIVE"
    updated = auth_client.patch(
        path,
        headers=headers,
        json={
            "version": 1,
            "status": "INACTIVE",
            "chassis_no": None,
        },
    ).json()
    assert updated["version"] == 2 and updated["chassis_no"] is None
    assert (
        auth_client.patch(
            path,
            headers=headers,
            json={
                "version": 2,
                "status": "DISPOSED",
                "disposed_at": "2026-10-01",
            },
        ).status_code
        == 200
    )
    assert (
        auth_client.patch(path, headers=headers, json={"version": 3, "status": "ACTIVE"}).json()[
            "code"
        ]
        == "INVALID_STATE_TRANSITION"
    )
    make_vehicle(auth_client, headers, plate_no="70-1234")
    assert db_session.execute(select(func.count()).select_from(Vehicle)).scalar_one() == 2


def test_summary_filters_and_pagination(
    auth_client: TestClient, auth_users: dict[str, AppUser], db_session: Session
) -> None:
    headers = authorization(auth_client, auth_users["viewer"])
    manager = authorization(auth_client, auth_users["manager"])
    item = make_vehicle(auth_client, manager, plate_no="A%_1")
    make_vehicle(auth_client, manager, plate_no="AXX1", status="INACTIVE")
    vehicle_id = uuid.UUID(str(item["id"]))
    for status, amount, day in (
        ("COMPLETED", "1605.00", 10),
        ("DRAFT", "999.00", 11),
        ("CANCELLED", "555.00", 12),
    ):
        db_session.add(
            MaintenanceRecord(
                vehicle_id=vehicle_id,
                plate_no_snapshot="A%_1",
                province_snapshot="สมมติ",
                maintenance_type="PM",
                status=status,
                service_start_date=date(2026, 10, day),
                subtotal=Decimal(amount),
                vat_amount=Decimal("0.00"),
                total_amount=Decimal(amount),
            )
        )
    db_session.add(PMPlan(vehicle_id=vehicle_id, name="Synthetic plan", interval_km=10000))
    db_session.flush()
    summary = auth_client.get(f"/api/v1/vehicles/{vehicle_id}/summary", headers=headers).json()
    assert summary["total_cost"] == "1605.00" and summary["completed_count"] == 1
    assert summary["latest_record"]["status"] == "CANCELLED"
    assert len(summary["pm_plans"]) == 1 and summary["pm_status"] == "OVERDUE"
    records = auth_client.get(
        f"/api/v1/vehicles/{vehicle_id}/maintenance-records",
        headers=headers,
        params={"type": "PM", "status": "COMPLETED", "from": "2026-10-10", "to": "2026-10-10"},
    ).json()
    assert len(records) == 1 and records[0]["total_amount"] == "1605.00"
    assert (
        auth_client.get("/api/v1/vehicles", headers=headers, params={"q": "%_"}).json()["total"]
        == 1
    )
    page = auth_client.get("/api/v1/vehicles?page=2&page_size=1", headers=headers).json()
    assert page["total"] == 2 and len(page["items"]) == 1
    assert auth_client.get("/api/v1/vehicles?status=INACTIVE", headers=headers).json()["total"] == 1
    assert (
        auth_client.get(f"/api/v1/vehicles/{uuid.uuid4()}", headers=headers).json()["code"]
        == "VEHICLE_NOT_FOUND"
    )


def test_delete_pending_only_and_preserve_audit(
    auth_client: TestClient, auth_users: dict[str, AppUser], db_session: Session
) -> None:
    headers = authorization(auth_client, auth_users["manager"])
    item = make_vehicle(auth_client, headers)
    path = f"/api/v1/vehicles/{item['id']}"
    assert (
        auth_client.delete(path + "?version=2", headers=headers).json()["code"]
        == "VERSION_CONFLICT"
    )
    assert auth_client.delete(path + "?version=1", headers=headers).status_code == 204
    assert auth_client.get(path, headers=headers).status_code == 404
    assert db_session.execute(
        select(AuditLog).where(AuditLog.entity_id == item["id"], AuditLog.action == "DELETE")
    ).scalar_one()
    protected = make_vehicle(auth_client, headers)
    db_session.add(
        PMPlan(
            vehicle_id=uuid.UUID(str(protected["id"])), name="Keeps reference", interval_days=180
        )
    )
    db_session.flush()
    assert (
        auth_client.delete(f"/api/v1/vehicles/{protected['id']}?version=1", headers=headers).json()[
            "code"
        ]
        == "VEHICLE_IN_USE"
    )
    assert (
        auth_client.get(f"/api/v1/vehicles/{protected['id']}", headers=headers).status_code == 200
    )


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
def test_vehicle_audit_failure_rolls_back(
    operation: str,
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import vehicles

    headers = authorization(auth_client, auth_users["manager"])
    item = make_vehicle(auth_client, headers) if operation != "create" else None

    def fail(*args: object, **kwargs: object) -> None:
        write_audit(*args, **kwargs)
        db_session.flush()
        raise RuntimeError("Synthetic registry audit failure")

    monkeypatch.setattr(vehicles, "write_audit", fail)
    with pytest.raises(RuntimeError, match="Synthetic registry audit failure"):
        if operation == "create":
            make_vehicle(auth_client, headers)
        elif operation == "update":
            auth_client.patch(
                f"/api/v1/vehicles/{item['id']}",
                headers=headers,
                json={"version": 1, "brand": "Rollback"},
            )
        else:
            auth_client.delete(f"/api/v1/vehicles/{item['id']}?version=1", headers=headers)
    assert db_session.execute(select(func.count()).select_from(Vehicle)).scalar_one() == (
        0 if item is None else 1
    )
    if item is not None:
        stored = db_session.get(Vehicle, uuid.UUID(str(item["id"])))
        assert stored.version == 1 and stored.brand is None
        assert db_session.execute(select(func.count()).select_from(VehicleChange)).scalar_one() == 1


def test_registry_lock_held_until_transaction_end(db_session: Session) -> None:
    from app.services.vehicles import lock_vehicle_writes

    lock_vehicle_writes(db_session)
    with db_session.bind.engine.connect() as other:
        assert not other.execute(
            text("SELECT pg_try_advisory_xact_lock(6835046301127)")
        ).scalar_one()
