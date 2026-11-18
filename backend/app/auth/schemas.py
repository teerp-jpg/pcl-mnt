"""Explicit public user fields prevent password/hash leakage through responses."""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

type Role = Literal["viewer", "clerk", "manager", "admin"]


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    parts = email.split("@")
    if (
        len(email) > 254
        or len(parts) != 2
        or not all(parts)
        or any(character.isspace() or ord(character) < 32 for character in email)
    ):
        raise ValueError("Invalid email")
    return email


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str
    role: Role
    is_active: bool
    version: int


class UserResponse(BaseModel):
    user: UserView


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=1024)


class CreateUserInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    email: str
    display_name: str = Field(min_length=1, max_length=200)
    role: Role
    password: SecretStr = Field(min_length=12, max_length=1024)

    @field_validator("email")
    @classmethod
    def email_valid(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("display_name")
    @classmethod
    def name_valid(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Display name is required")
        return value.strip()


class UpdateUserInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    is_active: bool | None = Field(default=None, strict=True)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    version: int = Field(ge=1, strict=True)

    @field_validator("role", "is_active", "display_name")
    @classmethod
    def not_null(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError("Explicit null/blank is not allowed")
        return value.strip() if isinstance(value, str) else value


class ResetPasswordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_password: SecretStr = Field(min_length=12, max_length=1024)
