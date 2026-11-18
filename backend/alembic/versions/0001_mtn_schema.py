"""Initial MTN schema, frozen independently of application model imports.

Revision ID: 0001_mtn_schema
Revises: None
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_mtn_schema"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(sa.schema.CreateSequence(sa.Sequence("maintenance_record_no_seq")))
    # Allocate once, use Bangkok's month, and never truncate numbers above 99999.
    op.execute("""
        CREATE FUNCTION mtn_next_record_no() RETURNS text
        LANGUAGE plpgsql VOLATILE AS $$
        DECLARE number bigint := nextval('maintenance_record_no_seq');
        BEGIN
            RETURN 'MTN-' || to_char(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Bangkok', 'YYYYMM')
                || '-' || lpad(number::text, greatest(5, length(number::text)), '0');
        END;
        $$
    """)
    op.create_table(
        "app_user",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("email", sa.Text, nullable=False, unique=True),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint("role IN ('viewer','clerk','manager','admin')", name="ck_app_user_role"),
        sa.CheckConstraint("email = lower(email) AND btrim(email) <> ''", name="ck_app_user_email"),
        sa.CheckConstraint("version >= 1", name="ck_app_user_version"),
    )
    op.create_table(
        "vehicle",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("core_asset_id", sa.Text, unique=True, nullable=True),
        sa.Column("plate_no", sa.Text, nullable=False),
        sa.Column("plate_no_norm", sa.Text, nullable=False),
        sa.Column("province", sa.Text, nullable=False),
        sa.Column("vehicle_type", sa.Text, nullable=True),
        sa.Column("brand", sa.Text, nullable=True),
        sa.Column("model", sa.Text, nullable=True),
        sa.Column("year", sa.SmallInteger, nullable=True),
        sa.Column("chassis_no", sa.Text, unique=True, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'ACTIVE'")),
        sa.Column("disposed_at", sa.Date, nullable=True),
        sa.Column("origin", sa.Text, nullable=False),
        sa.Column("core_sync_status", sa.Text, nullable=False),
        sa.Column("last_imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint("status IN ('ACTIVE','INACTIVE','DISPOSED')", name="ck_vehicle_status"),
        sa.CheckConstraint("origin IN ('CORE_IMPORT','MTN')", name="ck_vehicle_origin"),
        sa.CheckConstraint(
            "core_sync_status IN ('SYNCED','PENDING_CREATE','PENDING_UPDATE','EXPORTED')",
            name="ck_vehicle_core_sync_status",
        ),
        sa.CheckConstraint(
            "status <> 'DISPOSED' OR disposed_at IS NOT NULL", name="ck_vehicle_disposed_at"
        ),
        sa.CheckConstraint("version >= 1", name="ck_vehicle_version"),
    )
    op.create_table(
        "garage",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("name_norm", sa.Text, nullable=False, unique=True),
        sa.Column("tax_id", sa.Text, unique=True, nullable=True),
        sa.Column("branch_name", sa.Text, nullable=True),
        sa.Column("address", sa.Text, nullable=True),
        sa.Column("phone", sa.Text, nullable=True),
        sa.Column("contact_name", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint("tax_id IS NULL OR tax_id ~ '^[0-9]{13}$'", name="ck_garage_tax_id"),
        sa.CheckConstraint("version >= 1", name="ck_garage_version"),
    )
    op.create_table(
        "maintenance_category",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("code", sa.Text, nullable=False, unique=True),
        sa.Column("name_th", sa.Text, nullable=False),
        sa.Column("parent_id", sa.Uuid, sa.ForeignKey("maintenance_category.id"), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id", name="ck_category_not_self_parent"
        ),
    )
    op.create_table(
        "core_export_batch",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("file_name", sa.Text, nullable=False),
        sa.Column("row_count", sa.Integer, nullable=False),
        sa.Column("exported_by", sa.Uuid, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column(
            "exported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("row_count >= 0", name="ck_export_row_count"),
    )
    op.create_table(
        "vehicle_plate_history",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("vehicle_id", sa.Uuid, sa.ForeignKey("vehicle.id"), nullable=False),
        sa.Column("plate_no", sa.Text, nullable=False),
        sa.Column("plate_no_norm", sa.Text, nullable=False),
        sa.Column("province", sa.Text, nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.CheckConstraint("source IN ('CORE_IMPORT','MTN')", name="ck_plate_history_source"),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="ck_plate_history_dates"
        ),
    )
    op.create_table(
        "vehicle_change",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("vehicle_id", sa.Uuid, sa.ForeignKey("vehicle.id"), nullable=False),
        sa.Column("change_type", sa.Text, nullable=False),
        sa.Column("before_json", postgresql.JSONB, nullable=True),
        sa.Column("after_json", postgresql.JSONB, nullable=False),
        sa.Column("changed_by", sa.Uuid, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column(
            "changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("export_batch_id", sa.Uuid, sa.ForeignKey("core_export_batch.id"), nullable=True),
        sa.CheckConstraint("change_type IN ('CREATE','UPDATE')", name="ck_vehicle_change_type"),
    )
    op.create_table(
        "vehicle_import_batch",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("file_name", sa.Text, nullable=False),
        sa.Column("file_sha256", sa.Text, nullable=False),
        sa.Column("row_count", sa.Integer, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'VALIDATED'")),
        sa.Column("summary_json", postgresql.JSONB, nullable=False),
        sa.Column("uploaded_by", sa.Uuid, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column(
            "uploaded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("applied_by", sa.Uuid, sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('VALIDATED','APPLIED','DISCARDED')", name="ck_import_batch_status"
        ),
        sa.CheckConstraint("row_count BETWEEN 0 AND 10000", name="ck_import_batch_row_count"),
        sa.CheckConstraint("file_sha256 ~ '^[0-9a-f]{64}$'", name="ck_import_batch_sha256"),
    )
    op.create_table(
        "vehicle_import_row",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("batch_id", sa.Uuid, sa.ForeignKey("vehicle_import_batch.id"), nullable=False),
        sa.Column("row_no", sa.Integer, nullable=False),
        sa.Column("raw_json", postgresql.JSONB, nullable=False),
        sa.Column("result", sa.Text, nullable=False),
        sa.Column("matched_vehicle_id", sa.Uuid, sa.ForeignKey("vehicle.id"), nullable=True),
        sa.Column("messages_json", postgresql.JSONB, nullable=False),
        sa.Column("resolution", sa.Text, nullable=True),
        sa.UniqueConstraint("batch_id", "row_no", name="uq_import_row_batch_number"),
        sa.CheckConstraint("row_no > 0", name="ck_import_row_number"),
        sa.CheckConstraint(
            "result IN ('NEW','UPDATE','UNCHANGED','CONFLICT','ERROR')", name="ck_import_row_result"
        ),
        sa.CheckConstraint(
            "resolution IS NULL OR resolution IN ('USE_CORE','KEEP_MNT','LINK')",
            name="ck_import_row_resolution",
        ),
    )
    op.create_table(
        "pm_plan",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("vehicle_id", sa.Uuid, sa.ForeignKey("vehicle.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("category_id", sa.Uuid, sa.ForeignKey("maintenance_category.id"), nullable=True),
        sa.Column("interval_km", sa.Integer, nullable=True),
        sa.Column("interval_days", sa.Integer, nullable=True),
        sa.Column("interval_engine_hours", sa.Numeric(12, 2), nullable=True),
        sa.Column("last_done_at", sa.Date, nullable=True),
        sa.Column("last_done_km", sa.Integer, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.UniqueConstraint("id", "vehicle_id", name="uq_pm_plan_id_vehicle"),
        sa.CheckConstraint(
            "interval_km IS NOT NULL OR interval_days IS NOT NULL "
            "OR interval_engine_hours IS NOT NULL",
            name="ck_pm_plan_interval_required",
        ),
        sa.CheckConstraint(
            "(interval_km IS NULL OR interval_km > 0) AND "
            "(interval_days IS NULL OR interval_days > 0) AND "
            "(interval_engine_hours IS NULL OR interval_engine_hours > 0)",
            name="ck_pm_plan_intervals_positive",
        ),
        sa.CheckConstraint("last_done_km IS NULL OR last_done_km >= 0", name="ck_pm_plan_last_km"),
        sa.CheckConstraint("version >= 1", name="ck_pm_plan_version"),
    )
    op.create_table(
        "maintenance_record",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column(
            "record_no",
            sa.Text,
            nullable=False,
            unique=True,
            server_default=sa.text("mtn_next_record_no()"),
        ),
        sa.Column("vehicle_id", sa.Uuid, sa.ForeignKey("vehicle.id"), nullable=False),
        sa.Column("plate_no_snapshot", sa.Text, nullable=False),
        sa.Column("province_snapshot", sa.Text, nullable=False),
        sa.Column("maintenance_type", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'DRAFT'")),
        sa.Column("service_start_date", sa.Date, nullable=True),
        sa.Column("service_end_date", sa.Date, nullable=True),
        sa.Column("odometer_km", sa.Integer, nullable=True),
        sa.Column("odometer_missing_reason", sa.Text, nullable=True),
        sa.Column("odometer_regression_reason", sa.Text, nullable=True),
        sa.Column("garage_id", sa.Uuid, sa.ForeignKey("garage.id"), nullable=True),
        sa.Column("pm_plan_id", sa.Uuid, nullable=True),
        sa.Column("failure_description", sa.Text, nullable=True),
        sa.Column("invoice_no", sa.Text, nullable=True),
        sa.Column("subtotal", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("vat_amount", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("cancel_reason", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint("maintenance_type IN ('PM','CM')", name="ck_record_type"),
        sa.CheckConstraint(
            "status IN ('DRAFT','IN_PROGRESS','COMPLETED','CANCELLED')", name="ck_record_status"
        ),
        sa.CheckConstraint(
            "service_end_date IS NULL OR "
            "(service_start_date IS NOT NULL AND service_end_date >= service_start_date)",
            name="ck_record_dates",
        ),
        sa.CheckConstraint("odometer_km IS NULL OR odometer_km >= 0", name="ck_record_odometer"),
        sa.CheckConstraint(
            "subtotal >= 0 AND vat_amount >= 0 AND total_amount = subtotal + vat_amount",
            name="ck_record_totals",
        ),
        sa.CheckConstraint("version >= 1", name="ck_record_version"),
        sa.ForeignKeyConstraint(
            ["pm_plan_id", "vehicle_id"],
            ["pm_plan.id", "pm_plan.vehicle_id"],
            name="fk_record_pm_plan_vehicle",
        ),
    )
    op.create_table(
        "maintenance_line",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column(
            "record_id",
            sa.Uuid,
            sa.ForeignKey("maintenance_record.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_no", sa.Integer, nullable=False),
        sa.Column("category_id", sa.Uuid, sa.ForeignKey("maintenance_category.id"), nullable=False),
        sa.Column("line_type", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("qty", sa.Numeric(10, 2), nullable=False),
        sa.Column("unit_price", sa.Numeric(14, 2), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("is_warranty", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("note", sa.Text, nullable=True),
        sa.UniqueConstraint("record_id", "line_no", name="uq_line_record_number"),
        sa.CheckConstraint("line_no > 0", name="ck_line_number"),
        sa.CheckConstraint("line_type IN ('PART','LABOUR','OTHER')", name="ck_line_type"),
        sa.CheckConstraint("qty > 0 AND unit_price >= 0 AND amount >= 0", name="ck_line_amounts"),
    )
    op.create_table(
        "maintenance_attachment",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("record_id", sa.Uuid, sa.ForeignKey("maintenance_record.id"), nullable=False),
        sa.Column("file_name", sa.Text, nullable=False),
        sa.Column("content_type", sa.Text, nullable=False),
        sa.Column("storage_key", sa.Text, nullable=False, unique=True),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("uploaded_by", sa.Uuid, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column(
            "uploaded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "content_type IN ('pdf','jpeg','png')", name="ck_attachment_content_type"
        ),
        sa.CheckConstraint("size_bytes BETWEEN 0 AND 10485760", name="ck_attachment_size"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_attachment_sha256"),
    )
    op.create_table(
        "odometer_reading",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("vehicle_id", sa.Uuid, sa.ForeignKey("vehicle.id"), nullable=False),
        sa.Column("reading_km", sa.Integer, nullable=False),
        sa.Column("engine_hours", sa.Numeric(12, 2), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("source_ref", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("vehicle_id", "source", "source_ref", name="uq_odometer_source_ref"),
        sa.CheckConstraint("source IN ('MANUAL','MAINTENANCE','GPS')", name="ck_odometer_source"),
        sa.CheckConstraint("reading_km >= 0", name="ck_odometer_reading"),
        sa.CheckConstraint("engine_hours IS NULL OR engine_hours >= 0", name="ck_odometer_hours"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), nullable=False, primary_key=True),
        sa.Column("entity", sa.Text, nullable=False),
        sa.Column("entity_id", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("before_json", postgresql.JSONB, nullable=True),
        sa.Column("after_json", postgresql.JSONB, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("actor", sa.Uuid, sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "system_setting",
        sa.Column("key", sa.Text, primary_key=True, nullable=False),
        sa.Column("value_json", postgresql.JSONB, nullable=False),
    )
    op.create_index(
        "uq_vehicle_live_plate",
        "vehicle",
        ["plate_no_norm", "province"],
        unique=True,
        postgresql_where=sa.text("status <> 'DISPOSED'"),
    )
    op.create_index(
        "ix_plate_history_search", "vehicle_plate_history", ["plate_no_norm", "province"]
    )
    op.create_index("ix_plate_history_vehicle", "vehicle_plate_history", ["vehicle_id"])
    op.create_index("ix_vehicle_change_vehicle", "vehicle_change", ["vehicle_id"])
    op.create_index("ix_import_batch_sha256", "vehicle_import_batch", ["file_sha256"])
    op.create_index("ix_pm_plan_vehicle", "pm_plan", ["vehicle_id"])
    op.create_index(
        "ix_record_vehicle_service_date",
        "maintenance_record",
        ["vehicle_id", sa.text("service_start_date DESC")],
    )
    op.create_index("ix_record_status", "maintenance_record", ["status"])
    op.create_index("ix_record_garage", "maintenance_record", ["garage_id"])
    op.create_index(
        "ix_record_type_end_date", "maintenance_record", ["maintenance_type", "service_end_date"]
    )
    op.create_index("ix_attachment_record", "maintenance_attachment", ["record_id"])
    op.create_index(
        "ix_odometer_vehicle_recorded", "odometer_reading", ["vehicle_id", "recorded_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_odometer_vehicle_recorded", table_name="odometer_reading")
    op.drop_index("ix_attachment_record", table_name="maintenance_attachment")
    op.drop_index("ix_record_type_end_date", table_name="maintenance_record")
    op.drop_index("ix_record_garage", table_name="maintenance_record")
    op.drop_index("ix_record_status", table_name="maintenance_record")
    op.drop_index("ix_record_vehicle_service_date", table_name="maintenance_record")
    op.drop_index("ix_pm_plan_vehicle", table_name="pm_plan")
    op.drop_index("ix_import_batch_sha256", table_name="vehicle_import_batch")
    op.drop_index("ix_vehicle_change_vehicle", table_name="vehicle_change")
    op.drop_index("ix_plate_history_vehicle", table_name="vehicle_plate_history")
    op.drop_index("ix_plate_history_search", table_name="vehicle_plate_history")
    op.drop_index("uq_vehicle_live_plate", table_name="vehicle")
    op.drop_table("system_setting")
    op.drop_table("audit_log")
    op.drop_table("odometer_reading")
    op.drop_table("maintenance_attachment")
    op.drop_table("maintenance_line")
    op.drop_table("maintenance_record")
    op.drop_table("pm_plan")
    op.drop_table("vehicle_import_row")
    op.drop_table("vehicle_import_batch")
    op.drop_table("vehicle_change")
    op.drop_table("vehicle_plate_history")
    op.drop_table("core_export_batch")
    op.drop_table("maintenance_category")
    op.drop_table("garage")
    op.drop_table("vehicle")
    op.drop_table("app_user")
    op.execute("DROP FUNCTION mtn_next_record_no()")
    op.execute(sa.schema.DropSequence(sa.Sequence("maintenance_record_no_seq")))
