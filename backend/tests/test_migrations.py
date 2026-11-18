"""Schema contracts against PostgreSQL, confined to a new rollback-only schema."""

import io
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, DateTime, Enum, Numeric, Text, inspect, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, configure_mappers
from sqlalchemy.schema import CreateSchema

from app.db import Base
from app.models import (
    AppUser,
    Garage,
    MaintenanceAttachment,
    MaintenanceCategory,
    MaintenanceLine,
    MaintenanceRecord,
    OdometerReading,
    PMPlan,
    Vehicle,
    VehicleImportBatch,
    VehicleImportRow,
)

EXPECTED_TABLES = {
    "app_user",
    "audit_log",
    "core_export_batch",
    "garage",
    "maintenance_attachment",
    "maintenance_category",
    "maintenance_line",
    "maintenance_record",
    "odometer_reading",
    "pm_plan",
    "system_setting",
    "vehicle",
    "vehicle_change",
    "vehicle_import_batch",
    "vehicle_import_row",
    "vehicle_plate_history",
}


def alembic_config(connection: Connection | None = None) -> Config:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def test_models_have_specified_tables_types_and_no_native_enums() -> None:
    configure_mappers()
    assert set(Base.metadata.tables) == EXPECTED_TABLES
    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert not isinstance(column.type, Enum)
            if isinstance(column.type, DateTime):
                assert column.type.timezone
            if column.name in {"status", "core_sync_status", "role", "maintenance_type"}:
                assert isinstance(column.type, Text)
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint):
                assert "APPROV" not in str(constraint.sqltext).upper()
    for table_name, columns in {
        "maintenance_record": ("subtotal", "vat_amount", "total_amount"),
        "maintenance_line": ("unit_price", "amount"),
    }.items():
        for name in columns:
            column_type = Base.metadata.tables[table_name].c[name].type
            assert isinstance(column_type, Numeric)
            assert (column_type.precision, column_type.scale) == (14, 2)
            assert column_type.asdecimal


def test_alembic_renders_upgrade_and_downgrade_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unit@unused.invalid/mtn")
    config = alembic_config()
    output = io.StringIO()
    config.output_buffer = output
    command.upgrade(config, "head", sql=True)
    upgrade_sql = output.getvalue()
    assert "CREATE SEQUENCE maintenance_record_no_seq" in upgrade_sql
    assert "CREATE FUNCTION mtn_next_record_no()" in upgrade_sql
    assert "CREATE UNIQUE INDEX uq_vehicle_live_plate" in upgrade_sql
    assert "CREATE TYPE" not in upgrade_sql
    for name in EXPECTED_TABLES:
        assert f"CREATE TABLE {name} (" in upgrade_sql
    output.seek(0)
    output.truncate()
    command.downgrade(config, "0001_mtn_schema:base", sql=True)
    downgrade_sql = output.getvalue()
    assert "DROP FUNCTION mtn_next_record_no()" in downgrade_sql
    assert "DROP SEQUENCE maintenance_record_no_seq" in downgrade_sql
    for name in EXPECTED_TABLES:
        assert f"DROP TABLE {name};" in downgrade_sql


@pytest.fixture
def schema_connection(db_session: Session) -> Iterator[Connection]:
    # The controller supplies a test DB with SANDBOX_SEED=false (conftest).
    # DDL stays inside its outer transaction. Never downgrade the public schema.
    connection = db_session.connection()
    assert connection.dialect.name == "postgresql"
    schema_name = "mtn_t2_" + uuid.uuid4().hex
    connection.execute(CreateSchema(schema_name))
    quoted_schema = connection.dialect.identifier_preparer.quote(schema_name)
    connection.execute(text(f"SET LOCAL search_path TO {quoted_schema}"))
    assert connection.execute(text("SELECT current_schema()")).scalar_one() == schema_name
    command.upgrade(alembic_config(connection), "head")
    yield connection
    # conftest rolls back the new schema and all synthetic rows; no DROP SCHEMA.


@pytest.fixture
def schema_session(schema_connection: Connection) -> Iterator[Session]:
    with Session(bind=schema_connection, join_transaction_mode="create_savepoint") as session:
        yield session


@dataclass
class Sample:
    user: AppUser
    vehicle: Vehicle
    category: MaintenanceCategory
    garage: Garage
    plan: PMPlan
    record: MaintenanceRecord


def new_vehicle(**overrides: object) -> Vehicle:
    values: dict[str, object] = {
        "plate_no": "TEST-" + uuid.uuid4().hex[:10],
        "plate_no_norm": uuid.uuid4().hex,
        "province": "ข้อมูลสมมติ",
        "origin": "MTN",
        "core_sync_status": "PENDING_CREATE",
    }
    values.update(overrides)
    return Vehicle(**values)


def new_record(vehicle: Vehicle, **overrides: object) -> MaintenanceRecord:
    values: dict[str, object] = {
        "vehicle_id": vehicle.id,
        "plate_no_snapshot": vehicle.plate_no,
        "province_snapshot": vehicle.province,
        "maintenance_type": "PM",
    }
    values.update(overrides)
    return MaintenanceRecord(**values)


@pytest.fixture
def sample(schema_session: Session) -> Sample:
    user = AppUser(
        email="schema-test@mtn.local",
        display_name="ข้อมูลทดสอบ schema เท่านั้น",
        role="admin",
        password_hash="unusable-synthetic-schema-test-hash",
    )
    schema_session.add(user)
    schema_session.flush()
    vehicle = new_vehicle(created_by=user.id, updated_by=user.id)
    category = MaintenanceCategory(code="TEST", name_th="หมวดสมมติ")
    garage = Garage(name="อู่สมมติ", name_norm="อู่สมมติ")
    schema_session.add_all([vehicle, category, garage])
    schema_session.flush()
    plan = PMPlan(vehicle_id=vehicle.id, name="แผนสมมติ", interval_days=180)
    schema_session.add(plan)
    schema_session.flush()
    record = new_record(vehicle, pm_plan_id=plan.id, garage_id=garage.id)
    schema_session.add(record)
    schema_session.flush()
    return Sample(user, vehicle, category, garage, plan, record)


@pytest.mark.postgresql
def test_upgrade_matches_models_and_is_repeatable(schema_connection: Connection) -> None:
    assert set(inspect(schema_connection).get_table_names()) == EXPECTED_TABLES | {
        "alembic_version"
    }
    assert schema_connection.execute(
        text("SELECT version_num FROM alembic_version")
    ).scalar_one() == ("0001_mtn_schema")
    command.upgrade(alembic_config(schema_connection), "head")
    context = MigrationContext.configure(schema_connection, opts={"compare_type": True})
    assert compare_metadata(context, Base.metadata) == []
    own_schema = schema_connection.execute(text("SELECT current_schema()")).scalar_one()
    assert inspect(schema_connection).get_enums(schema=own_schema) == []


@pytest.mark.postgresql
def test_upgrade_downgrade_round_trip_only_in_new_schema(schema_connection: Connection) -> None:
    config = alembic_config(schema_connection)
    own_schema = schema_connection.execute(text("SELECT current_schema()")).scalar_one()
    assert own_schema.startswith("mtn_t2_")
    command.downgrade(config, "base")
    assert inspect(schema_connection).get_table_names() == ["alembic_version"]
    assert inspect(schema_connection).get_sequence_names() == []
    assert (
        schema_connection.execute(
            text("SELECT to_regprocedure('mtn_next_record_no()')")
        ).scalar_one()
        is None
    )
    command.upgrade(config, "head")
    assert set(inspect(schema_connection).get_table_names()) == EXPECTED_TABLES | {
        "alembic_version"
    }


@pytest.mark.postgresql
@pytest.mark.parametrize("status", ["ACTIVE", "INACTIVE"])
def test_live_plate_uniqueness_includes_inactive(
    schema_session: Session, sample: Sample, status: str
) -> None:
    with pytest.raises(IntegrityError) as error, schema_session.begin_nested():
        schema_session.add(
            new_vehicle(
                plate_no_norm=sample.vehicle.plate_no_norm,
                province=sample.vehicle.province,
                status=status,
            )
        )
        schema_session.flush()
    assert error.value.orig.diag.constraint_name == "uq_vehicle_live_plate"
    schema_session.add(
        new_vehicle(
            plate_no_norm=sample.vehicle.plate_no_norm,
            province=sample.vehicle.province,
            status="DISPOSED",
            disposed_at=date(2026, 1, 1),
        )
    )
    schema_session.flush()


@pytest.mark.postgresql
@pytest.mark.parametrize("field", ["core_asset_id", "chassis_no"])
def test_optional_vehicle_identifiers_are_unique_when_present(
    schema_session: Session, sample: Sample, field: str
) -> None:
    setattr(sample.vehicle, field, "SYNTHETIC-ID")
    schema_session.flush()
    with pytest.raises(IntegrityError) as error, schema_session.begin_nested():
        schema_session.add(new_vehicle(**{field: "SYNTHETIC-ID"}))
        schema_session.flush()
    assert error.value.orig.sqlstate == "23505"
    # The fixture and all other vehicles can still have NULL identifiers.
    schema_session.add_all([new_vehicle(), new_vehicle()])
    schema_session.flush()


@pytest.mark.postgresql
@pytest.mark.parametrize(
    ("table_name", "changes", "constraint"),
    [
        ("vehicle", {"status": "UNKNOWN"}, "ck_vehicle_status"),
        ("vehicle", {"origin": "MNT"}, "ck_vehicle_origin"),
        ("vehicle", {"core_sync_status": "UNKNOWN"}, "ck_vehicle_core_sync_status"),
        ("vehicle", {"status": "DISPOSED"}, "ck_vehicle_disposed_at"),
        ("vehicle", {"version": 0}, "ck_vehicle_version"),
        ("app_user", {"role": "owner"}, "ck_app_user_role"),
        ("app_user", {"email": "UPPER@mtn.local"}, "ck_app_user_email"),
        ("maintenance_record", {"status": "PENDING_APPROVAL"}, "ck_record_status"),
        ("maintenance_record", {"maintenance_type": "OTHER"}, "ck_record_type"),
        ("maintenance_record", {"version": 0}, "ck_record_version"),
        ("maintenance_record", {"odometer_km": -1}, "ck_record_odometer"),
        ("maintenance_record", {"total_amount": Decimal("1")}, "ck_record_totals"),
        (
            "maintenance_record",
            {"service_start_date": date(2026, 2, 2), "service_end_date": date(2026, 2, 1)},
            "ck_record_dates",
        ),
        ("pm_plan", {"interval_days": None}, "ck_pm_plan_interval_required"),
        ("pm_plan", {"interval_days": 0}, "ck_pm_plan_intervals_positive"),
        ("pm_plan", {"interval_days": -1}, "ck_pm_plan_intervals_positive"),
        ("pm_plan", {"version": 0}, "ck_pm_plan_version"),
        ("garage", {"tax_id": "123"}, "ck_garage_tax_id"),
        ("garage", {"tax_id": "ABCDEFGHIJKLM"}, "ck_garage_tax_id"),
        ("garage", {"version": 0}, "ck_garage_version"),
    ],
)
def test_database_rejects_invalid_values(
    schema_session: Session,
    sample: Sample,
    table_name: str,
    changes: dict[str, object],
    constraint: str,
) -> None:
    assert sample.record.id is not None
    with pytest.raises(IntegrityError) as error, schema_session.begin_nested():
        schema_session.execute(update(Base.metadata.tables[table_name]).values(**changes))
    assert error.value.orig.diag.constraint_name == constraint


@pytest.mark.postgresql
def test_record_plan_must_belong_to_same_vehicle(schema_session: Session, sample: Sample) -> None:
    another_vehicle = new_vehicle()
    schema_session.add(another_vehicle)
    schema_session.flush()
    with pytest.raises(IntegrityError) as error, schema_session.begin_nested():
        schema_session.execute(update(MaintenanceRecord).values(vehicle_id=another_vehicle.id))
    assert error.value.orig.diag.constraint_name == "fk_record_pm_plan_vehicle"
    with pytest.raises(IntegrityError) as error, schema_session.begin_nested():
        schema_session.add(new_record(sample.vehicle, vehicle_id=uuid.uuid4()))
        schema_session.flush()
    assert error.value.orig.sqlstate == "23503"


@pytest.mark.postgresql
def test_record_numbers_come_from_sequence_without_truncation(
    schema_session: Session, sample: Sample
) -> None:
    # A session timezone change must not change the specified Bangkok month.
    schema_session.execute(text("SET LOCAL TIME ZONE 'Pacific/Kiritimati'"))
    month = schema_session.execute(
        text("SELECT to_char(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Bangkok', 'YYYYMM')")
    ).scalar_one()
    assert re.fullmatch(rf"MTN-{month}-[0-9]{{5}}", sample.record.record_no)
    second = new_record(sample.vehicle)
    schema_session.add(second)
    schema_session.flush()
    assert int(second.record_no.rsplit("-", 1)[1]) > int(sample.record.record_no.rsplit("-", 1)[1])
    schema_session.execute(text("SELECT setval('maintenance_record_no_seq', 99999, true)"))
    overflow = new_record(sample.vehicle)
    schema_session.add(overflow)
    schema_session.flush()
    assert overflow.record_no == f"MTN-{month}-100000"


@pytest.mark.postgresql
def test_decimal_storage_and_parent_history_references(
    schema_session: Session, sample: Sample
) -> None:
    sample.record.subtotal = Decimal("1500.00")
    sample.record.vat_amount = Decimal("105.00")
    sample.record.total_amount = Decimal("1605.00")
    line = MaintenanceLine(
        record_id=sample.record.id,
        line_no=1,
        category_id=sample.category.id,
        line_type="LABOUR",
        description="รายการสมมติ",
        qty=Decimal("3.00"),
        unit_price=Decimal("500.00"),
        amount=Decimal("1500.00"),
    )
    schema_session.add(line)
    schema_session.flush()
    schema_session.expire_all()
    stored = schema_session.execute(select(MaintenanceRecord)).scalar_one()
    assert stored.total_amount == Decimal("1605.00")
    assert isinstance(stored.total_amount, Decimal)
    assert schema_session.execute(select(MaintenanceLine)).scalar_one().qty == Decimal("3.00")
    with pytest.raises(IntegrityError) as error, schema_session.begin_nested():
        schema_session.execute(Vehicle.__table__.delete().where(Vehicle.id == sample.vehicle.id))
    assert error.value.orig.sqlstate == "23503"
    with pytest.raises(IntegrityError) as error, schema_session.begin_nested():
        schema_session.execute(Garage.__table__.delete().where(Garage.id == sample.garage.id))
    assert error.value.orig.sqlstate == "23503"


@pytest.mark.postgresql
def test_import_and_odometer_idempotency_keys(schema_session: Session, sample: Sample) -> None:
    batch = VehicleImportBatch(
        file_name="synthetic.csv",
        file_sha256="a" * 64,
        row_count=1,
        summary_json={"NEW": 1},
        uploaded_by=sample.user.id,
    )
    schema_session.add(batch)
    schema_session.flush()
    schema_session.add(
        VehicleImportRow(batch_id=batch.id, row_no=1, raw_json={}, result="NEW", messages_json=[])
    )
    schema_session.flush()
    with pytest.raises(IntegrityError) as error, schema_session.begin_nested():
        schema_session.add(
            VehicleImportRow(
                batch_id=batch.id, row_no=1, raw_json={}, result="NEW", messages_json=[]
            )
        )
        schema_session.flush()
    assert error.value.orig.diag.constraint_name == "uq_import_row_batch_number"
    # Repeated file hashes are warnings in T6, so there must be no UNIQUE hash.
    schema_session.add(
        VehicleImportBatch(
            file_name="synthetic-again.csv",
            file_sha256="a" * 64,
            row_count=0,
            summary_json={},
            uploaded_by=sample.user.id,
        )
    )
    schema_session.flush()
    reading = {
        "vehicle_id": sample.vehicle.id,
        "reading_km": 120500,
        "recorded_at": sample.user.created_at,
        "source": "MAINTENANCE",
        "source_ref": str(sample.record.id),
    }
    schema_session.add(OdometerReading(**reading))
    schema_session.flush()
    with pytest.raises(IntegrityError) as error, schema_session.begin_nested():
        schema_session.add(OdometerReading(**reading))
        schema_session.flush()
    assert error.value.orig.diag.constraint_name == "uq_odometer_source_ref"


@pytest.mark.postgresql
@pytest.mark.parametrize("size", [-1, 10485761])
def test_attachment_storage_limits(schema_session: Session, sample: Sample, size: int) -> None:
    with pytest.raises(IntegrityError) as error, schema_session.begin_nested():
        schema_session.add(
            MaintenanceAttachment(
                record_id=sample.record.id,
                file_name="synthetic.pdf",
                content_type="pdf",
                storage_key=str(uuid.uuid4()),
                size_bytes=size,
                sha256="a" * 64,
                uploaded_by=sample.user.id,
            )
        )
        schema_session.flush()
    assert error.value.orig.diag.constraint_name == "ck_attachment_size"
