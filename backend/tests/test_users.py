"""Real PostgreSQL user management, authorization, audit and session invalidation."""

import json
import secrets
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth.dependencies import issue_token
from app.auth.passwords import verify_password
from app.auth.users import lock_user_writes
from app.models import AppUser, AuditLog


def authorization(client: TestClient, user: AppUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(user, client.app.state.settings)}"}


@pytest.mark.postgresql
@pytest.mark.parametrize("role", ["viewer", "clerk", "manager"])
def test_only_admin_manages_users(
    role: str,
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
) -> None:
    headers = authorization(auth_client, auth_users[role])
    user_id = auth_users["viewer"].id
    for method, path, body in [
        ("get", "/api/v1/users", None),
        ("post", "/api/v1/users", {}),
        ("patch", f"/api/v1/users/{user_id}", {}),
        ("post", f"/api/v1/users/{user_id}/reset-password", {}),
    ]:
        kwargs = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        assert getattr(auth_client, method)(path, **kwargs).status_code == 403


@pytest.mark.postgresql
def test_create_update_reset_and_audit_are_safe(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = authorization(auth_client, auth_users["admin"])
    password, replacement = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    email = f"user-{uuid.uuid4().hex}@mtn.local"
    created = auth_client.post(
        "/api/v1/users",
        headers=headers,
        json={
            "email": f" {email.upper()} ",
            "display_name": " Synthetic user ",
            "role": "clerk",
            "password": password,
        },
    )
    assert created.status_code == 201
    body = created.json()
    user_id = body["id"]
    assert body["email"] == email and body["display_name"] == "Synthetic user"
    assert set(body) == {"id", "email", "display_name", "role", "is_active", "version"}
    user = db_session.get(AppUser, uuid.UUID(user_id))
    assert user is not None and verify_password(password, user.password_hash)
    assert user.created_by == auth_users["admin"].id
    duplicate = auth_client.post(
        "/api/v1/users",
        headers=headers,
        json={
            "email": email.upper(),
            "display_name": "Duplicate",
            "role": "viewer",
            "password": password,
        },
    )
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "USER_EMAIL_EXISTS"
    updated = auth_client.patch(
        f"/api/v1/users/{user_id}",
        headers=headers,
        json={
            "version": body["version"],
            "display_name": "Updated",
            "role": "manager",
        },
    )
    assert updated.status_code == 200 and updated.json()["version"] == body["version"] + 1
    stale = auth_client.patch(
        f"/api/v1/users/{user_id}",
        headers=headers,
        json={
            "version": body["version"],
            "display_name": "Stale",
        },
    )
    assert stale.status_code == 409 and stale.json()["code"] == "VERSION_CONFLICT"
    old_token = issue_token(user, auth_client.app.state.settings)
    reset = auth_client.post(
        f"/api/v1/users/{user_id}/reset-password",
        headers=headers,
        json={"new_password": replacement},
    )
    assert reset.status_code == 204 and verify_password(replacement, user.password_hash)
    assert (
        auth_client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {old_token}"}
        ).status_code
        == 401
    )
    assert (
        auth_client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        ).status_code
        == 401
    )
    assert (
        auth_client.post(
            "/api/v1/auth/login", json={"email": email, "password": replacement}
        ).status_code
        == 200
    )
    listed = auth_client.get("/api/v1/users", headers=headers)
    assert listed.status_code == 200 and any(item["id"] == user_id for item in listed.json())
    audits = (
        db_session.execute(
            select(AuditLog).where(AuditLog.entity_id == user_id).order_by(AuditLog.at)
        )
        .scalars()
        .all()
    )
    assert {audit.action for audit in audits} == {"CREATE", "UPDATE", "RESET_PASSWORD"}
    assert all(audit.actor == auth_users["admin"].id for audit in audits)
    changed = next(audit for audit in audits if audit.action == "UPDATE")
    assert changed.before_json is not None and changed.after_json is not None
    assert changed.before_json["role"] == "clerk"
    assert changed.after_json["role"] == "manager"
    assert changed.before_json["version"] + 1 == changed.after_json["version"]
    audit_text = json.dumps([(audit.before_json, audit.after_json) for audit in audits])
    for secret in (password, replacement, user.password_hash, "password_hash", "new_password"):
        assert secret not in audit_text and secret not in listed.text and secret not in caplog.text


@pytest.mark.postgresql
@pytest.mark.parametrize("change", [{"is_active": False}, {"role": "viewer"}])
def test_last_admin_cannot_be_removed(
    change: dict[str, object],
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    admin = auth_users["admin"]
    response = auth_client.patch(
        f"/api/v1/users/{admin.id}",
        headers=authorization(auth_client, admin),
        json={"version": admin.version, **change},
    )
    assert response.status_code == 409 and response.json()["code"] == "LAST_ADMIN"
    assert admin.is_active and admin.role == "admin" and admin.version == 1
    assert (
        db_session.execute(select(AuditLog).where(AuditLog.entity_id == str(admin.id))).first()
        is None
    )


@pytest.mark.postgresql
def test_admin_can_change_self_when_another_active_admin_exists(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
) -> None:
    other = auth_users["manager"]
    other.role = "admin"
    db_session.flush()
    admin = auth_users["admin"]
    old_headers = authorization(auth_client, admin)
    response = auth_client.patch(
        f"/api/v1/users/{admin.id}",
        headers=old_headers,
        json={"version": admin.version, "role": "viewer"},
    )
    assert response.status_code == 200
    assert auth_client.get("/api/v1/users", headers=old_headers).status_code == 401
    assert (
        auth_client.get("/api/v1/users", headers=authorization(auth_client, admin)).status_code
        == 403
    )
    denied = auth_client.patch(
        f"/api/v1/users/{other.id}",
        headers=authorization(auth_client, other),
        json={"version": other.version, "is_active": False},
    )
    assert denied.status_code == 409 and denied.json()["code"] == "LAST_ADMIN"


@pytest.mark.postgresql
def test_user_write_lock_excludes_a_second_transaction(db_session: Session) -> None:
    lock_user_writes(db_session)
    connection = db_session.connection()
    assert connection.engine is not None
    with connection.engine.connect() as second:
        assert (
            second.execute(text("SELECT pg_try_advisory_xact_lock(6835046301123)")).scalar_one()
            is False
        )


@pytest.mark.postgresql
@pytest.mark.parametrize(
    "payload",
    [
        {"role": "owner", "version": 1},
        {"is_active": None, "version": 1},
        {"display_name": " ", "version": 1},
        {"role": "viewer"},
        {"password_hash": "forbidden", "version": 1},
    ],
)
def test_user_patch_rejects_invalid_fields(
    payload: dict[str, object],
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
) -> None:
    response = auth_client.patch(
        f"/api/v1/users/{auth_users['viewer'].id}",
        json=payload,
        headers=authorization(auth_client, auth_users["admin"]),
    )
    assert response.status_code == 422


@pytest.mark.postgresql
def test_short_password_and_missing_user(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
) -> None:
    headers = authorization(auth_client, auth_users["admin"])
    user_id = auth_users["viewer"].id
    short = auth_client.post(
        f"/api/v1/users/{user_id}/reset-password", headers=headers, json={"new_password": "short"}
    )
    assert short.status_code == 422
    # Check the full safe envelope: "short" also occurs in the error code "too_short".
    assert short.json() == {
        "code": "VALIDATION_FAILED",
        "message": "Request validation failed",
        "details": {"errors": [{"location": ["body", "new_password"], "type": "too_short"}]},
    }
    missing = auth_client.patch(
        f"/api/v1/users/{uuid.uuid4()}",
        headers=headers,
        json={"version": 1, "display_name": "missing"},
    )
    assert missing.status_code == 404
