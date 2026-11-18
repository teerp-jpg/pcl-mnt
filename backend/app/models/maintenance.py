"""Maintenance history and PM scheduling; workflow services arrive in T7/T8."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Sequence,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.common import Tracked, UUIDPrimaryKey, Versioned

# One global sequence avoids racing MAX()+1. Gaps on rollback are intentional.
record_number_sequence = Sequence("maintenance_record_no_seq", metadata=Base.metadata)


class PMPlan(UUIDPrimaryKey, Tracked, Versioned, Base):
    __tablename__ = "pm_plan"
    __table_args__ = (
        UniqueConstraint("id", "vehicle_id", name="uq_pm_plan_id_vehicle"),
        CheckConstraint(
            "interval_km IS NOT NULL OR interval_days IS NOT NULL "
            "OR interval_engine_hours IS NOT NULL",
            name="ck_pm_plan_interval_required",
        ),
        CheckConstraint(
            "(interval_km IS NULL OR interval_km > 0) AND "
            "(interval_days IS NULL OR interval_days > 0) AND "
            "(interval_engine_hours IS NULL OR interval_engine_hours > 0)",
            name="ck_pm_plan_intervals_positive",
        ),
        CheckConstraint("last_done_km IS NULL OR last_done_km >= 0", name="ck_pm_plan_last_km"),
        CheckConstraint("version >= 1", name="ck_pm_plan_version"),
        Index("ix_pm_plan_vehicle", "vehicle_id"),
    )

    vehicle_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("vehicle.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("maintenance_category.id")
    )
    interval_km: Mapped[int | None] = mapped_column(Integer)
    interval_days: Mapped[int | None] = mapped_column(Integer)
    interval_engine_hours: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    last_done_at: Mapped[date | None] = mapped_column(Date)
    last_done_km: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class MaintenanceRecord(UUIDPrimaryKey, Tracked, Versioned, Base):
    __tablename__ = "maintenance_record"
    __table_args__ = (
        CheckConstraint("maintenance_type IN ('PM','CM')", name="ck_record_type"),
        CheckConstraint(
            "status IN ('DRAFT','IN_PROGRESS','COMPLETED','CANCELLED')", name="ck_record_status"
        ),
        CheckConstraint(
            "service_end_date IS NULL OR "
            "(service_start_date IS NOT NULL AND service_end_date >= service_start_date)",
            name="ck_record_dates",
        ),
        CheckConstraint("odometer_km IS NULL OR odometer_km >= 0", name="ck_record_odometer"),
        CheckConstraint(
            "subtotal >= 0 AND vat_amount >= 0 AND total_amount = subtotal + vat_amount",
            name="ck_record_totals",
        ),
        CheckConstraint("version >= 1", name="ck_record_version"),
        ForeignKeyConstraint(
            ["pm_plan_id", "vehicle_id"],
            ["pm_plan.id", "pm_plan.vehicle_id"],
            name="fk_record_pm_plan_vehicle",
        ),
        Index("ix_record_vehicle_service_date", "vehicle_id", text("service_start_date DESC")),
        Index("ix_record_status", "status"),
        Index("ix_record_garage", "garage_id"),
        Index("ix_record_type_end_date", "maintenance_type", "service_end_date"),
    )

    record_no: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        unique=True,
        server_default=text("mtn_next_record_no()"),
    )
    vehicle_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("vehicle.id"), nullable=False)
    plate_no_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    province_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    maintenance_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'DRAFT'"))
    # Drafts can be incomplete; T7 enforces required fields at each transition.
    service_start_date: Mapped[date | None] = mapped_column(Date)
    service_end_date: Mapped[date | None] = mapped_column(Date)
    odometer_km: Mapped[int | None] = mapped_column(Integer)
    odometer_missing_reason: Mapped[str | None] = mapped_column(Text)
    odometer_regression_reason: Mapped[str | None] = mapped_column(Text)
    garage_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("garage.id"))
    pm_plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    failure_description: Mapped[str | None] = mapped_column(Text)
    invoice_no: Mapped[str | None] = mapped_column(Text)
    subtotal: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )
    vat_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default=text("0")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)


class MaintenanceLine(UUIDPrimaryKey, Base):
    __tablename__ = "maintenance_line"
    __table_args__ = (
        UniqueConstraint("record_id", "line_no", name="uq_line_record_number"),
        CheckConstraint("line_no > 0", name="ck_line_number"),
        CheckConstraint("line_type IN ('PART','LABOUR','OTHER')", name="ck_line_type"),
        CheckConstraint("qty > 0 AND unit_price >= 0 AND amount >= 0", name="ck_line_amounts"),
    )

    record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("maintenance_record.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("maintenance_category.id"), nullable=False
    )
    line_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    is_warranty: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    note: Mapped[str | None] = mapped_column(Text)


class MaintenanceAttachment(UUIDPrimaryKey, Base):
    __tablename__ = "maintenance_attachment"
    __table_args__ = (
        CheckConstraint("content_type IN ('pdf','jpeg','png')", name="ck_attachment_content_type"),
        CheckConstraint("size_bytes BETWEEN 0 AND 10485760", name="ck_attachment_size"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_attachment_sha256"),
        Index("ix_attachment_record", "record_id"),
    )

    record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("maintenance_record.id"), nullable=False
    )
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id"), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OdometerReading(UUIDPrimaryKey, Base):
    __tablename__ = "odometer_reading"
    __table_args__ = (
        UniqueConstraint("vehicle_id", "source", "source_ref", name="uq_odometer_source_ref"),
        CheckConstraint("source IN ('MANUAL','MAINTENANCE','GPS')", name="ck_odometer_source"),
        CheckConstraint("reading_km >= 0", name="ck_odometer_reading"),
        CheckConstraint("engine_hours IS NULL OR engine_hours >= 0", name="ck_odometer_hours"),
        Index("ix_odometer_vehicle_recorded", "vehicle_id", "recorded_at"),
    )

    vehicle_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("vehicle.id"), nullable=False)
    reading_km: Mapped[int] = mapped_column(Integer, nullable=False)
    engine_hours: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
