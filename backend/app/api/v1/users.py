"""Administrator-only local user management; no destructive user endpoint."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import require_role
from app.auth.passwords import hash_password
from app.auth.schemas import CreateUserInput, ResetPasswordInput, UpdateUserInput, UserView
from app.auth.users import audit_user, locked_admin
from app.db import get_session
from app.errors import APIError
from app.models import AppUser

router = APIRouter(prefix="/users", tags=["users"])
Admin = Annotated[AppUser, Depends(require_role("admin"))]
DB = Annotated[Session, Depends(get_session)]


@router.get("", response_model=list[UserView])
def list_users(
    _actor: Admin,
    session: DB,
    response: Response,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[UserView]:
    response.headers["Cache-Control"] = "no-store"
    users = session.execute(
        select(AppUser).order_by(AppUser.email).offset(offset).limit(limit)
    ).scalars()
    return [UserView.model_validate(user) for user in users]


@router.post("", response_model=UserView, status_code=201)
def create_user(payload: CreateUserInput, actor: Admin, session: DB) -> UserView:
    actor = locked_admin(session, actor)
    if session.execute(select(AppUser.id).where(AppUser.email == payload.email)).first():
        raise APIError(409, "USER_EMAIL_EXISTS", "Email is already registered")
    user = AppUser(
        email=payload.email,
        display_name=payload.display_name,
        role=payload.role,
        password_hash=hash_password(payload.password.get_secret_value()),
        is_active=True,
        created_by=actor.id,
        updated_by=actor.id,
    )
    session.add(user)
    session.flush()
    audit_user(session, user, actor, "CREATE", None)
    session.commit()
    return UserView.model_validate(user)


def find_user(session: Session, user_id: uuid.UUID) -> AppUser:
    user = session.execute(
        select(AppUser).where(AppUser.id == user_id).execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if user is None:
        raise APIError(404, "NOT_FOUND", "User not found")
    return user


@router.patch("/{user_id}", response_model=UserView)
def update_user(
    user_id: uuid.UUID, payload: UpdateUserInput, actor: Admin, session: DB
) -> UserView:
    actor = locked_admin(session, actor)
    user = find_user(session, user_id)
    if user.version != payload.version:
        raise APIError(409, "VERSION_CONFLICT", "User has changed")
    if (
        user.is_active
        and user.role == "admin"
        and (payload.is_active is False or (payload.role is not None and payload.role != "admin"))
    ):
        other_admin = session.execute(
            select(AppUser.id).where(
                AppUser.role == "admin", AppUser.is_active.is_(True), AppUser.id != user.id
            )
        ).first()
        if other_admin is None:
            raise APIError(409, "LAST_ADMIN", "At least one active administrator is required")
    before = UserView.model_validate(user).model_dump(mode="json")
    for field, value in payload.model_dump(exclude_unset=True, exclude={"version"}).items():
        setattr(user, field, value)
    user.version += 1
    user.updated_by = actor.id
    session.flush()
    audit_user(session, user, actor, "UPDATE", before)
    session.commit()
    return UserView.model_validate(user)


@router.post("/{user_id}/reset-password", status_code=204)
def reset_password(
    user_id: uuid.UUID, payload: ResetPasswordInput, actor: Admin, session: DB
) -> Response:
    actor = locked_admin(session, actor)
    user = find_user(session, user_id)
    before = UserView.model_validate(user).model_dump(mode="json")
    user.password_hash = hash_password(payload.new_password.get_secret_value())
    user.version += 1  # Invalidates all previously issued tokens for this user.
    user.updated_by = actor.id
    session.flush()
    audit_user(session, user, actor, "RESET_PASSWORD", before)
    session.commit()
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
