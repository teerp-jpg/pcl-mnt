"""Pure input validation; no database or application startup."""

import pytest
from pydantic import ValidationError

from app.schemas.master_data import (
    CategoryCreate,
    CategoryPatch,
    GarageCreate,
    GaragePatch,
    PMSettings,
)


def test_master_whitespace_and_nullable_patch_fields() -> None:
    garage = GarageCreate(name="  Synthetic Garage  ", tax_id="0123456789012")
    assert garage.name == "Synthetic Garage" and garage.tax_id == "0123456789012"
    patch = GaragePatch(version=2, tax_id=None, contact_name=None)
    assert patch.model_dump(exclude_unset=True) == {
        "version": 2,
        "tax_id": None,
        "contact_name": None,
    }
    assert CategoryPatch(parent_id=None).model_dump(exclude_unset=True) == {"parent_id": None}


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "  "},
        {"name": "Garage", "tax_id": "123"},
        {"name": "Garage", "tax_id": "๑๒๓๔๕๖๗๘๙๐๑๒๓"},
        {"name": "Garage", "tax_id": "123456789012x"},
        {"name": "Garage", "tax_id": 1234567890123},
        {"name": "Garage", "is_active": "false"},
        {"name": "Garage", "name_norm": "override"},
    ],
)
def test_garage_create_validation(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GarageCreate.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "Garage"},
        {"version": 0},
        {"version": True},
        {"version": "1"},
        {"version": 1, "name": None},
        {"version": 1, "name": " "},
        {"version": 1, "is_active": None},
        {"version": 1, "updated_by": "override"},
    ],
)
def test_garage_patch_validation(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GaragePatch.model_validate(payload)


@pytest.mark.parametrize("field", ["code", "name_th", "is_active"])
def test_category_patch_required_fields_cannot_be_null(field: str) -> None:
    with pytest.raises(ValidationError):
        CategoryPatch.model_validate({field: None})


def test_category_name_and_code_are_required() -> None:
    with pytest.raises(ValidationError):
        CategoryCreate(code=" ", name_th="Synthetic")
    with pytest.raises(ValidationError):
        CategoryCreate(code="TEST", name_th=" ")


@pytest.mark.parametrize(
    "change",
    [
        {"pm_due_soon_pct": -1},
        {"pm_due_soon_pct": 101},
        {"pm_due_soon_pct": "10"},
        {"pm_due_soon_pct": True},
        {"pm_due_soon_pct": float("nan")},
        {"pm_due_soon_pct": float("inf")},
        {"pm_due_soon_km": -1},
        {"pm_due_soon_km": 0.5},
        {"pm_due_soon_days": True},
        {"pm_due_soon_days": "14"},
        {"JWT_SECRET": "forbidden"},
    ],
)
def test_settings_reject_invalid_thresholds(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PMSettings.model_validate(
            {"pm_due_soon_pct": 10, "pm_due_soon_km": 500, "pm_due_soon_days": 14, **change}
        )


def test_settings_allow_zero_and_fractional_percentage() -> None:
    settings = PMSettings(pm_due_soon_pct=2.5, pm_due_soon_km=0, pm_due_soon_days=0)
    assert settings.pm_due_soon_pct == 2.5
