"""Shared column definitions; services own actor attribution and transactions."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class Tracked:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    # Nullable for the initial synthetic administrator, before an actor exists.
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("app_user.id"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("app_user.id"))


class Versioned:
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
