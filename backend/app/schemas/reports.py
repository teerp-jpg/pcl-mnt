"""Shared exact-money contract for JSON and CSV cost reports."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, PlainSerializer

type CostGrouping = Literal["vehicle", "month", "type", "garage"]
ReportMoney = Annotated[
    Decimal, PlainSerializer(lambda value: format(value, ".2f"), return_type=str)
]


class CostTotals(BaseModel):
    record_count: int
    subtotal: ReportMoney
    vat_amount: ReportMoney
    total_amount: ReportMoney


class CostGroup(CostTotals):
    group_key: str | None
    group_label: str


class CostSummary(BaseModel):
    group_by: CostGrouping
    date_from: date | None
    date_to: date | None
    items: list[CostGroup]
    totals: CostTotals
