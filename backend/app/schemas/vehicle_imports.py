"""Two-step import contract: decisions reference persisted row numbers only."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.master_data import Input
from app.schemas.vehicles import Label, VehicleCreate

type ImportResult = Literal["NEW", "UPDATE", "UNCHANGED", "CONFLICT", "ERROR"]
type Resolution = Literal["LINK", "USE_CORE", "KEEP_MNT"]


class CoreAssetValues(VehicleCreate):
    core_asset_id: Label

    @field_validator("*", mode="before")
    @classmethod
    def safe_text(cls, value: object) -> object:
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("NUL is not allowed")
        return value


class RowResolution(Input):
    row_no: int = Field(ge=1, le=10000, strict=True)
    resolution: Resolution


class ImportResolutions(Input):
    resolutions: list[RowResolution] = Field(min_length=1, max_length=10000)

    @field_validator("resolutions")
    @classmethod
    def distinct_rows(cls, value: list[RowResolution]) -> list[RowResolution]:
        if len({item.row_no for item in value}) != len(value):
            raise ValueError("Row numbers must be distinct")
        return value


class ImportRowView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    row_no: int
    raw_json: dict[str, object]
    result: ImportResult
    matched_vehicle_id: uuid.UUID | None
    messages_json: list[object]
    resolution: Resolution | None


class ImportView(BaseModel):
    id: uuid.UUID
    file_name: str
    file_sha256: str
    status: Literal["VALIDATED", "APPLIED", "DISCARDED"]
    row_count: int
    summary: dict[str, object]
    warnings: list[str]
    uploaded_at: datetime
    applied_at: datetime | None
    rows: list[ImportRowView]
