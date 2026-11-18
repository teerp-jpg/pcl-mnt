"""PM writes preserve the existing schema; engine-hour scheduling is phase 2."""

import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.pm_due import PMStatus
from app.schemas.master_data import Input, Name

PositiveCount = Annotated[int, Field(gt=0, le=2147483647, strict=True)]
Reading = Annotated[int, Field(ge=0, le=2147483647, strict=True)]


class PlanCreate(Input):
    vehicle_id: uuid.UUID
    name: Name
    category_id: uuid.UUID | None = None
    interval_km: PositiveCount | None = None
    interval_days: PositiveCount | None = None
    last_done_at: date | None = None
    last_done_km: Reading | None = None
    is_active: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def interval_required(self) -> "PlanCreate":
        if self.interval_km is None and self.interval_days is None:
            raise ValueError("At least one distance or day interval is required")
        return self


class PlanPatch(Input):
    version: int = Field(ge=1, strict=True)
    name: Name | None = None
    category_id: uuid.UUID | None = None
    interval_km: PositiveCount | None = None
    interval_days: PositiveCount | None = None
    last_done_at: date | None = None
    last_done_km: Reading | None = None
    is_active: bool | None = Field(default=None, strict=True)

    @field_validator("name", "is_active")
    @classmethod
    def not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Explicit null is not allowed")
        return value


class PlanView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    vehicle_id: uuid.UUID
    name: str
    category_id: uuid.UUID | None
    interval_km: int | None
    interval_days: int | None
    last_done_at: date | None
    last_done_km: int | None
    is_active: bool
    version: int


class DueView(PlanView):
    plate_no: str
    province: str
    status: PMStatus | None
    reason: str | None
    next_due_km: int | None
    next_due_at: date | None
    current_km: int | None
    reading_at: datetime | None
    remaining_km: int | None
    remaining_days: int | None
    data_as_of: date


class DuePage(BaseModel):
    items: list[DueView]
    total: int
    offset: int
    limit: int
