"""Maintenance categories with safe parent edits and soft deactivation."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user, require_role
from app.db import get_session
from app.models import AppUser, MaintenanceCategory
from app.schemas.master_data import CategoryCreate, CategoryPatch, CategoryView
from app.services.master_data import create_category, get_master, update_category

router = APIRouter(prefix="/maintenance-categories", tags=["maintenance-categories"])
Reader = Annotated[AppUser, Depends(current_user)]
Writer = Annotated[AppUser, Depends(require_role("manager", "admin"))]
DB = Annotated[Session, Depends(get_session)]


@router.get("", response_model=list[CategoryView])
def list_categories(
    _actor: Reader,
    session: DB,
    is_active: bool | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[CategoryView]:
    statement = select(MaintenanceCategory).order_by(
        MaintenanceCategory.code, MaintenanceCategory.id
    )
    if is_active is not None:
        statement = statement.where(MaintenanceCategory.is_active == is_active)
    items = session.execute(statement.offset(offset).limit(limit)).scalars()
    return [CategoryView.model_validate(item) for item in items]


@router.get("/{category_id}", response_model=CategoryView)
def category_detail(category_id: uuid.UUID, _actor: Reader, session: DB) -> CategoryView:
    return CategoryView.model_validate(get_master(session, MaintenanceCategory, category_id))


@router.post("", response_model=CategoryView, status_code=201)
def category_create(payload: CategoryCreate, actor: Writer, session: DB) -> CategoryView:
    return CategoryView.model_validate(create_category(session, payload, actor))


@router.patch("/{category_id}", response_model=CategoryView)
def category_update(
    category_id: uuid.UUID, payload: CategoryPatch, actor: Writer, session: DB
) -> CategoryView:
    return CategoryView.model_validate(update_category(session, category_id, payload, actor))


@router.delete("/{category_id}", status_code=204)
def category_deactivate(category_id: uuid.UUID, actor: Writer, session: DB) -> Response:
    update_category(session, category_id, CategoryPatch(), actor, deactivate=True)
    return Response(status_code=204)
