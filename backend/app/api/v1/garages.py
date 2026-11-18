"""MTN-owned garages; DELETE deactivates without removing history."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user, require_role
from app.db import get_session
from app.models import AppUser, Garage
from app.schemas.master_data import GarageCreate, GaragePatch, GarageView
from app.services.master_data import create_garage, get_master, update_garage

router = APIRouter(prefix="/garages", tags=["garages"])
Reader = Annotated[AppUser, Depends(current_user)]
Writer = Annotated[AppUser, Depends(require_role("manager", "admin"))]
DB = Annotated[Session, Depends(get_session)]


@router.get("", response_model=list[GarageView])
def list_garages(
    _actor: Reader,
    session: DB,
    is_active: bool | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[GarageView]:
    statement = select(Garage).order_by(Garage.name_norm, Garage.id)
    if is_active is not None:
        statement = statement.where(Garage.is_active == is_active)
    items = session.execute(statement.offset(offset).limit(limit)).scalars()
    return [GarageView.model_validate(item) for item in items]


@router.get("/{garage_id}", response_model=GarageView)
def garage_detail(garage_id: uuid.UUID, _actor: Reader, session: DB) -> GarageView:
    return GarageView.model_validate(get_master(session, Garage, garage_id))


@router.post("", response_model=GarageView, status_code=201)
def garage_create(payload: GarageCreate, actor: Writer, session: DB) -> GarageView:
    return GarageView.model_validate(create_garage(session, payload, actor))


@router.patch("/{garage_id}", response_model=GarageView)
def garage_update(
    garage_id: uuid.UUID, payload: GaragePatch, actor: Writer, session: DB
) -> GarageView:
    return GarageView.model_validate(update_garage(session, garage_id, payload, actor))


@router.delete("/{garage_id}", status_code=204)
def garage_deactivate(
    garage_id: uuid.UUID, version: Annotated[int, Query(ge=1)], actor: Writer, session: DB
) -> Response:
    update_garage(session, garage_id, GaragePatch(version=version), actor, deactivate=True)
    return Response(status_code=204)
