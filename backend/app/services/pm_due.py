"""PM CRUD and due observations share T7's registry lock and audit transaction."""

import uuid
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.pm_due import calculate_due
from app.errors import APIError
from app.models import AppUser, MaintenanceCategory, OdometerReading, PMPlan, Vehicle
from app.schemas.pm_plans import DueView, PlanCreate, PlanPatch, PlanView
from app.services.audit import write_audit
from app.services.master_data import read_pm_settings
from app.services.vehicles import get_vehicle, lock_vehicle_writes

BANGKOK = ZoneInfo("Asia/Bangkok")


def get_plan(session: Session, identity: uuid.UUID, *, lock: bool = False) -> PMPlan:
    statement = select(PMPlan).where(PMPlan.id == identity)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    item = session.execute(statement).scalar_one_or_none()
    if item is None:
        raise APIError(404, "PM_PLAN_NOT_FOUND", "PM plan not found")
    return item


def _validate(session: Session, item: PMPlan) -> None:
    if item.interval_km is None and item.interval_days is None:
        raise APIError(422, "VALIDATION_FAILED", "A distance or day interval is required")
    today = datetime.now(BANGKOK).date()
    if item.last_done_at is not None and item.last_done_at > today:
        raise APIError(422, "VALIDATION_FAILED", "PM baseline cannot be in the future")
    if item.category_id is not None and session.get(MaintenanceCategory, item.category_id) is None:
        raise APIError(422, "VALIDATION_FAILED", "Category not found")


def save_plan(
    session: Session,
    payload: PlanCreate | PlanPatch,
    actor: AppUser,
    *,
    identity: uuid.UUID | None = None,
    deactivate: bool = False,
) -> PMPlan:
    lock_vehicle_writes(session)
    with session.begin_nested():
        if identity is None:
            assert isinstance(payload, PlanCreate)
            get_vehicle(session, payload.vehicle_id, lock=True)
            item = PMPlan(**payload.model_dump(), created_by=actor.id, updated_by=actor.id)
            before = None
            action = "CREATE"
            session.add(item)
        else:
            assert isinstance(payload, PlanPatch)
            item = get_plan(session, identity, lock=True)
            if item.version != payload.version:
                raise APIError(409, "VERSION_CONFLICT", "PM plan has changed")
            before = PlanView.model_validate(item).model_dump(mode="json")
            for field, value in payload.model_dump(exclude_unset=True, exclude={"version"}).items():
                setattr(item, field, value)
            if deactivate:
                item.is_active = False
            item.version += 1
            item.updated_by = actor.id
            action = "DEACTIVATE" if deactivate else "UPDATE"
        # Validate nullable/foreign-key changes before a lookup can autoflush them.
        with session.no_autoflush:
            _validate(session, item)
        session.flush()
        write_audit(
            session,
            entity="pm_plan",
            entity_id=item.id,
            action=action,
            actor_id=actor.id,
            before=before,
            after=PlanView.model_validate(item).model_dump(mode="json"),
        )
        session.flush()
    session.commit()
    return item


def due_plans(session: Session, *, vehicle_id: uuid.UUID | None = None) -> list[DueView]:
    now = datetime.now(BANGKOK)
    settings = read_pm_settings(session)
    # Rank by observation time, not maximum mileage (regressions can be justified).
    readings = select(
        OdometerReading.vehicle_id,
        OdometerReading.reading_km,
        OdometerReading.recorded_at,
        func.row_number()
        .over(
            partition_by=OdometerReading.vehicle_id,
            order_by=(
                OdometerReading.recorded_at.desc(),
                OdometerReading.created_at.desc(),
                OdometerReading.id.desc(),
            ),
        )
        .label("position"),
    ).where(OdometerReading.recorded_at <= now)
    if vehicle_id is not None:
        readings = readings.where(OdometerReading.vehicle_id == vehicle_id)
    latest = readings.subquery()
    statement = (
        select(PMPlan, Vehicle, latest.c.reading_km, latest.c.recorded_at)
        .join(Vehicle, Vehicle.id == PMPlan.vehicle_id)
        .outerjoin(latest, (latest.c.vehicle_id == PMPlan.vehicle_id) & (latest.c.position == 1))
        .where(PMPlan.is_active.is_(True))
        .order_by(PMPlan.name, PMPlan.id)
    )
    if vehicle_id is not None:
        statement = statement.where(PMPlan.vehicle_id == vehicle_id)
    result = []
    for plan, vehicle, current_km, reading_at in session.execute(statement):
        due = calculate_due(
            interval_km=plan.interval_km,
            interval_days=plan.interval_days,
            last_done_km=plan.last_done_km,
            last_done_at=plan.last_done_at,
            current_km=current_km,
            today=now.date(),
            soon_pct=Decimal(str(settings.pm_due_soon_pct)),
            soon_km=settings.pm_due_soon_km,
            soon_days=settings.pm_due_soon_days,
        )
        result.append(
            DueView(
                **PlanView.model_validate(plan).model_dump(),
                **asdict(due),
                plate_no=vehicle.plate_no,
                province=vehicle.province,
                current_km=current_km,
                reading_at=reading_at,
                data_as_of=now.date(),
            )
        )
    return result


def vehicle_pm_status(plans: list[DueView]) -> str | None:
    for status in ("OVERDUE", "DUE_SOON"):
        if any(plan.status == status for plan in plans):
            return status
    return "OK" if plans and all(plan.status == "OK" for plan in plans) else None
