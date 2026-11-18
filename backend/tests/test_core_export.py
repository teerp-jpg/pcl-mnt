"""CORE lifecycle, atomic export and real JWT authorization on PostgreSQL."""

import csv
import io
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import issue_token
from app.models import AppUser, AuditLog, CoreExportBatch, Vehicle, VehicleChange
from app.services.audit import write_audit
from app.services.vehicles import export_core

pytestmark = pytest.mark.postgresql


def headers(client: TestClient, users: dict[str, AppUser]) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(users['manager'], client.app.state.settings)}"}


def create(
    client: TestClient, authorization: dict[str, str], **fields: object
) -> dict[str, object]:
    response = client.post(
        "/api/v1/vehicles",
        headers=authorization,
        json={
            "plate_no": uuid.uuid4().hex,
            "province": "จังหวัดสมมติ",
            **fields,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def rows(response: httpx.Response) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


def test_create_export_edit_reexport_and_acknowledge(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    authorization = headers(auth_client, auth_users)
    item = create(auth_client, authorization, plate_no="70-1234", brand="=DANGER", year=2569)
    path = f"/api/v1/vehicles/{item['id']}"
    assert item["origin"] == "MTN" and item["core_sync_status"] == "PENDING_CREATE"
    assert (
        auth_client.post(
            path + "/confirm-core-sync",
            headers=authorization,
            json={"core_asset_id": "SYNTHETIC-001"},
        ).json()["code"]
        == "INVALID_STATE_TRANSITION"
    )
    exported = auth_client.get("/api/v1/vehicles/core-export", headers=authorization)
    assert exported.status_code == 200
    assert exported.headers["cache-control"] == "no-store"
    assert "attachment" in exported.headers["content-disposition"]
    row = rows(exported)[0]
    assert row["change_type"] == "CREATE" and row["mnt_vehicle_id"] == item["id"]
    assert row["brand"] == "'=DANGER" and row["year"] == "2026"
    assert "plate_no" in row["changed_fields"].split(",")
    after_export = auth_client.get(path, headers=authorization).json()
    assert after_export["core_sync_status"] == "EXPORTED" and after_export["brand"] == "=DANGER"
    assert (
        auth_client.delete(
            path + f"?version={after_export['version']}", headers=authorization
        ).json()["code"]
        == "VEHICLE_DELETE_NOT_ALLOWED"
    )
    assert rows(auth_client.get("/api/v1/vehicles/core-export", headers=authorization)) == []
    edited = auth_client.patch(
        path,
        headers=authorization,
        json={
            "version": after_export["version"],
            "brand": "Updated",
        },
    ).json()
    assert edited["core_sync_status"] == "PENDING_CREATE"
    assert (
        auth_client.post(
            path + "/confirm-core-sync",
            headers=authorization,
            json={"core_asset_id": "SYNTHETIC-001"},
        ).status_code
        == 409
    )
    second = rows(auth_client.get("/api/v1/vehicles/core-export", headers=authorization))[0]
    assert second["change_type"] == "CREATE" and second["changed_fields"] == "brand"
    assert (
        auth_client.post(
            path + "/confirm-core-sync",
            headers=authorization,
            json={"core_asset_id": "SYNTHETIC-001", "version": 1},
        ).json()["code"]
        == "VERSION_CONFLICT"
    )
    confirmed = auth_client.post(
        path + "/confirm-core-sync", headers=authorization, json={"core_asset_id": "SYNTHETIC-001"}
    ).json()
    assert confirmed["core_sync_status"] == "SYNCED"
    assert confirmed["core_asset_id"] == "SYNTHETIC-001"
    updated = auth_client.patch(
        path,
        headers=authorization,
        json={
            "version": confirmed["version"],
            "model": "New model",
        },
    ).json()
    assert updated["core_sync_status"] == "PENDING_UPDATE"
    third = rows(auth_client.get("/api/v1/vehicles/core-export", headers=authorization))[0]
    assert third["change_type"] == "UPDATE" and third["changed_fields"] == "model"
    assert (
        auth_client.post(
            path + "/confirm-core-sync", headers=authorization, json={"core_asset_id": "WRONG-ID"}
        ).json()["code"]
        == "CORE_ID_MISMATCH"
    )
    assert (
        auth_client.post(
            path + "/confirm-core-sync",
            headers=authorization,
            json={"core_asset_id": "SYNTHETIC-001"},
        ).status_code
        == 200
    )
    changes = db_session.execute(select(VehicleChange)).scalars().all()
    assert all(change.export_batch_id is not None for change in changes)
    events = (
        db_session.execute(select(AuditLog).where(AuditLog.entity_id == item["id"])).scalars().all()
    )
    assert {"CREATE", "UPDATE", "CORE_EXPORT", "SYNC_CONFIRM"} <= {event.action for event in events}


def test_core_imported_vehicle_changes_and_unchanged_patch(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    authorization = headers(auth_client, auth_users)
    vehicle = Vehicle(
        core_asset_id="SYNTHETIC-CORE",
        plate_no="70-5555",
        plate_no_norm="705555",
        province="จังหวัดสมมติ",
        origin="CORE_IMPORT",
        core_sync_status="SYNCED",
    )
    db_session.add(vehicle)
    db_session.flush()
    path = f"/api/v1/vehicles/{vehicle.id}"
    assert (
        auth_client.patch(
            path, headers=authorization, json={"version": 1, "plate_no": "70-5555"}
        ).json()["version"]
        == 1
    )
    changed = auth_client.patch(
        path, headers=authorization, json={"version": 1, "plate_no": "71-0001"}
    )
    assert changed.status_code == 200 and changed.json()["core_sync_status"] == "PENDING_UPDATE"
    changed = auth_client.patch(
        path, headers=authorization, json={"version": 2, "brand": "Synthetic"}
    )
    assert changed.status_code == 200
    row = rows(auth_client.get("/api/v1/vehicles/core-export", headers=authorization))[0]
    assert row["change_type"] == "UPDATE" and row["changed_fields"] == "plate_no,brand"
    history = auth_client.get(path, headers=authorization).json()["plate_history"]
    assert len(history) == 2 and history[0]["source"] == "CORE_IMPORT"
    assert auth_client.get("/api/v1/vehicles?q=70-5555", headers=authorization).json()["total"] == 1


def test_duplicate_core_id_keeps_export_pending_ack(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    authorization = headers(auth_client, auth_users)
    first = create(auth_client, authorization)
    second = create(auth_client, authorization)
    auth_client.get("/api/v1/vehicles/core-export", headers=authorization)
    assert (
        auth_client.post(
            f"/api/v1/vehicles/{first['id']}/confirm-core-sync",
            headers=authorization,
            json={"core_asset_id": "SYNTHETIC-UNIQUE"},
        ).status_code
        == 200
    )
    response = auth_client.post(
        f"/api/v1/vehicles/{second['id']}/confirm-core-sync",
        headers=authorization,
        json={"core_asset_id": "SYNTHETIC-UNIQUE"},
    )
    assert response.status_code == 409 and response.json()["code"] == "DUPLICATE_CORE_ASSET"
    assert db_session.get(Vehicle, uuid.UUID(str(second["id"]))).core_sync_status == "EXPORTED"


@pytest.mark.parametrize("failure", ["sink", "audit"])
def test_export_failure_rolls_back_state_batch_and_change_links(
    failure: str,
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import vehicles

    authorization = headers(auth_client, auth_users)
    item = create(auth_client, authorization)
    second = create(auth_client, authorization)

    class BrokenSink:
        def export(self, rows: object) -> bytes:
            raise RuntimeError("Synthetic export failure")

    def broken_audit(*args: object, **kwargs: object) -> None:
        write_audit(*args, **kwargs)
        db_session.flush()
        raise RuntimeError("Synthetic export failure")

    if failure == "audit":
        monkeypatch.setattr(vehicles, "write_audit", broken_audit)
    with pytest.raises(RuntimeError, match="Synthetic export failure"):
        export_core(db_session, auth_users["manager"], BrokenSink() if failure == "sink" else None)
    vehicle = db_session.get(Vehicle, uuid.UUID(str(item["id"])))
    assert vehicle.core_sync_status == "PENDING_CREATE" and vehicle.version == 1
    vehicle = db_session.get(Vehicle, uuid.UUID(str(second["id"])))
    assert vehicle.core_sync_status == "PENDING_CREATE" and vehicle.version == 1
    assert db_session.execute(select(func.count()).select_from(CoreExportBatch)).scalar_one() == 0
    assert all(
        change.export_batch_id is None
        for change in db_session.execute(select(VehicleChange)).scalars()
    )
    assert (
        db_session.execute(select(AuditLog).where(AuditLog.action == "CORE_EXPORT")).first() is None
    )


@pytest.mark.parametrize(
    "extra", [{"Origin": "https://untrusted.invalid"}, {"Sec-Fetch-Site": "cross-site"}]
)
def test_state_changing_get_rejects_foreign_origin(
    auth_client: TestClient, auth_users: dict[str, AppUser], extra: dict[str, str]
) -> None:
    authorization = headers(auth_client, auth_users)
    item = create(auth_client, authorization)
    response = auth_client.get("/api/v1/vehicles/core-export", headers={**authorization, **extra})
    assert response.status_code == 403
    assert (
        auth_client.get(f"/api/v1/vehicles/{item['id']}", headers=authorization).json()[
            "core_sync_status"
        ]
        == "PENDING_CREATE"
    )
