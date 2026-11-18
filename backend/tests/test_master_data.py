"""Real JWT permissions, PostgreSQL master writes and history preservation."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.auth.dependencies import issue_token
from app.errors import APIError
from app.models import (
    AppUser,
    AuditLog,
    Garage,
    MaintenanceCategory,
    MaintenanceRecord,
    SystemSetting,
    Vehicle,
)
from app.services.master_data import lock_master_writes, require_active_garage

pytestmark = pytest.mark.postgresql


def authorization(client: TestClient, user: AppUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(user, client.app.state.settings)}"}


def make_garage(client: TestClient, headers: dict[str, str], **fields: object) -> dict[str, object]:
    response = client.post(
        "/api/v1/garages",
        headers=headers,
        json={"name": f"Synthetic Garage {uuid.uuid4().hex}", **fields},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_category(
    client: TestClient, headers: dict[str, str], **fields: object
) -> dict[str, object]:
    response = client.post(
        "/api/v1/maintenance-categories",
        headers=headers,
        json={"code": uuid.uuid4().hex, "name_th": "หมวดสมมติ", **fields},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("role", ["viewer", "clerk", "manager", "admin"])
def test_master_permissions_use_real_tokens(
    role: str, auth_client: TestClient, auth_users: dict[str, AppUser]
) -> None:
    admin = authorization(auth_client, auth_users["admin"])
    garage = make_garage(auth_client, admin)
    category = make_category(auth_client, admin)
    headers = authorization(auth_client, auth_users[role])
    for path in (
        "/api/v1/garages",
        f"/api/v1/garages/{garage['id']}",
        "/api/v1/maintenance-categories",
        f"/api/v1/maintenance-categories/{category['id']}",
    ):
        assert auth_client.get(path, headers=headers).status_code == 200
    allowed = role in {"manager", "admin"}
    created = auth_client.post("/api/v1/garages", headers=headers, json={"name": uuid.uuid4().hex})
    assert created.status_code == (201 if allowed else 403)
    created_category = auth_client.post(
        "/api/v1/maintenance-categories",
        headers=headers,
        json={"code": uuid.uuid4().hex, "name_th": "Synthetic"},
    )
    assert created_category.status_code == (201 if allowed else 403)
    updated = auth_client.patch(
        f"/api/v1/garages/{garage['id']}",
        headers=headers,
        json={"version": 1, "contact_name": "Synthetic"},
    )
    assert updated.status_code == (200 if allowed else 403)
    updated_category = auth_client.patch(
        f"/api/v1/maintenance-categories/{category['id']}",
        headers=headers,
        json={"name_th": "Synthetic updated"},
    )
    assert updated_category.status_code == (200 if allowed else 403)
    assert auth_client.delete(
        f"/api/v1/garages/{garage['id']}?version=2", headers=headers
    ).status_code == (204 if allowed else 403)
    assert auth_client.delete(
        f"/api/v1/maintenance-categories/{category['id']}", headers=headers
    ).status_code == (204 if allowed else 403)
    assert auth_client.get("/api/v1/settings", headers=headers).status_code == (
        200 if role == "admin" else 403
    )
    assert auth_client.put(
        "/api/v1/settings",
        headers=headers,
        json={"pm_due_soon_pct": 5, "pm_due_soon_km": 200, "pm_due_soon_days": 7},
    ).status_code == (200 if role == "admin" else 403)


def test_new_routes_require_authentication(auth_client: TestClient) -> None:
    identity = uuid.uuid4()
    for method, path in (
        ("get", "/api/v1/garages"),
        ("post", "/api/v1/garages"),
        ("patch", f"/api/v1/garages/{identity}"),
        ("delete", f"/api/v1/garages/{identity}"),
        ("get", "/api/v1/maintenance-categories"),
        ("post", "/api/v1/maintenance-categories"),
        ("patch", f"/api/v1/maintenance-categories/{identity}"),
        ("delete", f"/api/v1/maintenance-categories/{identity}"),
        ("get", "/api/v1/settings"),
        ("put", "/api/v1/settings"),
    ):
        assert getattr(auth_client, method)(path).status_code == 401


def test_garage_uniqueness_versions_and_nullable_fields(
    auth_client: TestClient, auth_users: dict[str, AppUser], db_session: Session
) -> None:
    headers = authorization(auth_client, auth_users["manager"])
    garage = make_garage(auth_client, headers, name=" Synthetic AB Garage ", tax_id="0123456789012")
    for body, code in (
        ({"name": "s Y n t h e t i c A B g a r a g e"}, "GARAGE_NAME_EXISTS"),
        ({"name": "Other Synthetic", "tax_id": "0123456789012"}, "GARAGE_TAX_ID_EXISTS"),
    ):
        duplicate = auth_client.post("/api/v1/garages", headers=headers, json=body)
        assert duplicate.status_code == 409 and duplicate.json()["code"] == code
    other = make_garage(auth_client, headers)
    conflict = auth_client.patch(
        f"/api/v1/garages/{other['id']}",
        headers=headers,
        json={"version": 1, "name": "SYNTHETICABGARAGE"},
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "GARAGE_NAME_EXISTS"
    # Failed updates roll back their fields and produce no UPDATE audit.
    assert auth_client.get(f"/api/v1/garages/{other['id']}", headers=headers).json() == other
    changed = auth_client.patch(
        f"/api/v1/garages/{garage['id']}",
        headers=headers,
        json={"version": 1, "tax_id": None, "contact_name": " Synthetic contact "},
    )
    assert changed.status_code == 200
    assert changed.json()["version"] == 2 and changed.json()["tax_id"] is None
    assert changed.json()["contact_name"] == "Synthetic contact"
    for method, kwargs in (
        ("patch", {"json": {"version": 1, "name": "Lost update"}}),
        ("delete", {"params": {"version": 1}}),
    ):
        stale = getattr(auth_client, method)(
            f"/api/v1/garages/{garage['id']}", headers=headers, **kwargs
        )
        assert stale.status_code == 409 and stale.json()["code"] == "VERSION_CONFLICT"
    row = db_session.get(Garage, uuid.UUID(str(garage["id"])))
    assert row is not None and row.updated_by == auth_users["manager"].id
    audits = (
        db_session.execute(
            select(AuditLog).where(AuditLog.entity_id == garage["id"]).order_by(AuditLog.at)
        )
        .scalars()
        .all()
    )
    assert {event.action for event in audits} == {"CREATE", "UPDATE"}
    updated = next(event for event in audits if event.action == "UPDATE")
    assert updated.before_json["tax_id"] == "0123456789012"
    assert updated.after_json["tax_id"] is None
    assert updated.before_json["version"] == 1 and updated.after_json["version"] == 2
    assert all(event.actor == auth_users["manager"].id for event in audits)


def test_deactivation_preserves_referenced_history_and_blocks_new_selection(
    auth_client: TestClient, auth_users: dict[str, AppUser], db_session: Session
) -> None:
    headers = authorization(auth_client, auth_users["manager"])
    garage = make_garage(auth_client, headers)
    garage_id = uuid.UUID(str(garage["id"]))
    assert require_active_garage(db_session, garage_id).is_active
    vehicle = Vehicle(
        plate_no=uuid.uuid4().hex,
        plate_no_norm=uuid.uuid4().hex,
        province="Synthetic",
        status="ACTIVE",
        origin="MTN",
        core_sync_status="PENDING_CREATE",
    )
    db_session.add(vehicle)
    db_session.flush()
    record = MaintenanceRecord(
        vehicle_id=vehicle.id,
        plate_no_snapshot=vehicle.plate_no,
        province_snapshot="Synthetic",
        maintenance_type="CM",
        status="DRAFT",
        garage_id=garage_id,
    )
    db_session.add(record)
    db_session.flush()
    response = auth_client.delete(f"/api/v1/garages/{garage_id}?version=1", headers=headers)
    assert response.status_code == 204
    with pytest.raises(APIError) as caught:
        require_active_garage(db_session, garage_id)
    assert caught.value.status_code == 422 and caught.value.code == "GARAGE_INACTIVE"
    detail = auth_client.get(f"/api/v1/garages/{garage_id}", headers=headers).json()
    assert detail["name"] == garage["name"] and detail["is_active"] is False
    assert db_session.get(MaintenanceRecord, record.id).garage_id == garage_id
    active = auth_client.get("/api/v1/garages?is_active=true", headers=headers).json()
    inactive = auth_client.get("/api/v1/garages?is_active=false", headers=headers).json()
    assert all(item["id"] != str(garage_id) for item in active)
    assert any(item["id"] == str(garage_id) for item in inactive)
    # A disabled garage still reserves its normalized name and tax identifier.
    duplicate = auth_client.post("/api/v1/garages", headers=headers, json={"name": garage["name"]})
    assert duplicate.status_code == 409
    reactivated = auth_client.patch(
        f"/api/v1/garages/{garage_id}", headers=headers, json={"version": 2, "is_active": True}
    )
    assert reactivated.status_code == 200
    assert require_active_garage(db_session, garage_id).is_active


def test_category_tree_rejects_cycles_duplicates_and_missing_parents(
    auth_client: TestClient, auth_users: dict[str, AppUser], db_session: Session
) -> None:
    headers = authorization(auth_client, auth_users["manager"])
    root = make_category(auth_client, headers)
    child = make_category(auth_client, headers, parent_id=root["id"])
    leaf = make_category(auth_client, headers, parent_id=child["id"])
    for identity, parent, status, code in (
        (root["id"], leaf["id"], 422, "CATEGORY_CYCLE"),
        (root["id"], root["id"], 422, "CATEGORY_CYCLE"),
        (leaf["id"], str(uuid.uuid4()), 404, "NOT_FOUND"),
    ):
        rejected = auth_client.patch(
            f"/api/v1/maintenance-categories/{identity}",
            headers=headers,
            json={"parent_id": parent},
        )
        assert rejected.status_code == status and rejected.json()["code"] == code
    duplicate = auth_client.post(
        "/api/v1/maintenance-categories",
        headers=headers,
        json={"code": root["code"], "name_th": "Duplicate"},
    )
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "CATEGORY_CODE_EXISTS"
    duplicate_patch = auth_client.patch(
        f"/api/v1/maintenance-categories/{child['id']}",
        headers=headers,
        json={"code": root["code"]},
    )
    assert duplicate_patch.status_code == 409
    assert (
        auth_client.delete(
            f"/api/v1/maintenance-categories/{root['id']}", headers=headers
        ).status_code
        == 204
    )
    stored_child = db_session.get(MaintenanceCategory, uuid.UUID(str(child["id"])))
    assert stored_child.parent_id == uuid.UUID(str(root["id"]))
    inactive = auth_client.get(
        "/api/v1/maintenance-categories?is_active=false", headers=headers
    ).json()
    assert any(item["id"] == root["id"] and item["name_th"] == root["name_th"] for item in inactive)
    detached = auth_client.patch(
        f"/api/v1/maintenance-categories/{child['id']}",
        headers=headers,
        json={"parent_id": None, "name_th": "Detached synthetic"},
    )
    assert detached.status_code == 200 and detached.json()["parent_id"] is None
    assert detached.json()["name_th"] == "Detached synthetic"
    events = (
        db_session.execute(select(AuditLog).where(AuditLog.entity_id == root["id"])).scalars().all()
    )
    assert {event.action for event in events} == {"CREATE", "DEACTIVATE"}


def test_settings_defaults_update_and_protected_unknown_fields(
    auth_client: TestClient, auth_users: dict[str, AppUser], db_session: Session
) -> None:
    headers = authorization(auth_client, auth_users["admin"])
    db_session.add(SystemSetting(key="unrelated_test_key", value_json={"preserve": True}))
    db_session.flush()
    defaults = {"pm_due_soon_pct": 10, "pm_due_soon_km": 500, "pm_due_soon_days": 14}
    assert auth_client.get("/api/v1/settings", headers=headers).json() == defaults
    assert db_session.execute(select(func.count()).select_from(SystemSetting)).scalar_one() == 1
    changed = {"pm_due_soon_pct": 2.5, "pm_due_soon_km": 0, "pm_due_soon_days": 7}
    response = auth_client.put("/api/v1/settings", headers=headers, json=changed)
    assert response.status_code == 200 and response.json() == changed
    assert auth_client.get("/api/v1/settings", headers=headers).json() == changed
    assert db_session.get(SystemSetting, "unrelated_test_key").value_json == {"preserve": True}
    for invalid in ({"JWT_SECRET": "forbidden", **changed}, {"pm_due_soon_km": 500}):
        rejected = auth_client.put("/api/v1/settings", headers=headers, json=invalid)
        assert rejected.status_code == 422
    events = (
        db_session.execute(select(AuditLog).where(AuditLog.entity == "system_setting"))
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].before_json == defaults and events[0].after_json == changed
    assert events[0].actor == auth_users["admin"].id and events[0].at is not None


@pytest.mark.parametrize(
    "resource,key",
    [("garage", 6835046301124), ("category", 6835046301125), ("settings", 6835046301126)],
)
def test_master_locks_exclude_a_second_transaction(
    resource: str, key: int, db_session: Session
) -> None:
    lock_master_writes(db_session, resource)
    connection = db_session.connection()
    with connection.engine.connect() as second:
        assert (
            second.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
            ).scalar_one()
            is False
        )


def test_unknown_master_ids_and_bounded_pagination(
    auth_client: TestClient, auth_users: dict[str, AppUser]
) -> None:
    headers = authorization(auth_client, auth_users["manager"])
    for resource in ("garages", "maintenance-categories"):
        path = f"/api/v1/{resource}/{uuid.uuid4()}"
        patch = {"version": 1} if resource == "garages" else {}
        for method, body in (("get", None), ("patch", patch)):
            kwargs = {"headers": headers}
            if body is not None:
                kwargs["json"] = body
            assert getattr(auth_client, method)(path, **kwargs).status_code == 404
        assert auth_client.delete(path, headers=headers, params={"version": 1}).status_code == 404
        assert auth_client.get(f"/api/v1/{resource}?limit=201", headers=headers).status_code == 422
        assert auth_client.get(f"/api/v1/{resource}?offset=-1", headers=headers).status_code == 422
    make_garage(auth_client, headers)
    make_garage(auth_client, headers)
    first = auth_client.get("/api/v1/garages?limit=1&offset=0", headers=headers).json()
    second = auth_client.get("/api/v1/garages?limit=1&offset=1", headers=headers).json()
    assert len(first) == len(second) == 1 and first[0]["id"] != second[0]["id"]
