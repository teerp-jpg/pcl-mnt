"""Pure registry validation without database/application imports."""

import pytest
from pydantic import ValidationError

from app.schemas.vehicles import VehicleCreate, VehiclePatch, normalize_plate


@pytest.mark.parametrize("plate", ["70-1234", " 7 0 - 1 2 3 4 ", "70\t1234"])
def test_plate_search_normalizes_spaces_and_hyphens(plate: str) -> None:
    assert normalize_plate(plate) == "701234"


@pytest.mark.parametrize(
    "fields",
    [
        {"plate_no": " - - "},
        {"province": " "},
        {"year": True},
        {"year": 999},
        {"status": "APPROVED"},
        {"core_asset_id": "UNAUTHORIZED"},
        {"origin": "CORE_IMPORT"},
        {"core_sync_status": "SYNCED"},
    ],
)
def test_invalid_or_server_owned_fields_rejected(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        VehicleCreate.model_validate({"plate_no": "70-1234", "province": "กทม.", **fields})


@pytest.mark.parametrize("field", ["plate_no", "province", "status"])
def test_patch_cannot_null_required_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        VehiclePatch.model_validate({"version": 1, field: None})


def test_year_conversion_and_nullable_patch() -> None:
    payload = VehicleCreate(plate_no="70-1234", province="กทม.", year=2569)
    assert payload.year == 2026
    assert VehiclePatch(version=1, year=2568).year == 2025
    assert VehiclePatch(version=1, chassis_no=None).model_dump(exclude_unset=True) == {
        "version": 1,
        "chassis_no": None,
    }


def test_output_year_is_not_converted_twice() -> None:
    import uuid
    from datetime import UTC, datetime

    from app.schemas.vehicles import VehicleView

    result = VehicleView.model_validate(
        {
            "id": uuid.uuid4(),
            "plate_no": "70-1234",
            "plate_no_norm": "701234",
            "province": "สมมติ",
            "core_asset_id": None,
            "origin": "MTN",
            "core_sync_status": "PENDING_CREATE",
            "last_imported_at": None,
            "version": 1,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
            "year": 2500,
        }
    )
    assert result.year == 2500
