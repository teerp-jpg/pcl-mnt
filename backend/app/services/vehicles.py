"""Registry writes, history and file exchange share the audit transaction."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import APIError
from app.integrations.core import CORE_COLUMNS, CoreChangeSink, CSVCoreChangeSink
from app.models import (
    AppUser,
    CoreExportBatch,
    MaintenanceRecord,
    Vehicle,
    VehicleChange,
    VehiclePlateHistory,
)
from app.schemas.vehicles import (
    ConfirmCoreSync,
    VehicleCreate,
    VehiclePatch,
    VehicleView,
    normalize_plate,
)
from app.services.audit import write_audit


def lock_vehicle_writes(session: Session) -> None:
    # Small registry: one collection lock serializes plate/chassis/CORE identities
    # and export selection. T6 must take this same lock before import apply.
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 6835046301127})


def get_vehicle(session: Session, identity: uuid.UUID, *, lock: bool = False) -> Vehicle:
    statement = (
        select(Vehicle).where(Vehicle.id == identity).execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    item = session.execute(statement).scalar_one_or_none()
    if item is None:
        raise APIError(404, "VEHICLE_NOT_FOUND", "Vehicle not found")
    return item


def snapshot(item: Vehicle) -> dict[str, object]:
    return VehicleView.model_validate(item).model_dump(mode="json")


def _unique(session: Session, item: Vehicle) -> None:
    checks = [
        (Vehicle.chassis_no == item.chassis_no, item.chassis_no is not None, "DUPLICATE_CHASSIS"),
        (
            Vehicle.core_asset_id == item.core_asset_id,
            item.core_asset_id is not None,
            "DUPLICATE_CORE_ASSET",
        ),
        (
            (Vehicle.plate_no_norm == item.plate_no_norm)
            & (Vehicle.province == item.province)
            & (Vehicle.status != "DISPOSED"),
            item.status != "DISPOSED",
            "DUPLICATE_PLATE",
        ),
    ]
    for condition, enabled, code in checks:
        if (
            enabled
            and session.execute(select(Vehicle.id).where(Vehicle.id != item.id, condition)).first()
        ):
            raise APIError(409, code, "Vehicle identity already exists")


def _integrity(exc: IntegrityError) -> None:
    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    codes = {
        "uq_vehicle_live_plate": "DUPLICATE_PLATE",
        "vehicle_chassis_no_key": "DUPLICATE_CHASSIS",
        "vehicle_core_asset_id_key": "DUPLICATE_CORE_ASSET",
    }
    if constraint in codes:
        raise APIError(409, codes[constraint], "Vehicle identity already exists") from None
    if getattr(exc.orig, "sqlstate", None) == "23503":
        raise APIError(409, "VEHICLE_IN_USE", "Vehicle has historical references") from None
    raise exc


def _state(item: Vehicle, previous_status: str | None = None) -> None:
    if previous_status == "DISPOSED" and item.status != "DISPOSED":
        raise APIError(409, "INVALID_STATE_TRANSITION", "Disposed vehicle cannot be reactivated")
    if item.status == "DISPOSED" and item.disposed_at is None:
        raise APIError(422, "VALIDATION_FAILED", "Disposed vehicle requires disposed_at")


def _plate_history(session: Session, item: Vehicle, now: datetime) -> None:
    rows = session.execute(
        select(VehiclePlateHistory).where(
            VehiclePlateHistory.vehicle_id == item.id, VehiclePlateHistory.valid_to.is_(None)
        )
    ).scalars()
    for row in rows:
        row.valid_to = now
    session.add(
        VehiclePlateHistory(
            vehicle_id=item.id,
            plate_no=item.plate_no,
            plate_no_norm=item.plate_no_norm,
            province=item.province,
            valid_from=now,
            source="MTN",
        )
    )


def _change(
    session: Session, item: Vehicle, actor: AppUser, before: dict[str, object] | None
) -> None:
    after = snapshot(item)
    session.add(
        VehicleChange(
            vehicle_id=item.id,
            change_type="CREATE" if before is None else "UPDATE",
            before_json=before,
            after_json=after,
            changed_by=actor.id,
            # Wall time agrees with last_imported_at even when this transaction
            # began before waiting for the shared registry lock.
            changed_at=datetime.now(UTC),
        )
    )
    write_audit(
        session,
        entity="vehicle",
        entity_id=item.id,
        action="CREATE" if before is None else "UPDATE",
        actor_id=actor.id,
        before=before,
        after=after,
    )


def create_vehicle(session: Session, payload: VehicleCreate, actor: AppUser) -> Vehicle:
    lock_vehicle_writes(session)
    try:
        with session.begin_nested():
            item = Vehicle(
                id=uuid.uuid4(),
                **payload.model_dump(),
                plate_no_norm=normalize_plate(payload.plate_no),
                origin="MTN",
                core_sync_status="PENDING_CREATE",
                created_by=actor.id,
                updated_by=actor.id,
            )
            _state(item)
            _unique(session, item)
            session.add(item)
            session.flush()
            _plate_history(session, item, datetime.now(UTC))
            _change(session, item, actor, None)
            session.flush()
    except IntegrityError as exc:
        _integrity(exc)
    session.commit()
    return item


def update_vehicle(
    session: Session, identity: uuid.UUID, payload: VehiclePatch, actor: AppUser
) -> Vehicle:
    lock_vehicle_writes(session)
    try:
        with session.begin_nested():
            item = get_vehicle(session, identity, lock=True)
            if item.version != payload.version:
                raise APIError(409, "VERSION_CONFLICT", "Vehicle has changed")
            before = snapshot(item)
            for field, value in payload.model_dump(exclude_unset=True, exclude={"version"}).items():
                setattr(item, field, value)
            _state(item, str(before["status"]))
            item.plate_no_norm = normalize_plate(item.plate_no)
            _unique(session, item)
            if any(
                before[field] != getattr(item, field)
                for field in CORE_COLUMNS
                if field not in {"disposed_at", "core_asset_id"}
            ) or (
                before["disposed_at"]
                != (item.disposed_at.isoformat() if item.disposed_at else None)
            ):
                item.version += 1
                item.updated_by = actor.id
                item.core_sync_status = (
                    "PENDING_CREATE" if item.core_asset_id is None else "PENDING_UPDATE"
                )
                if (before["plate_no"], before["province"]) != (item.plate_no, item.province):
                    # Existing imported rows may predate a history entry.
                    if not session.execute(
                        select(VehiclePlateHistory.id).where(
                            VehiclePlateHistory.vehicle_id == item.id
                        )
                    ).first():
                        session.add(
                            VehiclePlateHistory(
                                vehicle_id=item.id,
                                plate_no=str(before["plate_no"]),
                                plate_no_norm=str(before["plate_no_norm"]),
                                province=str(before["province"]),
                                valid_from=item.created_at,
                                valid_to=datetime.now(UTC),
                                source=item.origin,
                            )
                        )
                        session.flush()
                    _plate_history(session, item, datetime.now(UTC))
                session.flush()
                _change(session, item, actor, before)
                session.flush()
    except IntegrityError as exc:
        _integrity(exc)
    session.commit()
    return item


def confirm_core_sync(
    session: Session, identity: uuid.UUID, payload: ConfirmCoreSync, actor: AppUser
) -> Vehicle:
    lock_vehicle_writes(session)
    try:
        with session.begin_nested():
            item = get_vehicle(session, identity, lock=True)
            if payload.version is not None and item.version != payload.version:
                raise APIError(409, "VERSION_CONFLICT", "Vehicle has changed")
            if item.core_sync_status != "EXPORTED":
                raise APIError(
                    409, "INVALID_STATE_TRANSITION", "Export current changes before confirming"
                )
            if item.core_asset_id is not None and item.core_asset_id != payload.core_asset_id:
                raise APIError(409, "CORE_ID_MISMATCH", "Existing CORE identity cannot be replaced")
            before = snapshot(item)
            item.core_asset_id = payload.core_asset_id
            _unique(session, item)
            item.core_sync_status = "SYNCED"
            item.version += 1
            item.updated_by = actor.id
            session.flush()
            write_audit(
                session,
                entity="vehicle",
                entity_id=item.id,
                action="SYNC_CONFIRM",
                actor_id=actor.id,
                before=before,
                after=snapshot(item),
            )
            session.flush()
    except IntegrityError as exc:
        _integrity(exc)
    session.commit()
    return item


def export_core(
    session: Session, actor: AppUser, sink: CoreChangeSink | None = None
) -> tuple[bytes, str, uuid.UUID]:
    lock_vehicle_writes(session)
    with session.begin_nested():
        items = (
            session.execute(
                select(Vehicle)
                .where(Vehicle.core_sync_status.in_(["PENDING_CREATE", "PENDING_UPDATE"]))
                .order_by(Vehicle.id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            .scalars()
            .all()
        )
        now = datetime.now(UTC)
        batch = CoreExportBatch(
            id=uuid.uuid4(),
            file_name=f"mtn-core-{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex}.csv",
            row_count=len(items),
            exported_by=actor.id,
        )
        session.add(batch)
        session.flush()
        rows: list[dict[str, object]] = []
        for item in items:
            before = snapshot(item)
            changes = (
                session.execute(
                    select(VehicleChange)
                    .where(
                        VehicleChange.vehicle_id == item.id, VehicleChange.export_batch_id.is_(None)
                    )
                    # Accepted CORE values supersede earlier local edits; retain
                    # their history without exporting those edits again later.
                    .where(
                        VehicleChange.changed_at > item.last_imported_at
                        if item.last_imported_at is not None
                        else True
                    )
                    .order_by(VehicleChange.changed_at, VehicleChange.id)
                )
                .scalars()
                .all()
            )
            changed = set()
            for change in changes:
                changed.update(
                    field
                    for field in CORE_COLUMNS
                    if change.before_json is None
                    or change.before_json.get(field) != change.after_json.get(field)
                )
                change.export_batch_id = batch.id
            # Pending rows from import resolution can have no local change rows.
            if not changes:
                changed.update(CORE_COLUMNS)
            rows.append(
                {
                    **{field: before[field] for field in CORE_COLUMNS},
                    "mnt_vehicle_id": str(item.id),
                    "change_type": "CREATE" if item.core_asset_id is None else "UPDATE",
                    "changed_fields": ",".join(field for field in CORE_COLUMNS if field in changed),
                }
            )
            item.core_sync_status = "EXPORTED"
            item.version += 1
            item.updated_by = actor.id
            session.flush()
            write_audit(
                session,
                entity="vehicle",
                entity_id=item.id,
                action="CORE_EXPORT",
                actor_id=actor.id,
                before=before,
                after=snapshot(item),
            )
        # Serialization is inside the transaction: sink failure must not mark
        # records exported. The sink has no network/file-system authority.
        data = (sink if sink is not None else CSVCoreChangeSink()).export(rows)
        write_audit(
            session,
            entity="core_export_batch",
            entity_id=batch.id,
            action="CREATE",
            actor_id=actor.id,
            after={"file_name": batch.file_name, "row_count": len(items)},
        )
        session.flush()
    session.commit()
    return data, batch.file_name, batch.id


def delete_vehicle(session: Session, identity: uuid.UUID, version: int, actor: AppUser) -> None:
    lock_vehicle_writes(session)
    try:
        with session.begin_nested():
            item = get_vehicle(session, identity, lock=True)
            if item.version != version:
                raise APIError(409, "VERSION_CONFLICT", "Vehicle has changed")
            if (
                item.core_sync_status != "PENDING_CREATE"
                or session.execute(
                    select(MaintenanceRecord.id).where(MaintenanceRecord.vehicle_id == identity)
                ).first()
            ):
                raise APIError(409, "VEHICLE_DELETE_NOT_ALLOWED", "Change vehicle status instead")
            before = snapshot(item)
            session.execute(delete(VehicleChange).where(VehicleChange.vehicle_id == identity))
            session.execute(
                delete(VehiclePlateHistory).where(VehiclePlateHistory.vehicle_id == identity)
            )
            session.delete(item)
            write_audit(
                session,
                entity="vehicle",
                entity_id=identity,
                action="DELETE",
                actor_id=actor.id,
                before=before,
            )
            session.flush()
    except IntegrityError as exc:
        _integrity(exc)
    session.commit()


def list_vehicles(
    session: Session,
    *,
    q: str,
    status: str | None,
    sync_status: str | None,
    page: int,
    page_size: int,
) -> tuple[list[Vehicle], int]:
    statement = select(Vehicle)
    normalized = normalize_plate(q.strip())
    if normalized:
        # Escaping prevents '%'/'_' in a plate query becoming wildcard operators.
        pattern = (
            "%" + normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        historical = (
            select(VehiclePlateHistory.id)
            .where(
                VehiclePlateHistory.vehicle_id == Vehicle.id,
                VehiclePlateHistory.plate_no_norm.ilike(pattern, escape="\\"),
            )
            .exists()
        )
        statement = statement.where(
            or_(Vehicle.plate_no_norm.ilike(pattern, escape="\\"), historical)
        )
    if status is not None:
        statement = statement.where(Vehicle.status == status)
    if sync_status is not None:
        statement = statement.where(Vehicle.core_sync_status == sync_status)
    total = session.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
    items = (
        session.execute(
            statement.order_by(Vehicle.plate_no_norm, Vehicle.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    return list(items), total
