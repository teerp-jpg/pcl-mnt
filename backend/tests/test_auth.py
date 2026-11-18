"""Password primitives and real JWT/database authorization contracts."""

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.dependencies import COOKIE_NAME, enforce_api_session, issue_token, require_role
from app.auth.passwords import hash_password
from app.config import Settings
from app.models import AppUser


@pytest.mark.postgresql
def test_login_cookie_me_logout_and_secret_redaction(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    password_pair: tuple[str, str],
    environment: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    admin = auth_users["admin"]
    response = auth_client.post(
        "/api/v1/auth/login",
        json={
            "email": f" {admin.email.upper()} ",
            "password": password_pair[0],
        },
    )
    assert response.status_code == 200
    assert response.json()["user"]["id"] == str(admin.id)
    assert set(response.json()) == {"user"}
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    assert "Max-Age=28800" in cookie and "Secure" not in cookie
    token = auth_client.cookies[COOKIE_NAME]
    claims = jwt.decode(token, environment["JWT_SECRET"], algorithms=["HS256"])
    assert claims["exp"] - claims["iat"] == 8 * 60 * 60
    assert claims["role"] == "admin" and claims["sub"] == str(admin.id)
    assert auth_client.get("/api/v1/auth/me").status_code == 200
    for secret in (password_pair[0], password_pair[1], environment["JWT_SECRET"], token):
        assert secret not in response.text
        assert secret not in caplog.text
    logout = auth_client.post("/api/v1/auth/logout")
    assert logout.status_code == 204 and "Max-Age=0" in logout.headers["set-cookie"]
    assert auth_client.get("/api/v1/auth/me").status_code == 401


@pytest.mark.postgresql
def test_invalid_credentials_share_response_and_disabled_token_is_rejected(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    password_pair: tuple[str, str],
    db_session: Session,
) -> None:
    user = auth_users["viewer"]
    token = issue_token(user, auth_client.app.state.settings)
    wrong = auth_client.post("/api/v1/auth/login", json={"email": user.email, "password": "wrong"})
    missing = auth_client.post(
        "/api/v1/auth/login",
        json={
            "email": "missing@mtn.local",
            "password": password_pair[0],
        },
    )
    user.is_active = False
    db_session.flush()
    disabled = auth_client.post(
        "/api/v1/auth/login",
        json={
            "email": user.email,
            "password": password_pair[0],
        },
    )
    assert wrong.status_code == missing.status_code == disabled.status_code == 401
    assert wrong.json() == missing.json() == disabled.json()
    assert wrong.json()["code"] == "INVALID_CREDENTIALS"
    assert all("set-cookie" not in result.headers for result in (wrong, missing, disabled))
    assert (
        auth_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )


@pytest.mark.postgresql
@pytest.mark.parametrize(
    "kind", ["expired", "forged", "algorithm", "missing", "subject", "version", "role"]
)
def test_invalid_tokens_are_rejected(
    kind: str,
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
) -> None:
    settings = auth_client.app.state.settings
    user = auth_users["viewer"]
    now = datetime.now(UTC)
    claims = {
        "sub": str(user.id),
        "role": user.role,
        "ver": user.version,
        "iat": now,
        "exp": now + timedelta(hours=8),
    }
    key, algorithm = settings.jwt_secret, "HS256"
    if kind == "expired":
        claims["exp"] = now - timedelta(seconds=1)
    elif kind == "forged":
        key = secrets.token_urlsafe(32)
    elif kind == "algorithm":
        algorithm = "HS384"
    elif kind == "missing":
        del claims["exp"]
    elif kind == "subject":
        claims["sub"] = "not-a-uuid"
    elif kind == "version":
        claims["ver"] = user.version + 1
    else:
        claims["role"] = "admin"
    token = jwt.encode(claims, key, algorithm=algorithm)
    response = auth_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401 and response.json()["code"] == "UNAUTHENTICATED"
    assert token not in response.text


@pytest.mark.postgresql
def test_auth_guard_protects_api_and_ignores_role_headers(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
) -> None:
    assert auth_client.get("/health/ready").status_code == 200
    for path, method in [
        ("/api/v1/auth/me", "get"),
        ("/api/v1/users", "get"),
        ("/api/v1/auth/logout", "post"),
    ]:
        response = getattr(auth_client, method)(path, headers={"X-Role": "admin"})
        assert response.status_code == 401
    token = issue_token(auth_users["viewer"], auth_client.app.state.settings)
    auth_client.cookies.set(COOKIE_NAME, token)
    response = auth_client.get("/api/v1/auth/me", headers={"Authorization": "Basic bad"})
    assert response.status_code == 401  # Never fall back to a cookie after a bad Bearer header.


@pytest.mark.postgresql
def test_write_origin_is_checked_even_for_login(auth_client: TestClient) -> None:
    response = auth_client.post(
        "/api/v1/auth/login",
        headers={"Origin": "https://untrusted.invalid"},
        json={"email": "nobody@mtn.local", "password": "synthetic"},
    )
    assert response.status_code == 403


@pytest.mark.postgresql
@pytest.mark.parametrize("role", ["viewer", "clerk", "manager", "admin"])
def test_require_role_uses_real_token_and_database_role(
    role: str,
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
) -> None:
    router = APIRouter(prefix="/api/v1/probe", dependencies=[Depends(enforce_api_session)])

    @router.post("", dependencies=[Depends(require_role("clerk", "manager", "admin"))])
    def role_probe() -> dict[str, bool]:
        return {"allowed": True}

    auth_client.app.include_router(router)
    token = issue_token(auth_users[role], auth_client.app.state.settings)
    response = auth_client.post("/api/v1/probe", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == (403 if role == "viewer" else 200)


def test_secure_cookie_outside_sandbox(environment: dict[str, str]) -> None:
    # Cookie security is also covered without creating a non-sandbox database.
    from dataclasses import replace
    from unittest.mock import create_autospec

    from fastapi import Response

    from app.api.v1.auth import login
    from app.auth.schemas import LoginInput

    settings = replace(Settings.from_env(environment), app_env="test")
    user = AppUser(
        id=uuid.uuid4(),
        email="synthetic@mtn.local",
        display_name="Test",
        role="admin",
        password_hash=hash_password("synthetic-unit-password"),
        is_active=True,
        version=1,
    )
    session = create_autospec(Session, instance=True)
    session.execute.return_value.scalar_one_or_none.return_value = user
    from types import SimpleNamespace

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
    response = Response()
    login(
        LoginInput(email=user.email, password="synthetic-unit-password"), request, response, session
    )
    assert "Secure" in response.headers["set-cookie"]
