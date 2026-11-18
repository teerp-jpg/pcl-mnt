"""Local cookie/Bearer authentication for the approved sandbox."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import COOKIE_NAME, SESSION_SECONDS, current_user, issue_token
from app.auth.passwords import DUMMY_HASH, verify_password
from app.auth.schemas import LoginInput, UserResponse, UserView
from app.config import Settings
from app.db import get_session
from app.errors import APIError
from app.models import AppUser

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=UserResponse)
def login(
    payload: LoginInput,
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
) -> UserResponse:
    user = session.execute(
        select(AppUser).where(AppUser.email == payload.email.strip().lower())
    ).scalar_one_or_none()
    valid_password = verify_password(
        payload.password.get_secret_value(), user.password_hash if user else DUMMY_HASH
    )
    if user is None or not valid_password or not user.is_active:
        raise APIError(401, "INVALID_CREDENTIALS", "Invalid email or password")
    settings: Settings = request.app.state.settings
    response.set_cookie(
        COOKIE_NAME,
        issue_token(user, settings),
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=settings.app_env != "sandbox",
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return UserResponse(user=UserView.model_validate(user))


@router.get("/me", response_model=UserResponse)
def me(user: Annotated[AppUser, Depends(current_user)], response: Response) -> UserResponse:
    response.headers["Cache-Control"] = "no-store"
    return UserResponse(user=UserView.model_validate(user))


@router.post("/logout", status_code=204)
def logout(request: Request) -> Response:
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    response.delete_cookie(
        COOKIE_NAME,
        httponly=True,
        secure=request.app.state.settings.app_env != "sandbox",
        samesite="lax",
        path="/",
    )
    return response
