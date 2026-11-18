"""Synthetic PostgreSQL integration for preview/decide/apply and rollback."""

import csv
import io
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import issue_token
from app.models import AppUser, AuditLog, MaintenanceRecord, Vehicle, VehicleImportBatch
from app.schemas.vehicles import VehiclePatch
from app.services import vehicle_import
from app.services.vehicle_import import apply_import
from app.services.vehicles import update_vehicle

pytestmark = pytest.mark.postgresql


def headers(client: TestClient, user: AppUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(user, client.app.state.settings)}"}


def body(*rows: dict[str, object]) -> bytes:
    columns = [
        "core_asset_id",
        "plate_no",
        "province",
        "brand",
        "chassis_no",
        "status",
        "disposed_at",
        "year",
    ]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({"province": "จังหวัดสมมติ", "status": "ACTIVE", **row})
    return stream.getvalue().encode("utf-8-sig")


def upload(client: TestClient, user: AppUser, data: bytes) -> dict:
    response = client.post(
        "/api/v1/vehicle-imports",
        headers=headers(client, user),
        files={"file": ("../../synthetic.csv", data, "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["file_name"] == "synthetic.csv"
    return response.json()


def existing(
    session: Session, user: AppUser, core: str | None, plate: str, **fields: object
) -> Vehicle:
    item = Vehicle(
        core_asset_id=core,
        plate_no=plate,
        plate_no_norm=plate.replace("-", ""),
        province="จังหวัดสมมติ",
        origin="CORE_IMPORT" if core else "MTN",
        core_sync_status="SYNCED" if core else "PENDING_CREATE",
        created_by=user.id,
        updated_by=user.id,
        **fields,
    )
    session.add(item)
    session.flush()
    return item


def act(client: TestClient, user: AppUser, batch: dict, action: str, **kwargs):
    return client.post(
        f"/api/v1/vehicle-imports/{batch['id']}/{action}", headers=headers(client, user), **kwargs
    )


def test_preview_all_outcomes_atomic_apply_and_history(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    actor = auth_users["manager"]
    changed = existing(db_session, actor, "A-2", "70-5555")
    unchanged = existing(db_session, actor, "A-3", "70-3333")
    conflict = existing(db_session, actor, "A-4", "70-4444", brand="LOCAL")
    conflict.core_sync_status = "PENDING_UPDATE"
    omitted = existing(db_session, actor, "omitted", "70-9999")
    record = MaintenanceRecord(
        vehicle_id=changed.id,
        maintenance_type="PM",
        plate_no_snapshot=changed.plate_no,
        province_snapshot=changed.province,
    )
    db_session.add(record)
    db_session.flush()
    data = body(
        {"core_asset_id": "A-1", "plate_no": "70-1234", "year": "2569"},
        {"core_asset_id": "A-2", "plate_no": "71-0001"},
        {"core_asset_id": "A-3", "plate_no": "70-3333"},
        {"core_asset_id": "A-4", "plate_no": "70-4444", "brand": "CORE"},
        {"core_asset_id": "A-5", "plate_no": ""},
    )
    batch = upload(auth_client, actor, data)
    assert [row["result"] for row in batch["rows"]] == [
        "NEW",
        "UPDATE",
        "UNCHANGED",
        "CONFLICT",
        "ERROR",
    ]
    assert str(omitted.id) in batch["summary"]["not_found_in_file"]
    assert changed.plate_no == "70-5555"  # Preview writes no vehicles.
    assert act(auth_client, actor, batch, "apply").json()["code"] == "IMPORT_UNRESOLVED_CONFLICT"
    assert (
        act(
            auth_client,
            actor,
            batch,
            "resolutions",
            json={
                "resolutions": [
                    {"row_no": 4, "resolution": "USE_CORE"},
                ]
            },
        ).status_code
        == 200
    )
    result = act(auth_client, actor, batch, "apply")
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "APPLIED"
    assert result.json()["summary"]["applied_rows"] == 3
    assert changed.plate_no == "71-0001" and conflict.brand == "CORE"
    assert unchanged.version == 1 and omitted.version == 1
    assert conflict.core_sync_status == "SYNCED"
    assert (
        db_session.execute(select(Vehicle).where(Vehicle.core_asset_id == "A-1")).scalar_one().year
        == 2026
    )
    detail = auth_client.get(
        f"/api/v1/vehicles/{changed.id}", headers=headers(auth_client, actor)
    ).json()
    assert [row["plate_no"] for row in detail["plate_history"]] == ["70-5555", "71-0001"]
    assert detail["plate_history"][1]["source"] == "CORE_IMPORT"
    assert record.plate_no_snapshot == "70-5555"
    assert act(auth_client, actor, batch, "apply").json()["code"] == "IMPORT_ALREADY_APPLIED"
    audit = db_session.execute(
        select(AuditLog).where(
            AuditLog.entity_id == str(changed.id), AuditLog.action == "IMPORT_UPDATE"
        )
    ).scalar_one()
    assert audit.before_json["plate_no"] == "70-5555" and audit.after_json["plate_no"] == "71-0001"
    assert audit.actor == actor.id and audit.at is not None


@pytest.mark.parametrize("previous_status", ["APPLIED", "VALIDATED", "DISCARDED"])
def test_repeat_file_is_a_noop_even_after_a_local_edit(
    previous_status: str,
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    actor = auth_users["manager"]
    data = body({"core_asset_id": "A-1", "plate_no": "1"})
    first = upload(auth_client, actor, data)
    if previous_status != "VALIDATED":
        assert (
            act(
                auth_client, actor, first, "apply" if previous_status == "APPLIED" else "discard"
            ).status_code
            == 200
        )
    if previous_status == "APPLIED":
        item = db_session.execute(select(Vehicle)).scalar_one()
        update_vehicle(
            db_session, item.id, VehiclePatch(version=item.version, brand="retain"), actor
        )
        version = item.version
    repeated = upload(auth_client, actor, data)
    assert repeated["warnings"] == ["IMPORT_FILE_SEEN_BEFORE"]
    assert all(row["result"] == "UNCHANGED" for row in repeated["rows"])
    assert act(auth_client, actor, repeated, "apply").status_code == 200
    if previous_status == "APPLIED":
        assert item.brand == "retain" and item.version == version
    else:
        assert db_session.execute(select(func.count()).select_from(Vehicle)).scalar_one() == 0


@pytest.mark.parametrize("resolution", ["USE_CORE", "KEEP_MNT"])
def test_local_conflict_decisions_preserve_or_replace_pending_changes(
    resolution: str,
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    actor = auth_users["manager"]
    item = existing(db_session, actor, "A", "1", brand="old")
    update_vehicle(db_session, item.id, VehiclePatch(version=1, brand="LOCAL"), actor)
    batch = upload(
        auth_client, actor, body({"core_asset_id": "A", "plate_no": "1", "brand": "CORE"})
    )
    assert (
        act(
            auth_client,
            actor,
            batch,
            "resolutions",
            json={"resolutions": [{"row_no": 1, "resolution": resolution}]},
        ).status_code
        == 200
    )
    assert act(auth_client, actor, batch, "apply").status_code == 200
    assert item.brand == ("CORE" if resolution == "USE_CORE" else "LOCAL")
    assert item.core_sync_status == ("SYNCED" if resolution == "USE_CORE" else "PENDING_UPDATE")
    if resolution == "USE_CORE":
        update_vehicle(
            db_session, item.id, VehiclePatch(version=item.version, model="new model"), actor
        )
    response = auth_client.get("/api/v1/vehicles/core-export", headers=headers(auth_client, actor))
    row = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))[0]
    assert row["changed_fields"] == ("model" if resolution == "USE_CORE" else "brand")


def test_explicit_link_adopts_core_id_and_does_not_create_another_vehicle(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    actor = auth_users["manager"]
    item = existing(db_session, actor, None, "72-8888")
    batch = upload(auth_client, actor, body({"core_asset_id": "A-010", "plate_no": "72 8888"}))
    assert batch["rows"][0]["result"] == "CONFLICT"
    assert batch["rows"][0]["matched_vehicle_id"] == str(item.id)
    invalid = act(
        auth_client,
        actor,
        batch,
        "resolutions",
        json={"resolutions": [{"row_no": 1, "resolution": "USE_CORE"}]},
    )
    assert invalid.status_code == 422
    assert (
        act(
            auth_client,
            actor,
            batch,
            "resolutions",
            json={"resolutions": [{"row_no": 1, "resolution": "LINK"}]},
        ).status_code
        == 200
    )
    assert act(auth_client, actor, batch, "apply").status_code == 200
    assert item.core_asset_id == "A-010" and item.core_sync_status == "SYNCED"
    assert item.origin == "MTN"
    assert db_session.execute(select(func.count()).select_from(Vehicle)).scalar_one() == 1


@pytest.mark.parametrize("mutation", ["edit", "new_identity"])
def test_changed_registry_rejects_stale_preview_without_partial_apply(
    mutation: str,
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    actor = auth_users["manager"]
    item = existing(db_session, actor, "A", "1")
    batch = upload(
        auth_client,
        actor,
        body(
            {"core_asset_id": "B", "plate_no": "2"},
            {"core_asset_id": "A", "plate_no": "1", "brand": "CORE"},
        ),
    )
    if mutation == "edit":
        update_vehicle(db_session, item.id, VehiclePatch(version=1, brand="retain"), actor)
    else:
        existing(db_session, actor, "C", "2")
        db_session.commit()
    response = act(auth_client, actor, batch, "apply")
    assert response.status_code == 409 and response.json()["code"] == "IMPORT_STALE"
    assert (
        db_session.execute(select(Vehicle.id).where(Vehicle.core_asset_id == "B")).first() is None
    )
    assert db_session.get(VehicleImportBatch, uuid.UUID(batch["id"])).status == "VALIDATED"


def test_midway_audit_failure_rolls_back_all_vehicle_history_and_batch_changes(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = auth_users["manager"]
    batch = upload(
        auth_client,
        actor,
        body(
            {"core_asset_id": "A", "plate_no": "1"},
            {"core_asset_id": "B", "plate_no": "2"},
        ),
    )
    writer = vehicle_import.write_audit
    writes = 0

    def broken(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("synthetic audit failure")
        return writer(*args, **kwargs)

    before = db_session.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    monkeypatch.setattr(vehicle_import, "write_audit", broken)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        apply_import(db_session, uuid.UUID(batch["id"]), actor)
    assert writes == 2
    assert db_session.execute(select(func.count()).select_from(Vehicle)).scalar_one() == 0
    assert db_session.execute(select(func.count()).select_from(AuditLog)).scalar_one() == before
    assert db_session.get(VehicleImportBatch, uuid.UUID(batch["id"])).status == "VALIDATED"
    monkeypatch.setattr(vehicle_import, "write_audit", writer)
    assert act(auth_client, actor, batch, "apply").status_code == 200


@pytest.mark.parametrize("role", ["viewer", "clerk", "manager", "admin"])
def test_import_permissions_and_discard(
    role: str,
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
) -> None:
    data = body({"core_asset_id": "A", "plate_no": "1"})
    actor = auth_users[role]
    batch = upload(auth_client, auth_users["admin"], data)
    allowed = role in {"manager", "admin"}
    path = f"/api/v1/vehicle-imports/{batch['id']}"
    assert auth_client.get(path, headers=headers(auth_client, actor)).status_code == (
        200 if allowed else 403
    )
    response = auth_client.post(
        "/api/v1/vehicle-imports",
        headers=headers(auth_client, actor),
        files={"file": ("test.csv", data)},
    )
    assert response.status_code == (201 if allowed else 403)
    for action in ("apply", "discard"):
        if not allowed:
            assert act(auth_client, actor, batch, action).status_code == 403
    assert act(
        auth_client,
        actor,
        batch,
        "resolutions",
        json={"resolutions": [{"row_no": 1, "resolution": "LINK"}]},
    ).status_code == (422 if allowed else 403)
    if allowed:
        assert act(auth_client, actor, batch, "discard").json()["status"] == "DISCARDED"
        assert act(auth_client, actor, batch, "apply").json()["code"] == "INVALID_STATE_TRANSITION"
    assert auth_client.get(path).status_code == 401


def test_identity_collisions_and_disposed_reactivation_are_row_errors(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    actor = auth_users["manager"]
    existing(db_session, actor, "old", "1", chassis_no="CHASSIS")
    existing(
        db_session, actor, "disposed", "3", status="DISPOSED", disposed_at=datetime.now(UTC).date()
    )
    batch = upload(
        auth_client,
        actor,
        body(
            {"core_asset_id": "new", "plate_no": "1"},
            {"core_asset_id": "new2", "plate_no": "2", "chassis_no": "CHASSIS"},
            {"core_asset_id": "disposed", "plate_no": "3"},
            {"core_asset_id": "dup", "plate_no": "4"},
            {"core_asset_id": "dup", "plate_no": "5"},
            {"core_asset_id": "x", "plate_no": "6"},
            {"core_asset_id": "y", "plate_no": "6"},
        ),
    )
    assert [row["messages_json"][0] for row in batch["rows"]] == [
        "DUPLICATE_PLATE",
        "DUPLICATE_CHASSIS",
        "INVALID_STATE_TRANSITION",
        "DUPLICATE_CORE_ASSET",
        "DUPLICATE_CORE_ASSET",
        "DUPLICATE_PLATE",
        "DUPLICATE_PLATE",
    ]
    assert act(auth_client, actor, batch, "apply").status_code == 200
    assert db_session.execute(select(func.count()).select_from(Vehicle)).scalar_one() == 2


def test_invalid_file_has_no_batch_or_audit(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    before = db_session.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    response = auth_client.post(
        "/api/v1/vehicle-imports",
        headers=headers(auth_client, auth_users["manager"]),
        files={"file": ("x.csv", b"<xml/>")},
    )
    assert response.status_code == 422 and response.json()["code"] == "IMPORT_FILE_INVALID"
    assert (
        db_session.execute(select(func.count()).select_from(VehicleImportBatch)).scalar_one() == 0
    )
    assert db_session.execute(select(func.count()).select_from(AuditLog)).scalar_one() == before


def test_stale_batch_can_be_reviewed_again_without_bypassing_file_deduplication(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    actor = auth_users["manager"]
    item = existing(db_session, actor, "A", "1")
    data = body({"core_asset_id": "A", "plate_no": "1", "brand": "CORE"})
    batch = upload(auth_client, actor, data)
    update_vehicle(db_session, item.id, VehiclePatch(version=1, brand="LOCAL"), actor)
    assert act(auth_client, actor, batch, "apply").json()["code"] == "IMPORT_STALE"
    refreshed = act(auth_client, actor, batch, "revalidate")
    assert refreshed.status_code == 200
    row = refreshed.json()["rows"][0]
    assert row["result"] == "CONFLICT" and row["resolution"] is None
    assert act(auth_client, actor, batch, "apply").json()["code"] == "IMPORT_UNRESOLVED_CONFLICT"
    assert (
        act(
            auth_client,
            actor,
            batch,
            "resolutions",
            json={
                "resolutions": [
                    {"row_no": 1, "resolution": "KEEP_MNT"},
                ]
            },
        ).status_code
        == 200
    )
    # Refreshing again invalidates old decisions, even if the version is unchanged.
    assert act(auth_client, actor, batch, "revalidate").json()["rows"][0]["resolution"] is None
    assert (
        act(
            auth_client,
            actor,
            batch,
            "resolutions",
            json={
                "resolutions": [
                    {"row_no": 1, "resolution": "KEEP_MNT"},
                ]
            },
        ).status_code
        == 200
    )
    assert act(auth_client, actor, batch, "apply").status_code == 200
    assert item.brand == "LOCAL" and item.core_sync_status == "PENDING_UPDATE"
    repeated = upload(auth_client, actor, data)
    assert (
        act(auth_client, actor, repeated, "revalidate").json()["code"] == "IMPORT_FILE_SEEN_BEFORE"
    )


def test_resolution_failure_rolls_back_earlier_decision_in_same_request(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    actor = auth_users["manager"]
    item = existing(db_session, actor, "A", "1", brand="LOCAL")
    item.core_sync_status = "PENDING_UPDATE"
    db_session.flush()
    batch = upload(
        auth_client,
        actor,
        body(
            {"core_asset_id": "A", "plate_no": "1", "brand": "CORE"},
            {"core_asset_id": "B", "plate_no": "2"},
        ),
    )
    assert (
        act(
            auth_client,
            actor,
            batch,
            "resolutions",
            json={
                "resolutions": [
                    {"row_no": 1, "resolution": "USE_CORE"},
                    {"row_no": 2, "resolution": "LINK"},
                ]
            },
        ).status_code
        == 422
    )
    detail = auth_client.get(
        f"/api/v1/vehicle-imports/{batch['id']}", headers=headers(auth_client, actor)
    ).json()
    assert detail["rows"][0]["resolution"] is None
    assert item.brand == "LOCAL"


def test_equal_exported_core_values_confirm_sync_without_changing_identity(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    actor = auth_users["manager"]
    item = existing(db_session, actor, "A", "1")
    item.core_sync_status = "EXPORTED"
    db_session.flush()
    batch = upload(auth_client, actor, body({"core_asset_id": "A", "plate_no": "1"}))
    assert batch["rows"][0]["result"] == "UNCHANGED"
    assert act(auth_client, actor, batch, "apply").status_code == 200
    assert item.core_sync_status == "SYNCED" and item.version == 2


def test_upload_holds_the_registry_identity_lock(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    from sqlalchemy import text

    upload(auth_client, auth_users["manager"], body({"core_asset_id": "A", "plate_no": "1"}))
    with db_session.bind.engine.connect() as other:
        assert not other.execute(
            text("SELECT pg_try_advisory_xact_lock(6835046301127)")
        ).scalar_one()
