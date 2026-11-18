"""Validated master-data writes and public snapshots."""

import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

Name = Annotated[str, Field(min_length=1, max_length=200)]
TaxID = Annotated[str, Field(pattern=r"^[0-9]{13}$", min_length=13, max_length=13)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GarageCreate(Input):
    name: Name
    tax_id: TaxID | None = None
    branch_name: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=2000)
    phone: str | None = Field(default=None, max_length=100)
    contact_name: str | None = Field(default=None, max_length=200)
    is_active: bool = Field(default=True, strict=True)


class GaragePatch(Input):
    version: int = Field(ge=1, strict=True)
    name: Name | None = None
    tax_id: TaxID | None = None
    branch_name: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=2000)
    phone: str | None = Field(default=None, max_length=100)
    contact_name: str | None = Field(default=None, max_length=200)
    is_active: bool | None = Field(default=None, strict=True)

    @field_validator("name", "is_active")
    @classmethod
    def required_not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Explicit null is not allowed")
        return value


class GarageView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    tax_id: str | None
    branch_name: str | None
    address: str | None
    phone: str | None
    contact_name: str | None
    is_active: bool
    version: int


class CategoryCreate(Input):
    code: str = Field(min_length=1, max_length=80)
    name_th: Name
    parent_id: uuid.UUID | None = None
    is_active: bool = Field(default=True, strict=True)


class CategoryPatch(Input):
    code: str | None = Field(default=None, min_length=1, max_length=80)
    name_th: Name | None = None
    parent_id: uuid.UUID | None = None
    is_active: bool | None = Field(default=None, strict=True)

    @field_validator("code", "name_th", "is_active")
    @classmethod
    def required_not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Explicit null is not allowed")
        return value


class CategoryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name_th: str
    parent_id: uuid.UUID | None
    is_active: bool


class PMSettings(Input):
    # Percentage may be fractional. Counts must be whole, non-negative values.
    pm_due_soon_pct: float = Field(ge=0, le=100, strict=True, allow_inf_nan=False)
    pm_due_soon_km: int = Field(ge=0, le=2147483647, strict=True)
    pm_due_soon_days: int = Field(ge=0, le=2147483647, strict=True)


DEFAULT_PM_SETTINGS = PMSettings(pm_due_soon_pct=10, pm_due_soon_km=500, pm_due_soon_days=14)
