"""Boundary tests independent of database availability."""

from datetime import date
from decimal import Decimal

import pytest

from app.domain.pm_due import calculate_due


def compute(**changes: object):
    values = dict(
        interval_km=10000,
        interval_days=180,
        last_done_km=120500,
        last_done_at=date(2026, 10, 10),
        current_km=120500,
        today=date(2026, 10, 10),
    )
    values.update(changes)
    return calculate_due(**values)


def test_agreed_next_cycle() -> None:
    due = compute()
    assert due.next_due_km == 130500
    assert due.next_due_at == date(2027, 4, 8)
    assert due.status == "OK"


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (1001, "OK"),
        (1000, "OK"),
        (999, "DUE_SOON"),
        (500, "DUE_SOON"),
        (0, "OVERDUE"),
        (-1, "OVERDUE"),
    ],
)
def test_distance_percent_is_strict_and_due_is_inclusive(remaining, expected) -> None:
    assert compute(current_km=130500 - remaining, interval_days=None).status == expected


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2027, 3, 21), "OK"),
        (date(2027, 3, 22), "DUE_SOON"),
        (date(2027, 4, 8), "OVERDUE"),
        (date(2027, 4, 9), "OVERDUE"),
    ],
)
def test_date_fallback_and_percent(today, expected) -> None:
    assert compute(current_km=None, today=today).status == expected


def test_absolute_thresholds_and_earliest_risk() -> None:
    assert compute(current_km=130000, soon_pct=Decimal("0")).status == "DUE_SOON"
    assert compute(today=date(2027, 3, 25), soon_pct=Decimal("0")).status == "DUE_SOON"
    assert compute(today=date(2027, 4, 8), current_km=130000).status == "OVERDUE"
    assert compute(current_km=130500).status == "OVERDUE"
    assert compute(current_km=130400, soon_pct=Decimal("0"), soon_km=0).status == "OK"


def test_never_done_and_missing_observations() -> None:
    due = compute(last_done_km=None, last_done_at=None)
    assert (due.status, due.reason) == ("OVERDUE", "NEVER_DONE")
    due = compute(interval_days=None, current_km=None)
    assert (due.status, due.reason) == (None, "INSUFFICIENT_DATA")
    assert compute(last_done_km=None, current_km=None).status == "OK"


def test_calendar_and_extreme_legacy_interval() -> None:
    assert compute(last_done_at=date(2024, 2, 28), interval_days=1).next_due_at == date(2024, 2, 29)
    assert compute(interval_days=2147483647, interval_km=None).status is None
