"""Read-only cost reports available to every authenticated role."""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.auth.dependencies import current_user
from app.db import get_session
from app.errors import APIError
from app.models import AppUser
from app.schemas.reports import CostGrouping, CostSummary
from app.services.reports import cost_summary, cost_summary_csv

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get(
    "/cost-summary",
    response_model=CostSummary,
    responses={200: {"content": {"text/csv": {"schema": {"type": "string"}}}}},
)
def report_costs(
    _actor: Annotated[AppUser, Depends(current_user)],
    session: Annotated[Session, Depends(get_session)],
    response: Response,
    group_by: CostGrouping = "vehicle",
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    output_format: Annotated[Literal["json", "csv"], Query(alias="format")] = "json",
) -> CostSummary | Response:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise APIError(
            422,
            "VALIDATION_FAILED",
            "Report start date must not be after end date",
            {"errors": [{"location": ["query", "from"], "type": "date_range"}]},
        )
    report = cost_summary(session, group_by, date_from, date_to)
    if output_format == "csv":
        return Response(
            content=cost_summary_csv(report),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="mtn-cost-summary-{group_by}.csv"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    response.headers["Cache-Control"] = "no-store"
    return report
