"""Synthetic seed is opt-in, idempotent, and preserves existing account state."""

import secrets
import uuid
from dataclasses import replace
from unittest.mock import create_autospec

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.passwords import verify_password
from app.config import ConfigurationError, Settings
from app.models import AppUser, AuditLog
from app.seed import seed_admin


def test_seed_false_never_touches_database(environment: dict[str, str]) -> None:
    session = create_autospec(Session, instance=True)
    assert seed_admin(session, Settings.from_env(environment)) is None
    assert not session.mock_calls


def test_seed_refuses_non_sandbox(environment: dict[str, str]) -> None:
    session = create_autospec(Session, instance=True)
    settings = replace(Settings.from_env(environment), sandbox_seed=True, app_env="production")
    with pytest.raises(ConfigurationError, match="APP_ENV"):
        seed_admin(session, settings)
    assert not session.mock_calls


@pytest.mark.postgresql
def test_seed_admin_idempotent_and_preserves_existing_state(
    db_session: Session,
    environment: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    password = secrets.token_urlsafe(24)
    email = f"seed-{uuid.uuid4().hex}@mtn.local"
    settings = replace(
        Settings.from_env(environment),
        sandbox_seed=True,
        seed_admin_email=email.upper(),
        seed_admin_password=password,
    )
    first = seed_admin(db_session, settings)
    assert first is not None and first.email == email and first.role == "admin"
    assert verify_password(password, first.password_hash)
    assert first.created_by == first.id and first.updated_by == first.id
    original_hash = first.password_hash
    first.display_name = "Preserved synthetic name"
    first.role = "viewer"
    first.is_active = False
    db_session.flush()
    second = seed_admin(
        db_session, replace(settings, seed_admin_password=secrets.token_urlsafe(24))
    )
    assert second is not None and second.id == first.id
    assert second.password_hash == original_hash
    assert second.display_name == "Preserved synthetic name"
    assert second.role == "viewer" and not second.is_active
    assert (
        db_session.execute(
            select(func.count()).select_from(AppUser).where(AppUser.email == email)
        ).scalar_one()
        == 1
    )
    events = (
        db_session.execute(select(AuditLog).where(AuditLog.entity_id == str(first.id)))
        .scalars()
        .all()
    )
    assert len(events) == 1 and events[0].actor == first.id
    for secret in (password, original_hash, settings.jwt_secret):
        assert secret not in str(events[0].after_json) and secret not in caplog.text


@pytest.mark.postgresql
def test_seeded_admin_can_login_with_controller_supplied_password(
    db_session: Session, auth_client: TestClient, environment: dict[str, str]
) -> None:
    settings = replace(
        Settings.from_env(environment),
        sandbox_seed=True,
        seed_admin_email=f"seed-login-{uuid.uuid4().hex}@mtn.local",
    )
    user = seed_admin(db_session, settings)
    assert user is not None
    response = auth_client.post(
        "/api/v1/auth/login",
        json={"email": settings.seed_admin_email, "password": settings.seed_admin_password},
    )
    assert response.status_code == 200
    assert response.json()["user"]["id"] == str(user.id)
    assert response.json()["user"]["role"] == "admin"
    assert "HttpOnly" in response.headers["set-cookie"]
    assert settings.seed_admin_password not in response.text


@pytest.mark.postgresql
def test_seed_rejects_short_password_without_storing_it(
    db_session: Session,
    environment: dict[str, str],
) -> None:
    settings = replace(
        Settings.from_env(environment),
        sandbox_seed=True,
        seed_admin_email=f"seed-{uuid.uuid4().hex}@mtn.local",
        seed_admin_password="short",
    )
    with pytest.raises(ConfigurationError, match="SEED_ADMIN_PASSWORD") as caught:
        seed_admin(db_session, settings)
    assert "short" not in str(caught.value)
    assert (
        db_session.execute(
            select(AppUser).where(AppUser.email == settings.seed_admin_email)
        ).first()
        is None
    )


def test_startup_bootstraps_only_when_enabled(
    environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    import app.main as main_module
    from app.main import create_app

    called = []

    def seed_spy(session: Session, settings: Settings) -> None:
        called.append(settings.sandbox_seed)

    monkeypatch.setattr(main_module, "seed_admin", seed_spy)
    with TestClient(create_app(replace(Settings.from_env(environment), sandbox_seed=True))):
        pass
    with TestClient(create_app(Settings.from_env(environment))):
        pass
    assert called == [True]
