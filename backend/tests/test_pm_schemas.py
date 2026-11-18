"""Reject invalid PM writes before SQL execution."""

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.pm_plans import PlanCreate, PlanPatch


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"interval_km": 0},
        {"interval_days": -1},
        {"interval_km": True},
        {"interval_km": "1000"},
        {"interval_days": 2147483648},
        {"interval_days": 30, "interval_engine_hours": "100.00"},
        {"interval_days": 30, "last_done_km": -1},
        {"interval_days": 30, "name": " "},
    ],
)
def test_invalid_create(changes):
    with pytest.raises(ValidationError):
        PlanCreate.model_validate({"vehicle_id": str(uuid.uuid4()), "name": "PM", **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"name": None},
        {"is_active": None},
        {"vehicle_id": str(uuid.uuid4())},
        {"version": 0},
        {"version": True},
        {"last_done_km": 1.5},
    ],
)
def test_invalid_patch(changes):
    with pytest.raises(ValidationError):
        PlanPatch.model_validate({"version": 1, **changes})


def test_nullable_patch_and_time_only_plan():
    assert PlanCreate(vehicle_id=uuid.uuid4(), name="PM", interval_days=180).interval_km is None
    payload = PlanPatch(version=1, last_done_at=None, interval_km=None)
    assert payload.model_dump(exclude_unset=True) == {
        "version": 1,
        "last_done_at": None,
        "interval_km": None,
    }
