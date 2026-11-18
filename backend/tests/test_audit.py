"""Audit snapshots are safe, detached, and atomic with business writes."""

import json
import secrets
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import create_autospec

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import issue_token
from app.models import AppUser, AuditLog, Garage
from app.services.audit import write_audit


def test_audit_copies_public_snapshots_and_removes_nested_credentials() -> None:
    session = create_autospec(Session, instance=True)
    private = secrets.token_urlsafe(24)
    identity = uuid.uuid4()
    after = {
        "amount": Decimal("100.01"),
        "date": date(2026, 10, 29),
        "id": identity,
        "at": datetime(2026, 10, 29, tzinfo=UTC),
        "password_hash": private,
        "nested": [{"Password": private, "JWT_SECRET": private, "name": "Public"}],
        "Authorization": private,
        "accessToken": private,
        "api-key": private,
        "file_sha256": "a" * 64,
    }
    event = write_audit(
        session,
        entity="synthetic",
        entity_id=identity,
        action="UPDATE",
        actor_id=identity,
        before={"name": "Before"},
        after=after,
        reason="Synthetic correction",
    )
    session.add.assert_called_once_with(event)
    session.commit.assert_not_called()
    session.flush.assert_not_called()
    assert private not in json.dumps(event.after_json)
    assert event.after_json == {
        "amount": "100.01",
        "date": "2026-10-29",
        "id": str(identity),
        "at": "2026-10-29 00:00:00+00:00",
        "nested": [{"name": "Public"}],
        "file_sha256": "a" * 64,
    }
    after["nested"][0]["name"] = "Changed later"
    assert event.after_json["nested"] == [{"name": "Public"}]
    assert event.reason == "Synthetic correction" and event.actor == identity


@pytest.mark.parametrize("value", [SecretStr("private"), object(), float("nan"), {1: "value"}])
def test_audit_rejects_unsafe_objects_without_stringifying(value: object) -> None:
    session = create_autospec(Session, instance=True)
    with pytest.raises(ValueError):
        write_audit(
            session,
            entity="synthetic",
            entity_id="test",
            action="UPDATE",
            actor_id=uuid.uuid4(),
            after={"field": value},
        )
    session.add.assert_not_called()


@pytest.mark.postgresql
@pytest.mark.parametrize("operation", ["create", "update", "settings"])
def test_audit_failure_rolls_back_business_change_and_event(
    operation: str,
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import master_data

    token = issue_token(auth_users["admin"], auth_client.app.state.settings)
    headers = {"Authorization": f"Bearer {token}"}
    name = f"Synthetic atomic {uuid.uuid4().hex}"
    garage_id = None
    if operation == "update":
        created = auth_client.post("/api/v1/garages", headers=headers, json={"name": name})
        assert created.status_code == 201
        garage_id = created.json()["id"]

    def fail_after_adding_audit(*args: object, **kwargs: object) -> None:
        write_audit(*args, **kwargs)
        db_session.flush()
        raise RuntimeError("Synthetic audit failure")

    monkeypatch.setattr(master_data, "write_audit", fail_after_adding_audit)
    with pytest.raises(RuntimeError, match="Synthetic audit failure"):
        if operation == "create":
            auth_client.post("/api/v1/garages", headers=headers, json={"name": name})
        elif operation == "update":
            auth_client.patch(
                f"/api/v1/garages/{garage_id}",
                headers=headers,
                json={"version": 1, "name": "Rolled back synthetic"},
            )
        else:
            auth_client.put(
                "/api/v1/settings",
                headers=headers,
                json={"pm_due_soon_pct": 1, "pm_due_soon_km": 2, "pm_due_soon_days": 3},
            )
    if operation == "create":
        assert db_session.execute(select(Garage).where(Garage.name == name)).first() is None
        assert (
            db_session.execute(select(AuditLog).where(AuditLog.entity == "garage")).first() is None
        )
    elif operation == "update":
        garage = db_session.get(Garage, uuid.UUID(garage_id))
        assert garage.name == name and garage.version == 1
        events = (
            db_session.execute(select(AuditLog).where(AuditLog.entity_id == garage_id))
            .scalars()
            .all()
        )
        assert len(events) == 1 and events[0].action == "CREATE"
    else:
        from app.models import SystemSetting

        assert db_session.execute(select(SystemSetting)).first() is None
        assert (
            db_session.execute(select(AuditLog).where(AuditLog.entity == "system_setting")).first()
            is None
        )


@pytest.mark.postgresql
def test_audit_reason_actor_time_and_money_survive_persistence(
    db_session: Session, auth_users: dict[str, AppUser]
) -> None:
    identity = uuid.uuid4()
    event = write_audit(
        db_session,
        entity="synthetic_record",
        entity_id=identity,
        action="CORRECTION",
        actor_id=auth_users["manager"].id,
        before={"total": Decimal("0.00")},
        after={"total": Decimal("100.01")},
        reason="Synthetic documented correction",
    )
    db_session.flush()
    event_id = event.id
    db_session.commit()
    db_session.expire(event)
    stored = db_session.get(AuditLog, event_id)
    assert stored.before_json == {"total": "0.00"}
    assert stored.after_json == {"total": "100.01"}
    assert stored.reason == "Synthetic documented correction"
    assert stored.actor == auth_users["manager"].id
    assert stored.at.tzinfo is not None
