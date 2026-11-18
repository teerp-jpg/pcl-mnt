"""Single transition table and transactional in-process extension point."""

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.errors import APIError

TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"IN_PROGRESS", "COMPLETED", "CANCELLED"}),
    "IN_PROGRESS": frozenset({"COMPLETED", "CANCELLED"}),
    "COMPLETED": frozenset(),
    "CANCELLED": frozenset(),
}


def require_transition(source: str, target: str) -> None:
    if target not in TRANSITIONS.get(source, frozenset()):
        raise APIError(409, "INVALID_STATE_TRANSITION", "Transition is not allowed")


@dataclass(frozen=True)
class RecordStatusChanged:
    record_id: uuid.UUID
    before: str
    after: str
    actor_id: uuid.UUID
    name: str = "record_status_changed"


def emit_status_changed(session: Session, event: RecordStatusChanged) -> None:
    # Handlers run in the caller's transaction: only transactional DB work is
    # safe here. No external I/O; an exception rolls back the record and audit.
    for handler in session.info.get("record_status_changed_handlers", ()):
        handler(session, event)
