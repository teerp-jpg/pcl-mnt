"""Public registry fields; CORE identity is assigned only by sync/import."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.master_data import Input

type VehicleStatus = Literal["ACTIVE", "INACTIVE", "DISPOSED"]
type SyncStatus = Literal["SYNCED", "PENDING_CREATE", "PENDING_UPDATE", "EXPORTED"]
Label = Annotated[str, Field(min_length=1, max_length=200)]


def normalize_plate(value: str) -> str:
    return "".join(character for character in value if not character.isspace() and character != "-")


class VehicleCreate(Input):
    plate_no: Label
    province: Label
    vehicle_type: Label | None = None
    brand: Label | None = None
    model: Label | None = None
    year: int | None = Field(default=None, ge=1000, le=9999, strict=True)
    chassis_no: Label | None = None
    status: VehicleStatus = "ACTIVE"
    disposed_at: date | None = None

    @field_validator("plate_no")
    @classmethod
    def plate_searchable(cls, value: str) -> str:
        if not normalize_plate(value):
            raise ValueError("Plate must contain searchable characters")
        return value

    @field_validator("year")
    @classmethod
    def gregorian_year(cls, value: int | None) -> int | None:
        return value - 543 if value is not None and value > 2400 else value


class VehiclePatch(Input):
    version: int = Field(ge=1, strict=True)
    plate_no: Label | None = None
    province: Label | None = None
    vehicle_type: Label | None = None
    brand: Label | None = None
    model: Label | None = None
    year: int | None = Field(default=None, ge=1000, le=9999, strict=True)
    chassis_no: Label | None = None
    status: VehicleStatus | None = None
    disposed_at: date | None = None

    @field_validator("plate_no", "province", "status")
    @classmethod
    def not_null(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("Explicit null is not allowed")
        return value

    @field_validator("plate_no")
    @classmethod
    def plate_searchable(cls, value: str) -> str:
        return VehicleCreate.plate_searchable(value)

    @field_validator("year")
    @classmethod
    def gregorian_year(cls, value: int | None) -> int | None:
        return VehicleCreate.gregorian_year(value)


class ConfirmCoreSync(Input):
    core_asset_id: Label
    # The approved API permits {core_asset_id}; clients may additionally guard
    # acknowledgements against a newer export with its observed version.
    version: int | None = Field(default=None, ge=1, strict=True)


class VehicleView(VehicleCreate):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    plate_no_norm: str
    core_asset_id: str | None
    origin: Literal["CORE_IMPORT", "MTN"]
    core_sync_status: SyncStatus
    last_imported_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime

    # Output must preserve stored years; importing/creating normalizes once.
    @field_validator("year")
    @classmethod
    def gregorian_year(cls, value: int | None) -> int | None:
        return value


class PlateHistoryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    plate_no: str
    province: str
    valid_from: datetime
    valid_to: datetime | None
    source: str


class VehicleDetail(VehicleView):
    plate_history: list[PlateHistoryView]


class VehiclePage(BaseModel):
    items: list[VehicleView]
    total: int
    page: int
    page_size: int


class RecordSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    record_no: str
    plate_no_snapshot: str
    province_snapshot: str
    maintenance_type: str
    status: str
    service_start_date: date | None
    service_end_date: date | None
    total_amount: Decimal


class PlanSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    interval_km: int | None
    interval_days: int | None
    last_done_at: date | None
    last_done_km: int | None


class VehicleSummary(BaseModel):
    vehicle_id: uuid.UUID
    completed_count: int
    total_cost: Decimal
    latest_record: RecordSummary | None
    pm_plans: list[PlanSummary]
    # NULL means no active plans or insufficient observations; never display as OK.
    pm_status: str | None = None
