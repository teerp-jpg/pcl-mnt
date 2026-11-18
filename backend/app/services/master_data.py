"""Serialize master writes; preserve history and audit within the transaction."""

import uuid

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import APIError
from app.models import AppUser, Garage, MaintenanceCategory, SystemSetting
from app.schemas.master_data import (
    DEFAULT_PM_SETTINGS,
    CategoryCreate,
    CategoryPatch,
    CategoryView,
    GarageCreate,
    GaragePatch,
    GarageView,
    PMSettings,
)
from app.services.audit import write_audit


def normalize_garage_name(name: str) -> str:
    return "".join(name.split()).casefold()


def lock_master_writes(session: Session, resource: str) -> None:
    # Separate transaction locks for each small MTN master collection. Category
    # hierarchy edits must serialize to prevent two writes creating a cycle.
    keys = {"garage": 6835046301124, "category": 6835046301125, "settings": 6835046301126}
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": keys[resource]})


def get_master[Master: (Garage, MaintenanceCategory)](
    session: Session, model: type[Master], identity: uuid.UUID, *, for_update: bool = False
) -> Master:
    statement = select(model).where(model.id == identity).execution_options(populate_existing=True)
    if for_update:
        statement = statement.with_for_update()
    item = session.execute(statement).scalar_one_or_none()
    if item is None:
        raise APIError(404, "NOT_FOUND", "Master data not found")
    return item


def require_active_garage(session: Session, garage_id: uuid.UUID) -> Garage:
    """Shared guard for new maintenance records (integrated by T7).

    Lock until the caller commits so a concurrent deactivation cannot slip
    between validation and insertion. Historical reads must use get_master.
    """
    garage = get_master(session, Garage, garage_id, for_update=True)
    if not garage.is_active:
        raise APIError(422, "GARAGE_INACTIVE", "Garage is inactive")
    return garage


def _audit_master(
    session: Session,
    item: Garage | MaintenanceCategory,
    actor: AppUser,
    action: str,
    before: dict[str, object] | None,
) -> None:
    schema = GarageView if isinstance(item, Garage) else CategoryView
    write_audit(
        session,
        entity=item.__tablename__,
        entity_id=item.id,
        action=action,
        actor_id=actor.id,
        before=before,
        after=schema.model_validate(item).model_dump(mode="json"),
    )


def _duplicate(exc: IntegrityError) -> None:
    # Only translate known uniqueness errors; never reflect driver parameters.
    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    codes = {
        "garage_name_norm_key": "GARAGE_NAME_EXISTS",
        "garage_tax_id_key": "GARAGE_TAX_ID_EXISTS",
        "maintenance_category_code_key": "CATEGORY_CODE_EXISTS",
    }
    if constraint in codes:
        raise APIError(409, codes[constraint], "Master data already exists") from None
    raise exc


def _check_garage_unique(session: Session, item: Garage) -> None:
    for field, value, code in (
        (Garage.name_norm, item.name_norm, "GARAGE_NAME_EXISTS"),
        (Garage.tax_id, item.tax_id, "GARAGE_TAX_ID_EXISTS"),
    ):
        if value is None:
            continue
        statement = select(Garage.id).where(field == value)
        if item.id is not None:
            statement = statement.where(Garage.id != item.id)
        if session.execute(statement).first():
            raise APIError(409, code, "Master data already exists")


def create_garage(session: Session, payload: GarageCreate, actor: AppUser) -> Garage:
    lock_master_writes(session, "garage")
    try:
        with session.begin_nested():
            item = Garage(
                **payload.model_dump(),
                name_norm=normalize_garage_name(payload.name),
                created_by=actor.id,
                updated_by=actor.id,
            )
            _check_garage_unique(session, item)
            session.add(item)
            session.flush()
            _audit_master(session, item, actor, "CREATE", None)
            session.flush()
    except IntegrityError as exc:
        _duplicate(exc)
    session.commit()
    return item


def update_garage(
    session: Session,
    identity: uuid.UUID,
    payload: GaragePatch,
    actor: AppUser,
    *,
    deactivate: bool = False,
) -> Garage:
    lock_master_writes(session, "garage")
    try:
        with session.begin_nested():
            item = get_master(session, Garage, identity, for_update=True)
            if item.version != payload.version:
                raise APIError(409, "VERSION_CONFLICT", "Garage has changed")
            before = GarageView.model_validate(item).model_dump(mode="json")
            for field, value in payload.model_dump(exclude_unset=True, exclude={"version"}).items():
                setattr(item, field, value)
            if deactivate:
                item.is_active = False
            item.name_norm = normalize_garage_name(item.name)
            _check_garage_unique(session, item)
            item.version += 1
            item.updated_by = actor.id
            session.flush()
            _audit_master(session, item, actor, "DEACTIVATE" if deactivate else "UPDATE", before)
            session.flush()
    except IntegrityError as exc:
        _duplicate(exc)
    session.commit()
    return item


def _check_category(session: Session, item: MaintenanceCategory) -> None:
    if session.execute(
        select(MaintenanceCategory.id).where(
            MaintenanceCategory.code == item.code, MaintenanceCategory.id != item.id
        )
    ).first():
        raise APIError(409, "CATEGORY_CODE_EXISTS", "Category code already exists")
    visited = {item.id}
    parent_id = item.parent_id
    while parent_id is not None:
        if parent_id in visited:
            raise APIError(422, "CATEGORY_CYCLE", "Category hierarchy cannot contain a cycle")
        visited.add(parent_id)
        parent = get_master(session, MaintenanceCategory, parent_id)
        parent_id = parent.parent_id


def create_category(
    session: Session, payload: CategoryCreate, actor: AppUser
) -> MaintenanceCategory:
    lock_master_writes(session, "category")
    try:
        with session.begin_nested():
            item = MaintenanceCategory(
                id=uuid.uuid4(), **payload.model_dump(), created_by=actor.id, updated_by=actor.id
            )
            _check_category(session, item)
            session.add(item)
            session.flush()
            _audit_master(session, item, actor, "CREATE", None)
            session.flush()
    except IntegrityError as exc:
        _duplicate(exc)
    session.commit()
    return item


def update_category(
    session: Session,
    identity: uuid.UUID,
    payload: CategoryPatch,
    actor: AppUser,
    *,
    deactivate: bool = False,
) -> MaintenanceCategory:
    lock_master_writes(session, "category")
    try:
        with session.begin_nested():
            item = get_master(session, MaintenanceCategory, identity, for_update=True)
            before = CategoryView.model_validate(item).model_dump(mode="json")
            for field, value in payload.model_dump(exclude_unset=True).items():
                setattr(item, field, value)
            if deactivate:
                item.is_active = False
            _check_category(session, item)
            item.updated_by = actor.id
            session.flush()
            _audit_master(session, item, actor, "DEACTIVATE" if deactivate else "UPDATE", before)
            session.flush()
    except IntegrityError as exc:
        _duplicate(exc)
    session.commit()
    return item


def read_pm_settings(session: Session) -> PMSettings:
    values = DEFAULT_PM_SETTINGS.model_dump()
    stored = session.execute(select(SystemSetting).where(SystemSetting.key.in_(values))).scalars()
    for row in stored:
        values[row.key] = row.value_json
    return PMSettings.model_validate(values)


def update_pm_settings(session: Session, payload: PMSettings, actor: AppUser) -> PMSettings:
    lock_master_writes(session, "settings")
    with session.begin_nested():
        before = read_pm_settings(session).model_dump(mode="json")
        for key, value in payload.model_dump().items():
            row = session.get(SystemSetting, key)
            if row is None:
                session.add(SystemSetting(key=key, value_json=value))
            else:
                row.value_json = value
        write_audit(
            session,
            entity="system_setting",
            entity_id="pm_due",
            action="UPDATE",
            actor_id=actor.id,
            before=before,
            after=payload.model_dump(mode="json"),
        )
        session.flush()
    session.commit()
    return payload
