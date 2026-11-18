"""Record, lines, odometer, plan baseline and audit share a transaction."""

import uuid
from datetime import datetime, time, timedelta
from typing import BinaryIO
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.domain.maintenance_state import (
    RecordStatusChanged,
    emit_status_changed,
    require_transition,
)
from app.domain.money import calculate_money
from app.errors import APIError
from app.models import (
    AppUser,
    Garage,
    MaintenanceAttachment,
    MaintenanceCategory,
    MaintenanceLine,
    MaintenanceRecord,
    OdometerReading,
    PMPlan,
    Vehicle,
)
from app.schemas.maintenance import (
    AttachmentView,
    LineInput,
    LineView,
    RecordChanges,
    RecordCreate,
    RecordView,
)
from app.services.audit import write_audit
from app.services.master_data import get_master, require_active_garage
from app.services.vehicles import lock_vehicle_writes
from app.storage import FileStorage, safe_filename

BANGKOK = ZoneInfo("Asia/Bangkok")


def get_record(session: Session, identity: uuid.UUID, *, lock: bool = False) -> MaintenanceRecord:
    query = select(MaintenanceRecord).where(MaintenanceRecord.id == identity)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    record = session.execute(query).scalar_one_or_none()
    if record is None:
        raise APIError(404, "NOT_FOUND", "Maintenance record not found")
    return record


def record_lines(session: Session, identity: uuid.UUID) -> list[MaintenanceLine]:
    return list(
        session.execute(
            select(MaintenanceLine)
            .where(MaintenanceLine.record_id == identity)
            .order_by(MaintenanceLine.line_no)
        ).scalars()
    )


def record_view(session: Session, record: MaintenanceRecord) -> RecordView:
    view = RecordView.model_validate(record)
    view.lines = [LineView.model_validate(line) for line in record_lines(session, record.id)]
    view.attachments = [
        AttachmentView.model_validate(item)
        for item in session.execute(
            select(MaintenanceAttachment)
            .where(MaintenanceAttachment.record_id == record.id)
            .order_by(MaintenanceAttachment.uploaded_at, MaintenanceAttachment.id)
        ).scalars()
    ]
    if record.garage_id is not None:
        view.garage_name = get_master(session, Garage, record.garage_id).name
    vehicle = session.get(Vehicle, record.vehicle_id)
    if vehicle is not None and vehicle.status == "INACTIVE":
        view.warnings.append("VEHICLE_INACTIVE")
    if record.invoice_no and record.garage_id and record.status != "CANCELLED":
        duplicate = session.execute(
            select(MaintenanceRecord.id)
            .where(
                MaintenanceRecord.id != record.id,
                MaintenanceRecord.vehicle_id == record.vehicle_id,
                MaintenanceRecord.garage_id == record.garage_id,
                MaintenanceRecord.invoice_no == record.invoice_no,
                MaintenanceRecord.status != "CANCELLED",
            )
            .limit(1)
        ).first()
        if duplicate:
            view.warnings.append("POSSIBLE_DUPLICATE_INVOICE")
    return view


def snapshot(session: Session, record: MaintenanceRecord) -> dict[str, object]:
    return record_view(session, record).model_dump(mode="json", exclude={"warnings"})


def check_version(record: MaintenanceRecord, version: int) -> None:
    if record.version != version:
        raise APIError(409, "VERSION_CONFLICT", "Maintenance record has changed")


def _invalid(message: str) -> None:
    raise APIError(422, "VALIDATION_FAILED", message)


def _validate_record(
    session: Session,
    record: MaintenanceRecord,
    vehicle: Vehicle,
    *,
    new_garage: bool,
) -> None:
    start, end = record.service_start_date, record.service_end_date
    if start and start > datetime.now(BANGKOK).date():
        _invalid("Service start date cannot be in the future")
    if end and (start is None or end < start):
        _invalid("Service end date must follow start date")
    if vehicle.status == "DISPOSED" and start and start > vehicle.disposed_at:
        raise APIError(422, "ASSET_DISPOSED", "Vehicle was disposed before service date")
    if record.maintenance_type == "CM" and not record.failure_description:
        _invalid("CM requires failure description")
    if record.garage_id:
        if new_garage:
            require_active_garage(session, record.garage_id)
        else:
            get_master(session, Garage, record.garage_id)
    if record.pm_plan_id:
        plan = session.execute(
            select(PMPlan).where(PMPlan.id == record.pm_plan_id).with_for_update()
        ).scalar_one_or_none()
        if plan is None or plan.vehicle_id != record.vehicle_id or record.maintenance_type != "PM":
            _invalid("PM plan must belong to the same vehicle and PM record")
    if record.status != "DRAFT" and not start:
        _invalid("Service start date is required")
    if record.status == "COMPLETED":
        if not end or not record.garage_id or not record_lines(session, record.id):
            _invalid("Completion requires end date, garage and lines")
        if record.odometer_km is None and not record.odometer_missing_reason:
            _invalid("Missing odometer requires a reason")
    if record.odometer_km is not None and start:
        # Compare the latest prior dated reading, excluding this record's own
        # projection. Midnight + one day includes other readings on that date.
        cutoff = datetime.combine(start, time.min, BANGKOK) + timedelta(days=1)
        prior = session.execute(
            select(OdometerReading)
            .where(
                OdometerReading.vehicle_id == record.vehicle_id,
                OdometerReading.recorded_at < cutoff,
                (OdometerReading.source != "MAINTENANCE")
                | (OdometerReading.source_ref != str(record.id))
                | OdometerReading.source_ref.is_(None),
            )
            .order_by(OdometerReading.recorded_at.desc(), OdometerReading.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if (
            prior
            and record.odometer_km < prior.reading_km
            and not record.odometer_regression_reason
        ):
            raise APIError(
                422, "ODOMETER_REGRESSION_REASON_REQUIRED", "Odometer regression requires reason"
            )


def _replace_lines(session: Session, record: MaintenanceRecord, lines: list[LineInput]) -> None:
    amounts, subtotal, total = calculate_money(lines, record.vat_amount)
    for line in lines:
        category = get_master(session, MaintenanceCategory, line.category_id)
        if not category.is_active:
            _invalid("Line category is inactive")
    session.execute(delete(MaintenanceLine).where(MaintenanceLine.record_id == record.id))
    for number, (line, amount) in enumerate(zip(lines, amounts, strict=True), start=1):
        session.add(
            MaintenanceLine(**line.model_dump(), record_id=record.id, line_no=number, amount=amount)
        )
    record.subtotal, record.total_amount = subtotal, total
    session.flush()


def _recalculate(session: Session, record: MaintenanceRecord) -> None:
    # Run even on header-only PATCH/correction; persisted operands must produce
    # the same amounts after round-trip through PostgreSQL NUMERIC.
    lines = record_lines(session, record.id)
    inputs = [
        LineInput.model_validate(
            {field: getattr(line, field) for field in LineInput.model_fields if field != "amount"}
        )
        for line in lines
    ]
    amounts, record.subtotal, record.total_amount = calculate_money(inputs, record.vat_amount)
    for line, amount in zip(lines, amounts, strict=True):
        line.amount = amount


def _projections(session: Session, record: MaintenanceRecord, actor: AppUser) -> None:
    reading = session.execute(
        select(OdometerReading).where(
            OdometerReading.vehicle_id == record.vehicle_id,
            OdometerReading.source == "MAINTENANCE",
            OdometerReading.source_ref == str(record.id),
        )
    ).scalar_one_or_none()
    if record.status == "COMPLETED" and record.odometer_km is not None:
        assert record.service_start_date is not None
        if reading is None:
            reading = OdometerReading(
                vehicle_id=record.vehicle_id, source="MAINTENANCE", source_ref=str(record.id)
            )
            session.add(reading)
        reading.reading_km = record.odometer_km
        reading.recorded_at = datetime.combine(record.service_start_date, time.min, BANGKOK)
    elif reading is not None:
        session.delete(reading)
    session.flush()
    # Recompute from completed records so correcting an older job cannot move
    # the plan backwards or leave a detached plan pointing to obsolete data.
    plans = session.execute(
        select(PMPlan).where(PMPlan.vehicle_id == record.vehicle_id).with_for_update()
    ).scalars()
    for plan in plans:
        latest = session.execute(
            select(MaintenanceRecord)
            .where(
                MaintenanceRecord.pm_plan_id == plan.id,
                MaintenanceRecord.status == "COMPLETED",
            )
            .order_by(MaintenanceRecord.service_end_date.desc(), MaintenanceRecord.record_no.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest is None:
            # Leave manually supplied baseline alone unless this record has
            # previously completed against this plan (handled by caller).
            continue
        baseline = (latest.service_end_date, latest.odometer_km)
        if baseline != (plan.last_done_at, plan.last_done_km):
            before = {"last_done_at": plan.last_done_at, "last_done_km": plan.last_done_km}
            plan.last_done_at, plan.last_done_km = baseline
            plan.version += 1
            plan.updated_by = actor.id
            write_audit(
                session,
                entity="pm_plan",
                entity_id=plan.id,
                action="MAINTENANCE_BASELINE",
                actor_id=actor.id,
                before=before,
                after={"last_done_at": plan.last_done_at, "last_done_km": plan.last_done_km},
            )


def _vehicle(session: Session, identity: uuid.UUID) -> Vehicle:
    vehicle = session.execute(
        select(Vehicle)
        .where(Vehicle.id == identity)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if vehicle is None:
        raise APIError(404, "VEHICLE_NOT_FOUND", "Vehicle not found")
    return vehicle


def create_record(session: Session, payload: RecordCreate, actor: AppUser) -> RecordView:
    lock_vehicle_writes(session)
    with session.begin_nested():
        vehicle = _vehicle(session, payload.vehicle_id)
        _, subtotal, total = calculate_money(payload.lines, payload.vat_amount)
        record = MaintenanceRecord(
            **payload.model_dump(exclude={"lines"}),
            status="DRAFT",
            subtotal=subtotal,
            total_amount=total,
            plate_no_snapshot=vehicle.plate_no,
            province_snapshot=vehicle.province,
            created_by=actor.id,
            updated_by=actor.id,
        )
        session.add(record)
        # Prevent constraints from seeing an invalid unvalidated header.
        with session.no_autoflush:
            _validate_record(session, record, vehicle, new_garage=True)
        session.flush()
        _replace_lines(session, record, payload.lines)
        write_audit(
            session,
            entity="maintenance_record",
            entity_id=record.id,
            action="CREATE",
            actor_id=actor.id,
            after=snapshot(session, record),
        )
        session.flush()
        result = record_view(session, record)
    session.commit()
    return result


def change_record(
    session: Session,
    identity: uuid.UUID,
    actor: AppUser,
    version: int,
    *,
    changes: RecordChanges | None = None,
    target: str | None = None,
    correction: bool = False,
    reason: str | None = None,
) -> RecordView:
    lock_vehicle_writes(session)
    with session.begin_nested():
        record = get_record(session, identity, lock=True)
        vehicle = _vehicle(session, record.vehicle_id)
        check_version(record, version)
        before = snapshot(session, record)
        old_status, old_garage, old_plan = record.status, record.garage_id, record.pm_plan_id
        if correction:
            if record.status != "COMPLETED":
                raise APIError(
                    409, "INVALID_STATE_TRANSITION", "Only completed jobs allow correction"
                )
            if not reason or not reason.strip():
                raise APIError(422, "CORRECTION_REASON_REQUIRED", "Correction requires reason")
        elif target:
            require_transition(record.status, target)
            record.status = target
            if target == "CANCELLED":
                if not reason or not reason.strip():
                    _invalid("Cancellation requires reason")
                record.cancel_reason = reason
        elif record.status not in {"DRAFT", "IN_PROGRESS"}:
            raise APIError(409, "INVALID_STATE_TRANSITION", "Record is not editable")
        if changes is not None:
            for name, value in changes.model_dump(
                exclude_unset=True, exclude={"lines", "version"}
            ).items():
                setattr(record, name, value)
        with session.no_autoflush:
            # Cancellation preserves an incomplete draft without demanding the
            # missing start date/CM fields; it never creates financial history.
            if target != "CANCELLED":
                _validate_record(
                    session, record, vehicle, new_garage=old_garage != record.garage_id
                )
        with session.no_autoflush:
            if changes is not None and "lines" in changes.model_fields_set:
                assert changes.lines is not None
                _replace_lines(session, record, changes.lines)
            _recalculate(session, record)
        # Validate completion after lines have been changed, within savepoint.
        if target != "CANCELLED":
            _validate_record(session, record, vehicle, new_garage=False)
        record.version += 1
        record.updated_by = actor.id
        session.flush()
        if record.status == "COMPLETED":
            _projections(session, record, actor)
        if correction and old_plan and old_plan != record.pm_plan_id:
            old = session.get(PMPlan, old_plan)
            assert old is not None
            remaining = session.execute(
                select(MaintenanceRecord.id)
                .where(
                    MaintenanceRecord.pm_plan_id == old_plan,
                    MaintenanceRecord.status == "COMPLETED",
                )
                .limit(1)
            ).first()
            if not remaining:
                previous = {"last_done_at": old.last_done_at, "last_done_km": old.last_done_km}
                old.last_done_at, old.last_done_km = None, None
                old.version += 1
                old.updated_by = actor.id
                write_audit(
                    session,
                    entity="pm_plan",
                    entity_id=old.id,
                    action="MAINTENANCE_BASELINE",
                    actor_id=actor.id,
                    before=previous,
                    after={"last_done_at": None, "last_done_km": None},
                    reason=reason,
                )
        write_audit(
            session,
            entity="maintenance_record",
            entity_id=record.id,
            action="CORRECTION" if correction else "STATUS_CHANGE" if target else "UPDATE",
            actor_id=actor.id,
            before=before,
            after=snapshot(session, record),
            reason=reason,
        )
        if old_status != record.status:
            emit_status_changed(
                session, RecordStatusChanged(record.id, old_status, record.status, actor.id)
            )
        session.flush()
        result = record_view(session, record)
    session.commit()
    return result


def get_attachment(
    session: Session, record_id: uuid.UUID, identity: uuid.UUID
) -> MaintenanceAttachment:
    get_record(session, record_id)
    item = session.execute(
        select(MaintenanceAttachment).where(
            MaintenanceAttachment.id == identity, MaintenanceAttachment.record_id == record_id
        )
    ).scalar_one_or_none()
    if item is None:
        raise APIError(404, "NOT_FOUND", "Attachment not found")
    return item


def add_attachment(
    session: Session,
    identity: uuid.UUID,
    actor: AppUser,
    storage: FileStorage,
    source: BinaryIO,
    filename: str | None,
) -> AttachmentView:
    lock_vehicle_writes(session)
    with session.begin_nested():
        record = get_record(session, identity, lock=True)
        if record.status not in {"DRAFT", "IN_PROGRESS"}:
            raise APIError(409, "INVALID_STATE_TRANSITION", "Attachments are not editable")
        stored = storage.save(source)
        item = MaintenanceAttachment(
            record_id=record.id,
            file_name=safe_filename(filename),
            content_type=stored.content_type,
            storage_key=stored.key,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            uploaded_by=actor.id,
        )
        session.add(item)
        record.version += 1
        record.updated_by = actor.id
        session.flush()
        result = AttachmentView.model_validate(item)
        write_audit(
            session,
            entity="maintenance_attachment",
            entity_id=item.id,
            action="CREATE",
            actor_id=actor.id,
            after={"record_id": str(record.id), **result.model_dump(mode="json")},
        )
        session.flush()
    session.commit()
    return result


def remove_attachment(
    session: Session, identity: uuid.UUID, attachment_id: uuid.UUID, actor: AppUser
) -> None:
    lock_vehicle_writes(session)
    with session.begin_nested():
        record = get_record(session, identity, lock=True)
        if record.status not in {"DRAFT", "IN_PROGRESS"}:
            raise APIError(409, "INVALID_STATE_TRANSITION", "Attachments are not editable")
        item = get_attachment(session, identity, attachment_id)
        write_audit(
            session,
            entity="maintenance_attachment",
            entity_id=item.id,
            action="DELETE",
            actor_id=actor.id,
            before={
                "record_id": str(record.id),
                **AttachmentView.model_validate(item).model_dump(mode="json"),
            },
        )
        session.delete(item)
        record.version += 1
        record.updated_by = actor.id
        session.flush()
    session.commit()
    # Preserve the immutable blob. Filesystem deletion cannot participate in a
    # DB rollback; unreferenced files require a separately authorized retention job.
