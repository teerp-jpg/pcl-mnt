"""Administrator-only PM threshold settings, independent of environment secrets."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.dependencies import require_role
from app.db import get_session
from app.models import AppUser
from app.schemas.master_data import PMSettings
from app.services.master_data import read_pm_settings, update_pm_settings

router = APIRouter(prefix="/settings", tags=["settings"])
Admin = Annotated[AppUser, Depends(require_role("admin"))]
DB = Annotated[Session, Depends(get_session)]


@router.get("", response_model=PMSettings)
def get_settings(_actor: Admin, session: DB) -> PMSettings:
    return read_pm_settings(session)


@router.put("", response_model=PMSettings)
def put_settings(payload: PMSettings, actor: Admin, session: DB) -> PMSettings:
    return update_pm_settings(session, payload, actor)
