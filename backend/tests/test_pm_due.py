"""PostgreSQL PM API, completion projections, role/version and atomic audit tests."""

import uuid
from datetime import datetime, timedelta

import pytest
from maintenance_support import create, headers, setup_record
from sqlalchemy import select

from app.models import AuditLog, OdometerReading, PMPlan
from app.services import pm_due

pytestmark = pytest.mark.postgresql
BASE = "/api/v1/pm-plans"


def setup_plan(session, client, users, **changes):
    auth, payload, vehicle, _ = setup_record(session, client, users)
    response = client.post(
        BASE,
        headers=auth,
        json={
            "vehicle_id": str(vehicle.id),
            "name": "Synthetic PM",
            "interval_km": 10000,
            "interval_days": 180,
            **changes,
        },
    )
    assert response.status_code == 201, response.text
    return auth, payload, vehicle, response.json()


def test_completed_pm_round_and_stale_version(auth_client, auth_users, db_session):
    auth, payload, vehicle, plan = setup_plan(db_session, auth_client, auth_users)
    payload["pm_plan_id"] = plan["id"]
    item = create(auth_client, auth, payload)
    response = auth_client.post(
        f"/api/v1/maintenance-records/{item['id']}/complete",
        headers=auth,
        json={"version": 1},
    )
    assert response.status_code == 200, response.text
    due = auth_client.get("/api/v1/pm-due", headers=auth, params={"vehicle_id": str(vehicle.id)})
    row = due.json()["items"][0]
    assert row["last_done_at"] == "2026-10-10" and row["last_done_km"] == 120500
    assert row["next_due_at"] == "2027-04-08" and row["next_due_km"] == 130500
    assert row["current_km"] == 120500 and row["status"] == "OK"
    path = BASE + "/" + plan["id"]
    stale = auth_client.patch(path, headers=auth, json={"version": 1, "name": "stale"})
    assert stale.status_code == 409 and stale.json()["code"] == "VERSION_CONFLICT"
    assert auth_client.delete(path, headers=auth, params={"version": 1}).status_code == 409
    current = auth_client.get(path, headers=auth).json()
    assert current["version"] == 2
    result = auth_client.patch(path, headers=auth, json={"version": 2, "name": "Updated PM"})
    assert result.status_code == 200 and result.json()["version"] == 3
    summary = auth_client.get(f"/api/v1/vehicles/{vehicle.id}/summary", headers=auth).json()
    assert summary["pm_status"] == row["status"]


def test_never_done_filter_and_deactivation(auth_client, auth_users, db_session):
    auth, _, vehicle, plan = setup_plan(db_session, auth_client, auth_users)
    params = {"vehicle_id": str(vehicle.id), "status": "DUE_SOON,OVERDUE"}
    result = auth_client.get("/api/v1/pm-due", headers=auth, params=params).json()
    assert result["total"] == 1 and result["items"][0]["reason"] == "NEVER_DONE"
    assert result["items"][0]["status"] == "OVERDUE"
    assert auth_client.get("/api/v1/pm-due?status=NEVER_DONE", headers=auth).status_code == 422
    assert (
        auth_client.get(
            "/api/v1/pm-due",
            headers=auth,
            params={**params, "offset": 1},
        ).json()["items"]
        == []
    )
    path = BASE + "/" + plan["id"]
    assert auth_client.delete(path, headers=auth, params={"version": 1}).status_code == 204
    assert auth_client.get(path, headers=auth).json()["is_active"] is False
    assert db_session.get(PMPlan, uuid.UUID(plan["id"])) is not None
    assert auth_client.get("/api/v1/pm-due", headers=auth, params=params).json()["total"] == 0
    assert (
        auth_client.get(
            f"/api/v1/vehicles/{vehicle.id}/summary",
            headers=auth,
        ).json()["pm_status"]
        is None
    )
    events = db_session.execute(select(AuditLog).where(AuditLog.entity_id == plan["id"])).scalars()
    assert {event.action for event in events} == {"CREATE", "DEACTIVATE"}


def test_settings_latest_reading_and_summary(auth_client, auth_users, db_session):
    now = datetime.now(pm_due.BANGKOK)
    auth, _, vehicle, plan = setup_plan(
        db_session,
        auth_client,
        auth_users,
        last_done_at=now.date().isoformat(),
        last_done_km=1000,
        interval_km=1000,
    )
    for days, value in ((-2, 9999), (-1, 1400), (1, 99999)):
        db_session.add(
            OdometerReading(
                vehicle_id=vehicle.id,
                source="MANUAL",
                source_ref=uuid.uuid4().hex,
                recorded_at=now + timedelta(days=days),
                reading_km=value,
            )
        )
    db_session.flush()
    params = {"vehicle_id": str(vehicle.id)}
    row = auth_client.get("/api/v1/pm-due", headers=auth, params=params).json()["items"][0]
    assert row["current_km"] == 1400 and row["status"] == "OK"
    settings = auth_client.put(
        "/api/v1/settings",
        headers=headers(auth_client, auth_users["admin"]),
        json={"pm_due_soon_pct": 10, "pm_due_soon_km": 600, "pm_due_soon_days": 14},
    )
    assert settings.status_code == 200
    assert (
        auth_client.get("/api/v1/pm-due", headers=auth, params=params).json()["items"][0]["status"]
        == "DUE_SOON"
    )
    assert (
        auth_client.get(f"/api/v1/vehicles/{vehicle.id}/summary", headers=auth).json()["pm_status"]
        == "DUE_SOON"
    )
    assert (
        auth_client.patch(
            BASE + "/" + plan["id"], headers=auth, json={"version": 1, "interval_days": None}
        ).status_code
        == 200
    )


@pytest.mark.parametrize("role", ["viewer", "clerk", "manager", "admin"])
def test_roles(auth_client, auth_users, db_session, role):
    _, _, vehicle, plan = setup_plan(db_session, auth_client, auth_users)
    auth = headers(auth_client, auth_users[role])
    for path in (BASE, BASE + "/" + plan["id"], "/api/v1/pm-due"):
        assert auth_client.get(path, headers=auth).status_code == 200
        assert auth_client.get(path).status_code == 401
    allowed = role in {"manager", "admin"}
    response = auth_client.post(
        BASE,
        headers=auth,
        json={"vehicle_id": str(vehicle.id), "name": "Another", "interval_days": 30},
    )
    assert response.status_code == (201 if allowed else 403)
    response = auth_client.patch(
        BASE + "/" + plan["id"], headers=auth, json={"version": 1, "name": "Role update"}
    )
    assert response.status_code == (200 if allowed else 403)
    response = auth_client.delete(
        BASE + "/" + plan["id"], headers=auth, params={"version": 2 if allowed else 1}
    )
    assert response.status_code == (204 if allowed else 403)


def test_validation_and_rollback(auth_client, auth_users, db_session, monkeypatch):
    auth, _, vehicle, plan = setup_plan(db_session, auth_client, auth_users)
    path = BASE + "/" + plan["id"]
    for patch in (
        {"interval_km": None, "interval_days": None},
        {"name": None},
        {"interval_days": 0},
        {"category_id": str(uuid.uuid4())},
    ):
        response = auth_client.patch(path, headers=auth, json={"version": 1, **patch})
        assert response.status_code == 422, response.text
        assert auth_client.get(path, headers=auth).json() == plan
    assert (
        auth_client.post(
            BASE, headers=auth, json={"vehicle_id": str(vehicle.id), "name": "No interval"}
        ).status_code
        == 422
    )
    assert (
        auth_client.patch(
            BASE + "/" + str(uuid.uuid4()), headers=auth, json={"version": 1, "name": "Missing"}
        ).status_code
        == 404
    )

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(pm_due, "write_audit", fail)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        auth_client.patch(path, headers=auth, json={"version": 1, "name": "Rollback"})
    db_session.expire_all()
    assert auth_client.get(path, headers=auth).json() == plan


def test_missing_mileage_falls_back_to_days(auth_client, auth_users, db_session):
    today = datetime.now(pm_due.BANGKOK).date()
    auth, _, vehicle, plan = setup_plan(
        db_session,
        auth_client,
        auth_users,
        last_done_at=(today - timedelta(days=166)).isoformat(),
        last_done_km=1000,
    )
    params = {"vehicle_id": str(vehicle.id)}
    row = auth_client.get("/api/v1/pm-due", headers=auth, params=params).json()["items"][0]
    assert row["current_km"] is None and row["remaining_km"] is None
    assert row["remaining_days"] == 14 and row["status"] == "DUE_SOON"
    response = auth_client.patch(
        BASE + "/" + plan["id"], headers=auth, json={"version": 1, "interval_days": None}
    )
    assert response.status_code == 200
    row = auth_client.get("/api/v1/pm-due", headers=auth, params=params).json()["items"][0]
    assert row["status"] is None and row["reason"] == "INSUFFICIENT_DATA"
    assert (
        auth_client.get(f"/api/v1/vehicles/{vehicle.id}/summary", headers=auth).json()["pm_status"]
        is None
    )
