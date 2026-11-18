"""Authenticated file lifecycle and rollback with immutable blob retention."""

import hashlib
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from maintenance_support import create, headers, setup_record
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.maintenance_records import get_storage
from app.models import AppUser, MaintenanceAttachment
from app.services import maintenance
from app.storage import LocalFileStorage

pytestmark = pytest.mark.postgresql
BASE = "/api/v1/maintenance-records"


@pytest.fixture
def storage(auth_client: TestClient, tmp_path: Path) -> LocalFileStorage:
    store = LocalFileStorage(tmp_path)
    auth_client.app.dependency_overrides[get_storage] = lambda: store
    return store


def test_upload_download_delete_and_completed_guards(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
    storage: LocalFileStorage,
) -> None:
    auth, payload, _, _ = setup_record(db_session, auth_client, auth_users)
    item = create(auth_client, auth, payload)
    path = f"{BASE}/{item['id']}"
    blob = b"%PDF-synthetic test data"
    uploaded = auth_client.post(
        path + "/attachments",
        headers=auth,
        files={"file": ("../../synthetic.exe", blob, "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    data = uploaded.json()
    assert data["content_type"] == "pdf" and data["file_name"] == "synthetic.exe"
    assert "storage_key" not in data and data["sha256"] == hashlib.sha256(blob).hexdigest()
    attachment_path = path + f"/attachments/{data['id']}"
    assert auth_client.get(attachment_path).status_code == 401
    reader = headers(auth_client, auth_users["viewer"])
    downloaded = auth_client.get(attachment_path, headers=reader)
    assert downloaded.status_code == 200 and downloaded.content == blob
    assert downloaded.headers["content-type"] == "application/pdf"
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert auth_client.delete(attachment_path, headers=reader).status_code == 403
    other = create(auth_client, auth, payload)
    assert (
        auth_client.get(f"{BASE}/{other['id']}/attachments/{data['id']}", headers=auth).status_code
        == 404
    )
    assert auth_client.delete(attachment_path, headers=auth).status_code == 204
    assert auth_client.get(attachment_path, headers=auth).status_code == 404
    # DB access is revoked while the physical blob remains for recovery.
    assert len(list(storage.root.iterdir())) == 1
    assert db_session.scalar(select(func.count()).select_from(MaintenanceAttachment)) == 0
    uploaded = auth_client.post(
        path + "/attachments", headers=auth, files={"file": ("test.pdf", blob)}
    )
    assert uploaded.status_code == 201
    version = auth_client.get(path, headers=auth).json()["version"]
    assert (
        auth_client.post(path + "/complete", headers=auth, json={"version": version}).status_code
        == 200
    )
    assert (
        auth_client.delete(path + f"/attachments/{uploaded.json()['id']}", headers=auth).status_code
        == 409
    )
    assert (
        auth_client.post(
            path + "/attachments", headers=auth, files={"file": ("test.pdf", blob)}
        ).status_code
        == 409
    )


@pytest.mark.parametrize("role", ["viewer", "clerk", "manager", "admin"])
def test_upload_role_and_validation(
    role: str,
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
    storage: LocalFileStorage,
) -> None:
    auth, payload, _, _ = setup_record(db_session, auth_client, auth_users)
    item = create(auth_client, auth, payload)
    path = f"{BASE}/{item['id']}/attachments"
    role_auth = headers(auth_client, auth_users[role])
    response = auth_client.post(
        path, headers=role_auth, files={"file": ("test.pdf", b"%PDF-synthetic")}
    )
    assert response.status_code == (403 if role == "viewer" else 201)
    if role != "viewer":
        invalid = auth_client.post(
            path, headers=role_auth, files={"file": ("fake.pdf", b"MZfake", "application/pdf")}
        )
        assert invalid.status_code == 422 and invalid.json()["code"] == "ATTACHMENT_INVALID"
    assert (
        auth_client.post(path, files={"file": ("test.pdf", b"%PDF-synthetic")}).status_code == 401
    )


def test_attachment_audit_failure_rolls_back_metadata(
    auth_client: TestClient,
    auth_users: dict[str, AppUser],
    db_session: Session,
    storage: LocalFileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth, payload, _, _ = setup_record(db_session, auth_client, auth_users)
    item = create(auth_client, auth, payload)
    path = f"{BASE}/{item['id']}"

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Synthetic audit failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(maintenance, "write_audit", fail)
        with pytest.raises(RuntimeError):
            auth_client.post(
                path + "/attachments", headers=auth, files={"file": ("test.pdf", b"%PDF-synthetic")}
            )
    assert db_session.scalar(select(func.count()).select_from(MaintenanceAttachment)) == 0
    assert auth_client.get(path, headers=auth).json()["version"] == 1
    assert len(list(storage.root.iterdir())) == 1
    assert auth_client.get(path + f"/attachments/{uuid.uuid4()}", headers=auth).status_code == 404
