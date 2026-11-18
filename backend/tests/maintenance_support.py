"""Synthetic fixtures shared only by T7 integration tests."""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.dependencies import issue_token
from app.models import AppUser, Garage, MaintenanceCategory, Vehicle


def headers(client: TestClient, user: AppUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(user, client.app.state.settings)}"}


def setup_record(
    session: Session, client: TestClient, users: dict[str, AppUser], **overrides: object
) -> tuple[dict[str, str], dict[str, object], Vehicle, Garage]:
    vehicle = Vehicle(
        plate_no="SYN-" + uuid.uuid4().hex,
        plate_no_norm=uuid.uuid4().hex,
        province="Synthetic",
        origin="MTN",
        core_sync_status="PENDING_CREATE",
    )
    garage = Garage(name="Synthetic garage", name_norm=uuid.uuid4().hex)
    category = MaintenanceCategory(code=uuid.uuid4().hex, name_th="Synthetic category")
    session.add_all([vehicle, garage, category])
    session.flush()
    payload: dict[str, object] = {
        "vehicle_id": str(vehicle.id),
        "maintenance_type": "PM",
        "service_start_date": "2026-10-10",
        "service_end_date": "2026-10-10",
        "odometer_km": 120500,
        "garage_id": str(garage.id),
        "vat_amount": "105.00",
        "lines": [
            {
                "category_id": str(category.id),
                "line_type": "PART",
                "description": "Synthetic part",
                "qty": "1.00",
                "unit_price": "1500.00",
            }
        ],
        **overrides,
    }
    return headers(client, users["manager"]), payload, vehicle, garage


def create(client: TestClient, auth: dict[str, str], payload: dict[str, object]) -> dict:
    response = client.post("/api/v1/maintenance-records", headers=auth, json=payload)
    assert response.status_code == 201, response.text
    return response.json()
