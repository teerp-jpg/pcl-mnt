"""Synchronous CSV-only import with bounded reads and manager/admin decisions."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, UploadFile
from sqlalchemy.orm import Session

from app.auth.dependencies import require_role
from app.db import get_session
from app.integrations.core_source import MAX_CSV_BYTES, CSVCoreAssetSource
from app.models import AppUser
from app.schemas.vehicle_imports import ImportResolutions, ImportView
from app.services.vehicle_import import (
    apply_import,
    batch_view,
    discard_import,
    get_batch,
    resolve_import,
    revalidate_import,
    validate_import,
)

router = APIRouter(prefix="/vehicle-imports", tags=["vehicle-imports"])
Operator = Annotated[AppUser, Depends(require_role("manager", "admin"))]
DB = Annotated[Session, Depends(get_session)]


@router.post("", response_model=ImportView, status_code=201)
def upload_import(
    request: Request, actor: Operator, session: DB, file: Annotated[UploadFile, File()]
) -> ImportView:
    try:
        data = file.file.read(MAX_CSV_BYTES + 1)
        source = CSVCoreAssetSource(data, request.app.state.settings.core_csv_column_map)
        # Display name only; never use a client filename as a filesystem path.
        filename = (file.filename or "import.csv").replace("\\", "/").split("/")[-1]
        filename = (
            "".join(character for character in filename if character.isprintable())[:200]
            or "import.csv"
        )
        batch = validate_import(session, actor, source, data, filename)
        return batch_view(session, batch)
    finally:
        file.file.close()


@router.get("/{batch_id}", response_model=ImportView)
def import_detail(batch_id: uuid.UUID, _actor: Operator, session: DB) -> ImportView:
    return batch_view(session, get_batch(session, batch_id))


@router.post("/{batch_id}/resolutions", response_model=ImportView)
def import_resolve(
    batch_id: uuid.UUID, payload: ImportResolutions, actor: Operator, session: DB
) -> ImportView:
    return batch_view(session, resolve_import(session, batch_id, payload, actor))


@router.post("/{batch_id}/apply", response_model=ImportView)
def import_apply(batch_id: uuid.UUID, actor: Operator, session: DB) -> ImportView:
    return batch_view(session, apply_import(session, batch_id, actor))


@router.post("/{batch_id}/revalidate", response_model=ImportView)
def import_revalidate(batch_id: uuid.UUID, actor: Operator, session: DB) -> ImportView:
    return batch_view(session, revalidate_import(session, batch_id, actor))


@router.post("/{batch_id}/discard", response_model=ImportView)
def import_discard(batch_id: uuid.UUID, actor: Operator, session: DB) -> ImportView:
    return batch_view(session, discard_import(session, batch_id, actor))
