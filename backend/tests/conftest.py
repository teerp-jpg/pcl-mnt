"""Unit tests never connect; integration uses only the controller's test DB."""

import os
import secrets
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from app.models import AppUser


@pytest.fixture
def environment() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+psycopg://unit@unused.invalid/mtn",
        "APP_ENV": "sandbox",
        "SANDBOX_SEED": "false",
        "JWT_SECRET": secrets.token_urlsafe(32),
        "SEED_ADMIN_EMAIL": "admin@mtn.local",
        "SEED_ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "UPLOAD_ROOT": "/tmp/mtn-unit-uploads",
        "CORS_ORIGINS": '["http://localhost:3100"]',
    }


@pytest.fixture
def application(environment: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> "FastAPI":
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    from app.config import Settings
    from app.main import create_app

    return create_app(Settings.from_env(environment))


@pytest.fixture
def db_session() -> Iterator["Session"]:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("Controller-provided PostgreSQL test DATABASE_URL is unavailable")
    if os.environ.get("SANDBOX_SEED") != "false":
        pytest.fail("PostgreSQL tests require SANDBOX_SEED=false")

    from app.db import Database

    database = Database(database_url)
    try:
        with database.engine.connect() as connection:
            transaction = connection.begin()
            try:
                with database.sessions(
                    bind=connection, join_transaction_mode="create_savepoint"
                ) as session:
                    yield session
            finally:
                if transaction.is_active:
                    transaction.rollback()
    finally:
        database.dispose()


@pytest.fixture(scope="session")
def password_pair() -> tuple[str, str]:
    from app.auth.passwords import hash_password

    password = secrets.token_urlsafe(24)
    return password, hash_password(password)


@pytest.fixture
def auth_users(db_session: "Session", password_pair: tuple[str, str]) -> dict[str, "AppUser"]:
    from app.models import AppUser

    users: dict[str, AppUser] = {}
    for role in ("viewer", "clerk", "manager", "admin"):
        user = AppUser(
            email=f"{role}-{uuid.uuid4().hex}@mtn.local",
            display_name=f"Synthetic {role}",
            role=role,
            password_hash=password_pair[1],
            is_active=True,
        )
        db_session.add(user)
        users[role] = user
    db_session.flush()
    return users


@pytest.fixture
def auth_client(
    db_session: "Session", environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> Iterator["TestClient"]:
    # Resolve db_session before replacing environment with disconnected unit settings.
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.db import get_session

    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    from app.main import create_app

    application = create_app(Settings.from_env(environment))

    def override_session() -> Iterator["Session"]:
        yield db_session

    application.dependency_overrides[get_session] = override_session
    with TestClient(application) as client:
        yield client
