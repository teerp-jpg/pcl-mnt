"""Validate first, then apply an unchanged preview and its decisions atomically."""

import hashlib
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import APIError
from app.integrations.core import CORE_COLUMNS
from app.integrations.core_source import CoreAssetRow, CoreAssetSource, parse_asset
from app.models import AppUser, Vehicle, VehicleImportBatch, VehicleImportRow, VehiclePlateHistory
from app.schemas.vehicle_imports import ImportResolutions, ImportRowView, ImportView
from app.schemas.vehicles import normalize_plate
from app.services.audit import write_audit
from app.services.vehicles import lock_vehicle_writes, snapshot


@dataclass
class Assessment:
    source: CoreAssetRow
    result: str
    vehicle: Vehicle | None = None
    conflict_kind: str | None = None
    messages: tuple[str, ...] = ()


def _assess(sources: list[CoreAssetRow], vehicles: list[Vehicle]) -> list[Assessment]:
    by_core = {item.core_asset_id: item for item in vehicles if item.core_asset_id is not None}
    by_plate: dict[tuple[str, str], list[Vehicle]] = defaultdict(list)
    by_chassis = {item.chassis_no: item for item in vehicles if item.chassis_no is not None}
    for item in vehicles:
        by_plate[item.plate_no_norm, item.province].append(item)
    assets = [row.asset for row in sources if row.asset is not None]
    cores = Counter(asset.core_asset_id for asset in assets)
    plates = Counter(
        (normalize_plate(asset.plate_no), asset.province)
        for asset in assets
        if asset.status != "DISPOSED"
    )
    chassis = Counter(asset.chassis_no for asset in assets if asset.chassis_no is not None)
    assessed: list[Assessment] = []
    for source in sources:
        asset = source.asset
        if asset is None:
            assessed.append(Assessment(source, "ERROR", messages=source.errors))
            continue
        plate = normalize_plate(asset.plate_no), asset.province
        item = by_core.get(asset.core_asset_id)
        kind = None
        if item is None:
            matches = [
                candidate
                for candidate in by_plate[plate]
                if candidate.origin == "MTN" and candidate.core_asset_id is None
            ]
            if len(matches) > 1:
                assessed.append(Assessment(source, "ERROR", messages=("AMBIGUOUS_MATCH",)))
                continue
            if matches:
                item, kind = matches[0], "LINK"
        error = None
        if cores[asset.core_asset_id] > 1:
            error = "DUPLICATE_CORE_ASSET"
        elif asset.status != "DISPOSED" and (
            plates[plate] > 1
            or any(
                candidate.status != "DISPOSED" and candidate is not item
                for candidate in by_plate[plate]
            )
        ):
            error = "DUPLICATE_PLATE"
        elif asset.chassis_no is not None and (
            chassis[asset.chassis_no] > 1
            or (asset.chassis_no in by_chassis and by_chassis[asset.chassis_no] is not item)
        ):
            error = "DUPLICATE_CHASSIS"
        elif item is not None and item.status == "DISPOSED" and asset.status != "DISPOSED":
            error = "INVALID_STATE_TRANSITION"
        if error is not None:
            assessed.append(Assessment(source, "ERROR", item, messages=(error,)))
        elif kind == "LINK":
            assessed.append(Assessment(source, "CONFLICT", item, kind, ("LINK_REQUIRED",)))
        elif item is None:
            assessed.append(Assessment(source, "NEW"))
        else:
            incoming = asset.model_dump(mode="json")
            current = snapshot(item)
            different = any(incoming[key] != current[key] for key in CORE_COLUMNS)
            if different and item.core_sync_status != "SYNCED":
                assessed.append(
                    Assessment(source, "CONFLICT", item, "LOCAL_CHANGES", ("LOCAL_CHANGES",))
                )
            else:
                assessed.append(Assessment(source, "UPDATE" if different else "UNCHANGED", item))
    targets = Counter(
        row.vehicle.id for row in assessed if row.vehicle is not None and row.result != "ERROR"
    )
    for row in assessed:
        if row.vehicle is not None and targets[row.vehicle.id] > 1 and row.result != "ERROR":
            row.result, row.messages, row.conflict_kind = "ERROR", ("DUPLICATE_TARGET",), None
    return assessed


def _vehicles(session: Session) -> list[Vehicle]:
    # The small company registry is indexed once per batch, avoiding 10,000
    # per-row lookup queries. All writers take the same advisory lock first.
    return list(
        session.execute(select(Vehicle).execution_options(populate_existing=True)).scalars()
    )


def _rows(session: Session, identity: uuid.UUID) -> list[VehicleImportRow]:
    return list(
        session.execute(
            select(VehicleImportRow)
            .where(VehicleImportRow.batch_id == identity)
            .order_by(VehicleImportRow.row_no)
            .execution_options(populate_existing=True)
        ).scalars()
    )


def get_batch(session: Session, identity: uuid.UUID, *, lock: bool = False) -> VehicleImportBatch:
    statement = (
        select(VehicleImportBatch)
        .where(VehicleImportBatch.id == identity)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    batch = session.execute(statement).scalar_one_or_none()
    if batch is None:
        raise APIError(404, "IMPORT_NOT_FOUND", "Import batch not found")
    return batch


def batch_view(session: Session, batch: VehicleImportBatch) -> ImportView:
    warnings = ["IMPORT_FILE_SEEN_BEFORE"] if batch.summary_json.get("file_seen_before") else []
    return ImportView(
        id=batch.id,
        file_name=batch.file_name,
        file_sha256=batch.file_sha256,
        status=batch.status,
        row_count=batch.row_count,
        summary=batch.summary_json,
        warnings=warnings,
        uploaded_at=batch.uploaded_at,
        applied_at=batch.applied_at,
        rows=[ImportRowView.model_validate(row) for row in _rows(session, batch.id)],
    )


def _editable(batch: VehicleImportBatch) -> None:
    if batch.status == "APPLIED":
        raise APIError(409, "IMPORT_ALREADY_APPLIED", "Import batch was already applied")
    if batch.status != "VALIDATED":
        raise APIError(409, "INVALID_STATE_TRANSITION", "Import batch was discarded")


def _batch_snapshot(batch: VehicleImportBatch) -> dict[str, object]:
    return {"status": batch.status, "row_count": batch.row_count, "summary": batch.summary_json}


def _restore_sources(rows: list[VehicleImportRow]) -> list[CoreAssetRow]:
    sources = []
    for row in rows:
        raw = row.raw_json["input"]
        assert isinstance(raw, dict)
        errors = row.raw_json["validation_errors"]
        assert isinstance(errors, list)
        sources.append(
            CoreAssetRow(row.row_no, raw, None if errors else parse_asset(raw), tuple(errors))
        )
    return sources


def revalidate_import(session: Session, identity: uuid.UUID, actor: AppUser) -> VehicleImportBatch:
    """Refresh the original preview after staleness, without evading SHA deduplication."""
    lock_vehicle_writes(session)
    with session.begin_nested():
        batch = get_batch(session, identity, lock=True)
        _editable(batch)
        if batch.summary_json.get("file_seen_before"):
            raise APIError(409, "IMPORT_FILE_SEEN_BEFORE", "Repeated-file batches remain no-ops")
        before = _batch_snapshot(batch)
        rows = _rows(session, identity)
        vehicles = _vehicles(session)
        assessed = _assess(_restore_sources(rows), vehicles)
        for row, assessment in zip(rows, assessed, strict=True):
            target = assessment.vehicle
            row.result, row.resolution = assessment.result, None
            row.matched_vehicle_id = target.id if target is not None else None
            row.messages_json = list(assessment.messages)
            row.raw_json = {
                **row.raw_json,
                "expected_version": target.version if target is not None else None,
                "conflict_kind": assessment.conflict_kind,
            }
        present = {row.vehicle.id for row in assessed if row.vehicle is not None}
        batch.summary_json = {
            "counts": dict(Counter(row.result for row in assessed)),
            "file_seen_before": False,
            "not_found_in_file": [str(item.id) for item in vehicles if item.id not in present],
        }
        write_audit(
            session,
            entity="vehicle_import_batch",
            entity_id=batch.id,
            action="REVALIDATE",
            actor_id=actor.id,
            before=before,
            after=_batch_snapshot(batch),
        )
        session.flush()
    session.commit()
    return batch


def validate_import(
    session: Session,
    actor: AppUser,
    source: CoreAssetSource,
    data: bytes,
    filename: str,
) -> VehicleImportBatch:
    sources = list(source.iter_assets())  # Complete structural validation before DB writes.
    lock_vehicle_writes(session)
    with session.begin_nested():
        digest = hashlib.sha256(data).hexdigest()
        seen = (
            session.execute(
                select(VehicleImportBatch.id)
                .where(VehicleImportBatch.file_sha256 == digest)
                .limit(1)
            ).first()
            is not None
        )
        vehicles = _vehicles(session)
        assessed = _assess(sources, vehicles)
        present = {row.vehicle.id for row in assessed if row.vehicle is not None}
        batch = VehicleImportBatch(
            id=uuid.uuid4(),
            file_name=filename,
            file_sha256=digest,
            row_count=len(sources),
            status="VALIDATED",
            uploaded_by=actor.id,
            summary_json={
                "counts": dict(Counter("UNCHANGED" if seen else row.result for row in assessed)),
                "file_seen_before": seen,
                "not_found_in_file": [str(item.id) for item in vehicles if item.id not in present],
            },
        )
        session.add(batch)
        session.flush()
        for row in assessed:
            session.add(
                VehicleImportRow(
                    batch_id=batch.id,
                    row_no=row.source.row_no,
                    raw_json={
                        "input": row.source.raw,
                        "expected_version": row.vehicle.version
                        if row.vehicle is not None
                        else None,
                        "conflict_kind": row.conflict_kind,
                        "validation_errors": list(row.source.errors),
                    },
                    result="UNCHANGED" if seen else row.result,
                    matched_vehicle_id=row.vehicle.id if row.vehicle is not None else None,
                    messages_json=["IMPORT_FILE_SEEN_BEFORE"] if seen else list(row.messages),
                )
            )
        write_audit(
            session,
            entity="vehicle_import_batch",
            entity_id=batch.id,
            action="CREATE",
            actor_id=actor.id,
            after=_batch_snapshot(batch),
        )
        session.flush()
    session.commit()
    return batch


def resolve_import(
    session: Session, identity: uuid.UUID, payload: ImportResolutions, actor: AppUser
) -> VehicleImportBatch:
    lock_vehicle_writes(session)
    with session.begin_nested():
        batch = get_batch(session, identity, lock=True)
        _editable(batch)
        rows = {row.row_no: row for row in _rows(session, identity)}
        before = {str(number): row.resolution for number, row in rows.items() if row.resolution}
        for decision in payload.resolutions:
            row = rows.get(decision.row_no)
            if row is None or row.result != "CONFLICT":
                raise APIError(
                    422, "IMPORT_RESOLUTION_INVALID", "Decision must reference a conflict row"
                )
            allowed = (
                {"LINK"} if row.raw_json["conflict_kind"] == "LINK" else {"USE_CORE", "KEEP_MNT"}
            )
            if decision.resolution not in allowed:
                raise APIError(
                    422, "IMPORT_RESOLUTION_INVALID", "Decision is invalid for this conflict"
                )
            row.resolution = decision.resolution
        write_audit(
            session,
            entity="vehicle_import_batch",
            entity_id=batch.id,
            action="RESOLVE",
            actor_id=actor.id,
            before=before,
            after={str(number): row.resolution for number, row in rows.items() if row.resolution},
        )
        session.flush()
    session.commit()
    return batch


def _accept(session: Session, assessment: Assessment, actor: AppUser, now: datetime) -> None:
    asset = assessment.source.asset
    assert asset is not None
    item = assessment.vehicle
    before = snapshot(item) if item is not None else None
    if item is None:
        item = Vehicle(id=uuid.uuid4(), origin="CORE_IMPORT", created_by=actor.id, version=1)
        session.add(item)
    elif assessment.result == "UNCHANGED" and item.core_sync_status == "SYNCED":
        return
    else:
        item.version += 1
    for key, value in asset.model_dump().items():
        setattr(item, key, value)
    item.plate_no_norm = normalize_plate(item.plate_no)
    item.core_sync_status = "SYNCED"
    item.last_imported_at = now
    item.updated_by = actor.id
    session.flush()
    if before is None or (before["plate_no"], before["province"]) != (item.plate_no, item.province):
        history = list(
            session.execute(
                select(VehiclePlateHistory).where(VehiclePlateHistory.vehicle_id == item.id)
            ).scalars()
        )
        if before is not None and not history:
            session.add(
                VehiclePlateHistory(
                    vehicle_id=item.id,
                    plate_no=str(before["plate_no"]),
                    plate_no_norm=str(before["plate_no_norm"]),
                    province=str(before["province"]),
                    valid_from=item.created_at,
                    valid_to=now,
                    source=item.origin,
                )
            )
        for previous in history:
            if previous.valid_to is None:
                previous.valid_to = now
        session.add(
            VehiclePlateHistory(
                vehicle_id=item.id,
                plate_no=item.plate_no,
                plate_no_norm=item.plate_no_norm,
                province=item.province,
                valid_from=now,
                source="CORE_IMPORT",
            )
        )
    write_audit(
        session,
        entity="vehicle",
        entity_id=item.id,
        action="IMPORT_CREATE" if before is None else "IMPORT_UPDATE",
        actor_id=actor.id,
        before=before,
        after=snapshot(item),
    )
    assessment.vehicle = item
    session.flush()


def apply_import(session: Session, identity: uuid.UUID, actor: AppUser) -> VehicleImportBatch:
    lock_vehicle_writes(session)
    try:
        with session.begin_nested():
            batch = get_batch(session, identity, lock=True)
            _editable(batch)
            rows = _rows(session, identity)
            if any(row.result == "CONFLICT" and row.resolution is None for row in rows):
                raise APIError(
                    409, "IMPORT_UNRESOLVED_CONFLICT", "Resolve every conflict before applying"
                )
            before = _batch_snapshot(batch)
            applied = 0
            if not batch.summary_json.get("file_seen_before"):
                assessments = _assess(_restore_sources(rows), _vehicles(session))
                # All previews are checked before the first write. Reclassification
                # never silently adopts a new target or overwrites a newer edit.
                for row, assessment in zip(rows, assessments, strict=True):
                    target = assessment.vehicle
                    if (
                        row.result != assessment.result
                        or row.matched_vehicle_id != (target.id if target is not None else None)
                        or row.raw_json["expected_version"]
                        != (target.version if target is not None else None)
                        or row.raw_json["conflict_kind"] != assessment.conflict_kind
                    ):
                        raise APIError(
                            409,
                            "IMPORT_STALE",
                            "Registry changed; revalidate and review this batch",
                        )
                now = datetime.now(UTC)
                for row, assessment in zip(rows, assessments, strict=True):
                    if row.result == "ERROR" or row.resolution == "KEEP_MNT":
                        continue
                    _accept(session, assessment, actor, now)
                    if assessment.vehicle is not None:
                        row.matched_vehicle_id = assessment.vehicle.id
                    if row.result != "UNCHANGED":
                        applied += 1
            batch.status, batch.applied_by, batch.applied_at = (
                "APPLIED",
                actor.id,
                datetime.now(UTC),
            )
            batch.summary_json = {**batch.summary_json, "applied_rows": applied}
            write_audit(
                session,
                entity="vehicle_import_batch",
                entity_id=batch.id,
                action="APPLY",
                actor_id=actor.id,
                before=before,
                after=_batch_snapshot(batch),
            )
            session.flush()
    except IntegrityError:
        raise APIError(
            409, "IMPORT_STALE", "Registry identities changed; upload a fresh preview"
        ) from None
    session.commit()
    return batch


def discard_import(session: Session, identity: uuid.UUID, actor: AppUser) -> VehicleImportBatch:
    lock_vehicle_writes(session)
    with session.begin_nested():
        batch = get_batch(session, identity, lock=True)
        _editable(batch)
        before = _batch_snapshot(batch)
        batch.status = "DISCARDED"
        write_audit(
            session,
            entity="vehicle_import_batch",
            entity_id=batch.id,
            action="DISCARD",
            actor_id=actor.id,
            before=before,
            after=_batch_snapshot(batch),
        )
        session.flush()
    session.commit()
    return batch
