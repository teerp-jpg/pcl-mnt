"""User writes serialize administrator checks and audit in the same transaction."""

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth.dependencies import unauthenticated
from app.auth.schemas import UserView
from app.errors import APIError
from app.models import AppUser
from app.services.audit import write_audit


def lock_user_writes(session: Session) -> None:
    # One transaction lock shared by creation, PATCH, reset and bootstrap prevents
    # two administrators concurrently removing the last active administrator.
    session.execute(text("SELECT pg_advisory_xact_lock(6835046301123)"))


def locked_admin(session: Session, actor: AppUser) -> AppUser:
    original_version = actor.version
    lock_user_writes(session)
    fresh = session.execute(
        select(AppUser).where(AppUser.id == actor.id).execution_options(populate_existing=True)
    ).scalar_one()
    if not fresh.is_active or fresh.version != original_version:
        raise unauthenticated()
    if fresh.role != "admin":
        raise APIError(403, "FORBIDDEN", "Permission denied")
    return fresh


def audit_user(
    session: Session, user: AppUser, actor: AppUser, action: str, before: dict[str, object] | None
) -> None:
    write_audit(
        session,
        entity="app_user",
        entity_id=user.id,
        action=action,
        before=before,
        after=UserView.model_validate(user).model_dump(mode="json"),
        actor_id=actor.id,
    )
