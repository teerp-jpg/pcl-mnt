"""Money inputs preserve exact decimals; computed client totals are ignored."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

from app.schemas.master_data import Input


def exact_decimal(value: object) -> object:
    # JSON decimal strings avoid binary float conversion altogether. Integers
    # and Decimal are exact too; JSON fractional numbers must use strings.
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Use an exact decimal string")
    return value


Money = Annotated[
    Decimal,
    BeforeValidator(exact_decimal, json_schema_input_type=str | int),
    Field(ge=0, max_digits=14, decimal_places=2, allow_inf_nan=False),
    PlainSerializer(lambda value: format(value, ".2f"), return_type=str, when_used="json"),
]
Quantity = Annotated[
    Decimal,
    BeforeValidator(exact_decimal, json_schema_input_type=str | int),
    Field(gt=0, max_digits=10, decimal_places=2, allow_inf_nan=False),
    PlainSerializer(lambda value: format(value, ".2f"), return_type=str, when_used="json"),
]
Text = Annotated[str, Field(min_length=1, max_length=4000)]
MaintenanceType = Literal["PM", "CM"]
RecordStatus = Literal["DRAFT", "IN_PROGRESS", "COMPLETED", "CANCELLED"]


class LineInput(Input):
    category_id: uuid.UUID
    line_type: Literal["PART", "LABOUR", "OTHER"]
    description: Text
    qty: Quantity
    unit_price: Money
    is_warranty: bool = Field(default=False, strict=True)
    note: Text | None = None
    amount: object = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def free_line_explained(self) -> "LineInput":
        if self.unit_price == 0 and not self.is_warranty and not self.note:
            raise ValueError("Zero price requires warranty or note")
        return self


class RecordFields(Input):
    maintenance_type: MaintenanceType = "PM"
    service_start_date: date | None = None
    service_end_date: date | None = None
    odometer_km: int | None = Field(default=None, ge=0, le=2147483647, strict=True)
    odometer_missing_reason: Text | None = None
    odometer_regression_reason: Text | None = None
    garage_id: uuid.UUID | None = None
    pm_plan_id: uuid.UUID | None = None
    failure_description: Text | None = None
    invoice_no: Text | None = None
    vat_amount: Money = Decimal("0.00")
    notes: Text | None = None
    lines: list[LineInput] = Field(default_factory=list, max_length=200)
    subtotal: object = Field(default=None, exclude=True)
    total_amount: object = Field(default=None, exclude=True)
    amount: object = Field(default=None, exclude=True)


class RecordCreate(RecordFields):
    vehicle_id: uuid.UUID


class RecordChanges(Input):
    maintenance_type: MaintenanceType | None = None
    service_start_date: date | None = None
    service_end_date: date | None = None
    odometer_km: int | None = Field(default=None, ge=0, le=2147483647, strict=True)
    odometer_missing_reason: Text | None = None
    odometer_regression_reason: Text | None = None
    garage_id: uuid.UUID | None = None
    pm_plan_id: uuid.UUID | None = None
    failure_description: Text | None = None
    invoice_no: Text | None = None
    vat_amount: Money | None = None
    notes: Text | None = None
    lines: list[LineInput] | None = Field(default=None, max_length=200)
    subtotal: object = Field(default=None, exclude=True)
    total_amount: object = Field(default=None, exclude=True)
    amount: object = Field(default=None, exclude=True)

    @field_validator("maintenance_type", "vat_amount", "lines")
    @classmethod
    def not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Explicit null is not allowed")
        return value


class RecordPatch(RecordChanges):
    version: int = Field(ge=1, strict=True)


class TransitionInput(Input):
    version: int = Field(ge=1, strict=True)


class CancelInput(TransitionInput):
    reason: Text


class CorrectionInput(TransitionInput):
    # Domain rule supplies CORRECTION_REASON_REQUIRED, including missing reason.
    reason: str | None = Field(default=None, max_length=4000)
    changes: RecordChanges


class LineView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    line_no: int
    category_id: uuid.UUID
    line_type: str
    description: str
    qty: Quantity
    unit_price: Money
    amount: Money
    is_warranty: bool
    note: str | None


class AttachmentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    file_name: str
    content_type: Literal["pdf", "jpeg", "png"]
    size_bytes: int
    sha256: str


class RecordView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    record_no: str
    vehicle_id: uuid.UUID
    plate_no_snapshot: str
    province_snapshot: str
    maintenance_type: MaintenanceType
    status: RecordStatus
    service_start_date: date | None
    service_end_date: date | None
    odometer_km: int | None
    odometer_missing_reason: str | None
    odometer_regression_reason: str | None
    garage_id: uuid.UUID | None
    garage_name: str | None = None
    pm_plan_id: uuid.UUID | None
    failure_description: str | None
    invoice_no: str | None
    subtotal: Money
    vat_amount: Money
    total_amount: Money
    notes: str | None
    cancel_reason: str | None
    version: int
    lines: list[LineView] = Field(default_factory=list)
    attachments: list[AttachmentView] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
