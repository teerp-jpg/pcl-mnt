"""MTN-owned garages and maintenance categories."""

import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import Tracked, UUIDPrimaryKey, Versioned


class Garage(UUIDPrimaryKey, Tracked, Versioned, Base):
    __tablename__ = "garage"
    __table_args__ = (
        CheckConstraint("tax_id IS NULL OR tax_id ~ '^[0-9]{13}$'", name="ck_garage_tax_id"),
        CheckConstraint("version >= 1", name="ck_garage_version"),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_norm: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    tax_id: Mapped[str | None] = mapped_column(Text, unique=True)
    branch_name: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    contact_name: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class MaintenanceCategory(UUIDPrimaryKey, Tracked, Base):
    __tablename__ = "maintenance_category"
    __table_args__ = (
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_category_not_self_parent"),
    )

    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name_th: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("maintenance_category.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
