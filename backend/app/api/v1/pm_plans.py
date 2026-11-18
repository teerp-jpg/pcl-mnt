"""All roles can inspect PM; manager/admin manage plans without deleting history."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user, require_role
from app.db import get_session
from app.errors import APIError
from app.models import AppUser, PMPlan
from app.schemas.pm_plans import DuePage, PlanCreate, PlanPatch, PlanView
from app.services.pm_due import due_plans, get_plan, save_plan

router = APIRouter(tags=["pm-plans"])
Reader = Annotated[AppUser, Depends(current_user)]
Writer = Annotated[AppUser, Depends(require_role("manager", "admin"))]
DB = Annotated[Session, Depends(get_session)]


@router.get("/pm-plans", response_model=list[PlanView])
def list_plans(
    _actor: Reader,
    session: DB,
    vehicle_id: uuid.UUID | None = None,
    is_active: bool | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[PlanView]:
    statement = select(PMPlan).order_by(PMPlan.name, PMPlan.id)
    if vehicle_id is not None:
        statement = statement.where(PMPlan.vehicle_id == vehicle_id)
    if is_active is not None:
        statement = statement.where(PMPlan.is_active == is_active)
    return [
        PlanView.model_validate(row)
        for row in session.execute(statement.offset(offset).limit(limit)).scalars()
    ]


@router.post("/pm-plans", response_model=PlanView, status_code=201)
def create_plan(payload: PlanCreate, actor: Writer, session: DB) -> PlanView:
    return PlanView.model_validate(save_plan(session, payload, actor))


@router.get("/pm-plans/{plan_id}", response_model=PlanView)
def plan_detail(plan_id: uuid.UUID, _actor: Reader, session: DB) -> PlanView:
    return PlanView.model_validate(get_plan(session, plan_id))


@router.patch("/pm-plans/{plan_id}", response_model=PlanView)
def update_plan(plan_id: uuid.UUID, payload: PlanPatch, actor: Writer, session: DB) -> PlanView:
    return PlanView.model_validate(save_plan(session, payload, actor, identity=plan_id))


@router.delete("/pm-plans/{plan_id}", status_code=204)
def deactivate_plan(
    plan_id: uuid.UUID,
    version: Annotated[int, Query(ge=1)],
    actor: Writer,
    session: DB,
) -> Response:
    save_plan(session, PlanPatch(version=version), actor, identity=plan_id, deactivate=True)
    return Response(status_code=204)


@router.get("/pm-due", response_model=DuePage)
def pm_due(
    _actor: Reader,
    session: DB,
    vehicle_id: uuid.UUID | None = None,
    status: Annotated[str | None, Query(max_length=100)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> DuePage:
    statuses = {part.strip() for part in status.split(",")} if status is not None else None
    if statuses is not None and not statuses <= {"OK", "DUE_SOON", "OVERDUE"}:
        raise APIError(422, "VALIDATION_FAILED", "Invalid PM status filter")
    items = due_plans(session, vehicle_id=vehicle_id)
    if statuses is not None:
        items = [item for item in items if item.status in statuses]
    return DuePage(
        items=items[offset : offset + limit], total=len(items), offset=offset, limit=limit
    )
