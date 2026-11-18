"""Vehicle registry, import validation and tracked CORE CSV exchanges."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import Tracked, UUIDPrimaryKey, Versioned


class Vehicle(UUIDPrimaryKey, Tracked, Versioned, Base):
    __tablename__ = "vehicle"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','INACTIVE','DISPOSED')", name="ck_vehicle_status"),
        CheckConstraint("origin IN ('CORE_IMPORT','MTN')", name="ck_vehicle_origin"),
        CheckConstraint(
            "core_sync_status IN ('SYNCED','PENDING_CREATE','PENDING_UPDATE','EXPORTED')",
            name="ck_vehicle_core_sync_status",
        ),
        CheckConstraint(
            "status <> 'DISPOSED' OR disposed_at IS NOT NULL", name="ck_vehicle_disposed_at"
        ),
        CheckConstraint("version >= 1", name="ck_vehicle_version"),
        Index(
            "uq_vehicle_live_plate",
            "plate_no_norm",
            "province",
            unique=True,
            postgresql_where=text("status <> 'DISPOSED'"),
        ),
    )

    core_asset_id: Mapped[str | None] = mapped_column(Text, unique=True)
    plate_no: Mapped[str] = mapped_column(Text, nullable=False)
    plate_no_norm: Mapped[str] = mapped_column(Text, nullable=False)
    province: Mapped[str] = mapped_column(Text, nullable=False)
    vehicle_type: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    year: Mapped[int | None] = mapped_column(SmallInteger)
    chassis_no: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ACTIVE'"))
    disposed_at: Mapped[date | None] = mapped_column(Date)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    core_sync_status: Mapped[str] = mapped_column(Text, nullable=False)
    last_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VehiclePlateHistory(UUIDPrimaryKey, Base):
    __tablename__ = "vehicle_plate_history"
    __table_args__ = (
        CheckConstraint("source IN ('CORE_IMPORT','MTN')", name="ck_plate_history_source"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="ck_plate_history_dates"
        ),
        Index("ix_plate_history_search", "plate_no_norm", "province"),
        Index("ix_plate_history_vehicle", "vehicle_id"),
    )

    vehicle_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("vehicle.id"), nullable=False)
    plate_no: Mapped[str] = mapped_column(Text, nullable=False)
    plate_no_norm: Mapped[str] = mapped_column(Text, nullable=False)
    province: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(Text, nullable=False)


class CoreExportBatch(UUIDPrimaryKey, Base):
    __tablename__ = "core_export_batch"
    __table_args__ = (CheckConstraint("row_count >= 0", name="ck_export_row_count"),)

    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    exported_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id"), nullable=False)
    exported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class VehicleChange(UUIDPrimaryKey, Base):
    __tablename__ = "vehicle_change"
    __table_args__ = (
        CheckConstraint("change_type IN ('CREATE','UPDATE')", name="ck_vehicle_change_type"),
        Index("ix_vehicle_change_vehicle", "vehicle_id"),
    )

    vehicle_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("vehicle.id"), nullable=False)
    change_type: Mapped[str] = mapped_column(Text, nullable=False)
    before_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    after_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    changed_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id"), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    export_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("core_export_batch.id")
    )


class VehicleImportBatch(UUIDPrimaryKey, Base):
    __tablename__ = "vehicle_import_batch"
    __table_args__ = (
        CheckConstraint(
            "status IN ('VALIDATED','APPLIED','DISCARDED')", name="ck_import_batch_status"
        ),
        CheckConstraint("row_count BETWEEN 0 AND 10000", name="ck_import_batch_row_count"),
        CheckConstraint("file_sha256 ~ '^[0-9a-f]{64}$'", name="ck_import_batch_sha256"),
        Index("ix_import_batch_sha256", "file_sha256"),
    )

    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'VALIDATED'"))
    summary_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id"), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    applied_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("app_user.id"))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VehicleImportRow(UUIDPrimaryKey, Base):
    __tablename__ = "vehicle_import_row"
    __table_args__ = (
        UniqueConstraint("batch_id", "row_no", name="uq_import_row_batch_number"),
        CheckConstraint("row_no > 0", name="ck_import_row_number"),
        CheckConstraint(
            "result IN ('NEW','UPDATE','UNCHANGED','CONFLICT','ERROR')", name="ck_import_row_result"
        ),
        CheckConstraint(
            "resolution IS NULL OR resolution IN ('USE_CORE','KEEP_MNT','LINK')",
            name="ck_import_row_resolution",
        ),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("vehicle_import_batch.id"), nullable=False
    )
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    matched_vehicle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("vehicle.id"))
    messages_json: Mapped[list[object]] = mapped_column(JSONB, nullable=False)
    resolution: Mapped[str | None] = mapped_column(Text)
