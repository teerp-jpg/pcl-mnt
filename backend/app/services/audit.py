"""Explicit public snapshots, recorded in the caller's transaction."""

import math
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import AuditLog


def _sensitive(key: str) -> bool:
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    return any(
        word in normalized for word in ("password", "passwd", "secret", "token", "authorization")
    ) or normalized in {"hash", "cookie", "credentials", "apikey"}


def _safe(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, (date, datetime, Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("Audit snapshot keys must be strings")
        return {key: _safe(item) for key, item in value.items() if not _sensitive(key)}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    # Never stringify an ORM object, SecretStr or arbitrary object (possible secrets).
    raise ValueError("Unsupported audit snapshot value")


def write_audit(
    session: Session,
    *,
    entity: str,
    entity_id: str | uuid.UUID,
    action: str,
    actor_id: uuid.UUID,
    before: dict[str, object] | None = None,
    after: dict[str, object] | None = None,
    reason: str | None = None,
) -> AuditLog:
    """Add only; caller flushes/commits business writes and audit atomically.

    Callers supply explicit public fields, never raw request/ORM dictionaries.
    Recursive filtering is defense in depth, not a free-text secret detector.
    """
    safe_before = _safe(before)
    safe_after = _safe(after)
    assert safe_before is None or isinstance(safe_before, dict)
    assert safe_after is None or isinstance(safe_after, dict)
    event = AuditLog(
        entity=entity,
        entity_id=str(entity_id),
        action=action,
        actor=actor_id,
        before_json=safe_before,
        after_json=safe_after,
        reason=reason,
    )
    session.add(event)
    return event
