"""Role-protected registry, history and synchronous CORE file export."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user, require_role
from app.db import get_session
from app.errors import APIError
from app.models import AppUser, MaintenanceRecord, PMPlan, VehiclePlateHistory
from app.schemas.vehicles import (
    ConfirmCoreSync,
    PlanSummary,
    PlateHistoryView,
    RecordSummary,
    SyncStatus,
    VehicleCreate,
    VehicleDetail,
    VehiclePage,
    VehiclePatch,
    VehicleStatus,
    VehicleSummary,
    VehicleView,
)
from app.services.pm_due import due_plans, vehicle_pm_status
from app.services.vehicles import (
    confirm_core_sync,
    create_vehicle,
    delete_vehicle,
    export_core,
    get_vehicle,
    list_vehicles,
    update_vehicle,
)

router = APIRouter(prefix="/vehicles", tags=["vehicles"])
Reader = Annotated[AppUser, Depends(current_user)]
Writer = Annotated[AppUser, Depends(require_role("manager", "admin"))]
DB = Annotated[Session, Depends(get_session)]


@router.get("", response_model=VehiclePage)
def vehicle_list(
    _actor: Reader,
    session: DB,
    q: Annotated[str, Query(max_length=200)] = "",
    status: VehicleStatus | None = None,
    sync_status: SyncStatus | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> VehiclePage:
    items, total = list_vehicles(
        session, q=q, status=status, sync_status=sync_status, page=page, page_size=page_size
    )
    return VehiclePage(
        items=[VehicleView.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


# Static route must precede the UUID route. This GET mutates export bookkeeping
# per the approved contract: require writer role, allowed Origin, and no caching.
@router.get("/core-export", response_class=Response)
def core_export(request: Request, actor: Writer, session: DB) -> Response:
    origin = request.headers.get("origin")
    if (
        origin is not None and origin not in request.app.state.settings.cors_origins
    ) or request.headers.get("sec-fetch-site") == "cross-site":
        raise APIError(403, "FORBIDDEN", "Origin is not allowed")
    data, filename, batch_id = export_core(session, actor)
    return Response(
        content=data,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Export-Batch-ID": str(batch_id),
        },
    )


@router.post("", response_model=VehicleView, status_code=201)
def vehicle_create(payload: VehicleCreate, actor: Writer, session: DB) -> VehicleView:
    return VehicleView.model_validate(create_vehicle(session, payload, actor))


@router.get("/{vehicle_id}", response_model=VehicleDetail)
def vehicle_detail(vehicle_id: uuid.UUID, _actor: Reader, session: DB) -> VehicleDetail:
    item = get_vehicle(session, vehicle_id)
    history = session.execute(
        select(VehiclePlateHistory)
        .where(VehiclePlateHistory.vehicle_id == vehicle_id)
        .order_by(VehiclePlateHistory.valid_from, VehiclePlateHistory.id)
    ).scalars()
    return VehicleDetail(
        **VehicleView.model_validate(item).model_dump(),
        plate_history=[PlateHistoryView.model_validate(row) for row in history],
    )


@router.patch("/{vehicle_id}", response_model=VehicleView)
def vehicle_update(
    vehicle_id: uuid.UUID, payload: VehiclePatch, actor: Writer, session: DB
) -> VehicleView:
    return VehicleView.model_validate(update_vehicle(session, vehicle_id, payload, actor))


@router.delete("/{vehicle_id}", status_code=204)
def vehicle_delete(
    vehicle_id: uuid.UUID, version: Annotated[int, Query(ge=1)], actor: Writer, session: DB
) -> Response:
    delete_vehicle(session, vehicle_id, version, actor)
    return Response(status_code=204)


@router.post("/{vehicle_id}/confirm-core-sync", response_model=VehicleView)
def vehicle_sync(
    vehicle_id: uuid.UUID, payload: ConfirmCoreSync, actor: Writer, session: DB
) -> VehicleView:
    return VehicleView.model_validate(confirm_core_sync(session, vehicle_id, payload, actor))


@router.get("/{vehicle_id}/summary", response_model=VehicleSummary)
def vehicle_summary(vehicle_id: uuid.UUID, _actor: Reader, session: DB) -> VehicleSummary:
    get_vehicle(session, vehicle_id)
    count, total = session.execute(
        select(func.count(), func.sum(MaintenanceRecord.total_amount)).where(
            MaintenanceRecord.vehicle_id == vehicle_id, MaintenanceRecord.status == "COMPLETED"
        )
    ).one()
    latest = session.execute(
        select(MaintenanceRecord)
        .where(MaintenanceRecord.vehicle_id == vehicle_id)
        .order_by(
            MaintenanceRecord.service_start_date.desc().nulls_last(),
            MaintenanceRecord.created_at.desc(),
            MaintenanceRecord.id,
        )
        .limit(1)
    ).scalar_one_or_none()
    plans = session.execute(
        select(PMPlan)
        .where(PMPlan.vehicle_id == vehicle_id, PMPlan.is_active.is_(True))
        .order_by(PMPlan.name, PMPlan.id)
    ).scalars()
    return VehicleSummary(
        vehicle_id=vehicle_id,
        completed_count=count,
        total_cost=total if total is not None else Decimal("0.00"),
        latest_record=RecordSummary.model_validate(latest) if latest else None,
        pm_plans=[PlanSummary.model_validate(plan) for plan in plans],
        pm_status=vehicle_pm_status(due_plans(session, vehicle_id=vehicle_id)),
    )


@router.get("/{vehicle_id}/maintenance-records", response_model=list[RecordSummary])
def vehicle_records(
    vehicle_id: uuid.UUID,
    _actor: Reader,
    session: DB,
    maintenance_type: Annotated[Literal["PM", "CM"] | None, Query(alias="type")] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    status: Literal["DRAFT", "IN_PROGRESS", "COMPLETED", "CANCELLED"] | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[RecordSummary]:
    get_vehicle(session, vehicle_id)
    if date_from is not None and date_to is not None and date_from > date_to:
        raise APIError(422, "VALIDATION_FAILED", "Invalid date range")
    statement = select(MaintenanceRecord).where(MaintenanceRecord.vehicle_id == vehicle_id)
    if maintenance_type is not None:
        statement = statement.where(MaintenanceRecord.maintenance_type == maintenance_type)
    if status is not None:
        statement = statement.where(MaintenanceRecord.status == status)
    if date_from is not None:
        statement = statement.where(MaintenanceRecord.service_start_date >= date_from)
    if date_to is not None:
        statement = statement.where(MaintenanceRecord.service_start_date <= date_to)
    rows = session.execute(
        statement.order_by(
            MaintenanceRecord.service_start_date.desc().nulls_last(), MaintenanceRecord.id
        )
        .offset(offset)
        .limit(limit)
    ).scalars()
    return [RecordSummary.model_validate(row) for row in rows]
