"""Validate signed tokens AND current database identity on every request."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.schemas import Role
from app.config import Settings
from app.db import get_session
from app.errors import APIError
from app.models import AppUser

COOKIE_NAME = "mtn_session"
SESSION_SECONDS = 8 * 60 * 60


def unauthenticated() -> APIError:
    return APIError(401, "UNAUTHENTICATED", "Authentication required")


def issue_token(user: AppUser, settings: Settings) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user.id),
            "role": user.role,
            "ver": user.version,
            "iat": now,
            "exp": now + timedelta(seconds=SESSION_SECONDS),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def current_user(request: Request, session: Annotated[Session, Depends(get_session)]) -> AppUser:
    authorization = request.headers.get("authorization")
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or " " in token:
            raise unauthenticated()
    else:
        token = request.cookies.get(COOKIE_NAME, "")
    if not token or len(token) > 4096:
        raise unauthenticated()
    settings: Settings = request.app.state.settings
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            options={"require": ["sub", "role", "ver", "exp", "iat"]},
        )
        user_id = uuid.UUID(claims["sub"])
        if type(claims["ver"]) is not int or type(claims["exp"]) is not int:
            raise ValueError
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError, AttributeError):
        raise unauthenticated() from None
    user = session.execute(select(AppUser).where(AppUser.id == user_id)).scalar_one_or_none()
    if (
        user is None
        or not user.is_active
        or user.version != claims["ver"]
        or user.role != claims["role"]
    ):
        raise unauthenticated()
    return user


def require_role(*roles: Role) -> Callable[..., AppUser]:
    def authorized(user: Annotated[AppUser, Depends(current_user)]) -> AppUser:
        if user.role not in roles:
            raise APIError(403, "FORBIDDEN", "Permission denied")
        return user

    return authorized


def enforce_api_session(
    request: Request, session: Annotated[Session, Depends(get_session)]
) -> None:
    # An explicit Origin on writes must be allowed, including login (login CSRF).
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin is not None and origin not in request.app.state.settings.cors_origins:
            raise APIError(403, "FORBIDDEN", "Origin is not allowed")
    if request.url.path != "/api/v1/auth/login":
        current_user(request, session)
