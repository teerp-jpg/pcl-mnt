from collections.abc import Iterator
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db import get_session


def test_readiness_queries_session(application: FastAPI) -> None:
    session = create_autospec(Session, instance=True)
    session.execute.return_value.scalar_one.return_value = 1

    def override_session() -> Iterator[Session]:
        yield session

    application.dependency_overrides[get_session] = override_session
    with TestClient(application) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert response.headers["cache-control"] == "no-store"
    session.execute.assert_called_once()
    assert str(session.execute.call_args.args[0]) == "SELECT 1"


def test_database_failure_is_unavailable_and_redacted(
    application: FastAPI, environment: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    session = create_autospec(Session, instance=True)
    private_message = environment["JWT_SECRET"]
    session.execute.side_effect = OperationalError("SELECT 1", {}, RuntimeError(private_message))

    def override_session() -> Iterator[Session]:
        yield session

    application.dependency_overrides[get_session] = override_session
    with TestClient(application) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert private_message not in response.text
    assert private_message not in caplog.text


def test_credentialed_cors_is_limited_to_allowlist(application: FastAPI) -> None:
    with TestClient(application) as client:
        allowed = client.options(
            "/health/ready",
            headers={
                "Origin": "http://localhost:3100",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization,Content-Type",
            },
        )
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == "http://localhost:3100"
        assert allowed.headers["access-control-allow-credentials"] == "true"
        denied = client.options(
            "/health/ready",
            headers={
                "Origin": "https://untrusted.invalid",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert denied.status_code == 400
        assert "access-control-allow-origin" not in denied.headers


def test_startup_does_not_connect_or_migrate(
    application: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    def disallow_connection() -> None:
        pytest.fail("Startup must not connect to the database or run migrations")

    monkeypatch.setattr(application.state.database.engine, "connect", disallow_connection)
    with TestClient(application) as client:
        assert client.get("/openapi.json").status_code == 200


@pytest.mark.postgresql
def test_readiness_against_postgresql(
    db_session: Session, environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fixture evaluates the controller's original env before installing unit env.
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    from app.config import Settings
    from app.main import create_app

    application = create_app(Settings.from_env(environment))

    def override_session() -> Iterator[Session]:
        yield db_session

    application.dependency_overrides[get_session] = override_session
    with TestClient(application) as client:
        response = client.get("/health/ready")
    assert db_session.bind is not None
    assert db_session.bind.dialect.name == "postgresql"
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
