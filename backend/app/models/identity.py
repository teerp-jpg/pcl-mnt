"""Local sandbox users, audit events and application settings."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import Tracked, UUIDPrimaryKey, Versioned


class AppUser(UUIDPrimaryKey, Tracked, Versioned, Base):
    __tablename__ = "app_user"
    __table_args__ = (
        CheckConstraint("role IN ('viewer','clerk','manager','admin')", name="ck_app_user_role"),
        CheckConstraint("email = lower(email) AND btrim(email) <> ''", name="ck_app_user_email"),
        CheckConstraint("version >= 1", name="ck_app_user_version"),
    )

    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class AuditLog(UUIDPrimaryKey, Base):
    __tablename__ = "audit_log"

    entity: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    before_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    after_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id"), nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SystemSetting(Base):
    __tablename__ = "system_setting"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value_json: Mapped[object] = mapped_column(JSONB, nullable=False)
