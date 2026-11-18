"""Pure PM calculation: evaluate each available clock, then take the earliest risk."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

PMStatus = Literal["OK", "DUE_SOON", "OVERDUE"]


@dataclass(frozen=True)
class DueResult:
    status: PMStatus | None
    reason: str | None
    next_due_km: int | None
    next_due_at: date | None
    remaining_km: int | None
    remaining_days: int | None


def calculate_due(
    *,
    interval_km: int | None,
    interval_days: int | None,
    last_done_km: int | None,
    last_done_at: date | None,
    current_km: int | None,
    today: date,
    soon_pct: Decimal = Decimal("10"),
    soon_km: int = 500,
    soon_days: int = 14,
) -> DueResult:
    next_km = (
        last_done_km + interval_km
        if (last_done_km is not None and interval_km is not None)
        else None
    )
    next_at = None
    if last_done_at is not None and interval_days is not None:
        # An extreme legacy interval must not crash the entire due listing.
        ordinal = last_done_at.toordinal() + interval_days
        if ordinal <= date.max.toordinal():
            next_at = last_done_at + timedelta(days=interval_days)
    remaining_km = (
        next_km - current_km if (next_km is not None and current_km is not None) else None
    )
    remaining_days = (next_at - today).days if next_at else None
    clocks = [
        (remaining_km, interval_km, soon_km),
        (remaining_days, interval_days, soon_days),
    ]
    if last_done_at is None and last_done_km is None:
        status, reason = "OVERDUE", "NEVER_DONE"
    elif any(remaining is not None and remaining <= 0 for remaining, _, _ in clocks):
        status, reason = "OVERDUE", None
    elif any(
        remaining is not None
        and interval is not None
        and (remaining <= absolute or Decimal(remaining) * 100 < Decimal(interval) * soon_pct)
        for remaining, interval, absolute in clocks
    ):
        status, reason = "DUE_SOON", None
    elif remaining_km is not None or remaining_days is not None:
        status, reason = "OK", None
    else:
        # Missing observations are unknown, never evidence of an OK plan.
        status, reason = None, "INSUFFICIENT_DATA"
    return DueResult(status, reason, next_km, next_at, remaining_km, remaining_days)
