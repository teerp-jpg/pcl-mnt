"""Synchronous maintenance workflow and bounded local attachment streams."""

import uuid
from collections.abc import Iterator
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user, require_role
from app.db import get_session
from app.errors import APIError
from app.models import AppUser
from app.schemas.maintenance import (
    AttachmentView,
    CancelInput,
    CorrectionInput,
    RecordCreate,
    RecordPatch,
    RecordView,
    TransitionInput,
)
from app.services.maintenance import (
    add_attachment,
    change_record,
    create_record,
    get_attachment,
    get_record,
    record_view,
    remove_attachment,
)
from app.storage import CHUNK_BYTES, MIME_TYPES, FileStorage, LocalFileStorage

router = APIRouter(prefix="/maintenance-records", tags=["maintenance-records"])
DB = Annotated[Session, Depends(get_session)]
Reader = Annotated[AppUser, Depends(current_user)]
Writer = Annotated[AppUser, Depends(require_role("clerk", "manager", "admin"))]
Manager = Annotated[AppUser, Depends(require_role("manager", "admin"))]


def get_storage(request: Request) -> FileStorage:
    return LocalFileStorage(request.app.state.settings.upload_root)


Storage = Annotated[FileStorage, Depends(get_storage)]


@router.post("", response_model=RecordView, status_code=201)
def record_create(payload: RecordCreate, actor: Writer, session: DB) -> RecordView:
    return create_record(session, payload, actor)


@router.get("/{record_id}", response_model=RecordView)
def record_detail(record_id: uuid.UUID, _actor: Reader, session: DB) -> RecordView:
    return record_view(session, get_record(session, record_id))


@router.patch("/{record_id}", response_model=RecordView)
def record_update(
    record_id: uuid.UUID, payload: RecordPatch, actor: Writer, session: DB
) -> RecordView:
    return change_record(session, record_id, actor, payload.version, changes=payload)


@router.post("/{record_id}/start", response_model=RecordView)
def record_start(
    record_id: uuid.UUID, payload: TransitionInput, actor: Writer, session: DB
) -> RecordView:
    return change_record(session, record_id, actor, payload.version, target="IN_PROGRESS")


@router.post("/{record_id}/complete", response_model=RecordView)
def record_complete(
    record_id: uuid.UUID, payload: TransitionInput, actor: Writer, session: DB
) -> RecordView:
    return change_record(session, record_id, actor, payload.version, target="COMPLETED")


@router.post("/{record_id}/cancel", response_model=RecordView)
def record_cancel(
    record_id: uuid.UUID, payload: CancelInput, actor: Manager, session: DB
) -> RecordView:
    return change_record(
        session, record_id, actor, payload.version, target="CANCELLED", reason=payload.reason
    )


@router.post("/{record_id}/corrections", response_model=RecordView)
def record_correct(
    record_id: uuid.UUID, payload: CorrectionInput, actor: Manager, session: DB
) -> RecordView:
    return change_record(
        session,
        record_id,
        actor,
        payload.version,
        changes=payload.changes,
        correction=True,
        reason=payload.reason,
    )


@router.post("/{record_id}/attachments", response_model=AttachmentView, status_code=201)
def attachment_upload(
    record_id: uuid.UUID,
    actor: Writer,
    session: DB,
    storage: Storage,
    file: Annotated[UploadFile, File()],
) -> AttachmentView:
    try:
        return add_attachment(session, record_id, actor, storage, file.file, file.filename)
    finally:
        file.file.close()


@router.get("/{record_id}/attachments/{attachment_id}")
def attachment_download(
    record_id: uuid.UUID,
    attachment_id: uuid.UUID,
    _actor: Reader,
    session: DB,
    storage: Storage,
) -> StreamingResponse:
    item = get_attachment(session, record_id, attachment_id)
    try:
        source = storage.open(item.storage_key)
    except (OSError, ValueError):
        raise APIError(404, "NOT_FOUND", "Attachment file unavailable") from None

    def chunks() -> Iterator[bytes]:
        with source:
            while chunk := source.read(CHUNK_BYTES):
                yield chunk

    return StreamingResponse(
        chunks(),
        media_type=MIME_TYPES[item.content_type],
        headers={
            "Content-Disposition": "attachment; filename=attachment; filename*=UTF-8''"
            + quote(item.file_name, safe=""),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "Content-Length": str(item.size_bytes),
        },
    )


@router.delete("/{record_id}/attachments/{attachment_id}", status_code=204)
def attachment_remove(
    record_id: uuid.UUID, attachment_id: uuid.UUID, actor: Writer, session: DB
) -> Response:
    remove_attachment(session, record_id, attachment_id, actor)
    return Response(status_code=204)
