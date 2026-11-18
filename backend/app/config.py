"""Validated environment configuration with value-free error messages."""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.integrations.core import CORE_COLUMNS


class ConfigurationError(ValueError):
    """Report the setting name, never its potentially secret value."""


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if not value.strip():
        raise ConfigurationError(f"Missing required setting: {name}")
    return value


def _database_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "postgresql+psycopg"
            and bool(parsed.hostname)
            and bool(parsed.path.strip("/"))
            and parsed.fragment == ""
        )
        # Accessing port validates malformed and out-of-range ports too.
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError("Invalid setting: DATABASE_URL") from None
    return value


def _cors_origins(value: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value)
        if not isinstance(decoded, list):
            raise ValueError
        for origin in decoded:
            if not isinstance(origin, str):
                raise ValueError
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or "*" in origin
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError
            _ = parsed.port
    except (ValueError, TypeError):
        raise ConfigurationError("Invalid setting: CORS_ORIGINS") from None
    return tuple(dict.fromkeys(decoded))


def _column_map(value: str) -> dict[str, str]:
    """Map canonical fields to CORE headers, retaining defaults for other fields."""
    try:
        decoded = json.loads(value)
        if not isinstance(decoded, dict) or any(
            key not in CORE_COLUMNS
            or not isinstance(header, str)
            or not header.strip()
            or len(header) > 200
            or "\x00" in header
            for key, header in decoded.items()
        ):
            raise ValueError
        mapping = {column: column for column in CORE_COLUMNS}
        mapping.update({key: header.strip() for key, header in decoded.items()})
        if len(set(mapping.values())) != len(mapping):
            raise ValueError
    except (ValueError, TypeError):
        raise ConfigurationError("Invalid setting: CORE_CSV_COLUMN_MAP") from None
    return mapping


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = field(repr=False)
    app_env: str
    sandbox_seed: bool
    jwt_secret: str = field(repr=False)
    seed_admin_email: str = field(repr=False)
    seed_admin_password: str = field(repr=False)
    upload_root: str
    cors_origins: tuple[str, ...]
    core_csv_column_map: dict[str, str] = field(default_factory=lambda: _column_map("{}"))

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if environment is None else environment
        seed = _required(values, "SANDBOX_SEED")
        if seed not in {"true", "false"}:
            raise ConfigurationError("Invalid setting: SANDBOX_SEED")
        return cls(
            database_url=_database_url(_required(values, "DATABASE_URL")),
            app_env=_required(values, "APP_ENV"),
            sandbox_seed=seed == "true",
            jwt_secret=_required(values, "JWT_SECRET"),
            seed_admin_email=_required(values, "SEED_ADMIN_EMAIL"),
            seed_admin_password=_required(values, "SEED_ADMIN_PASSWORD"),
            upload_root=_required(values, "UPLOAD_ROOT"),
            cors_origins=_cors_origins(_required(values, "CORS_ORIGINS")),
            core_csv_column_map=_column_map(values.get("CORE_CSV_COLUMN_MAP", "{}")),
        )
