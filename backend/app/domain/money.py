"""Round each line once, then sum exact rounded amounts."""

from decimal import ROUND_HALF_UP, Decimal, localcontext

from app.errors import APIError
from app.schemas.maintenance import LineInput

MAX_MONEY = Decimal("999999999999.99")


def calculate_money(lines: list[LineInput], vat: Decimal) -> tuple[list[Decimal], Decimal, Decimal]:
    with localcontext() as context:
        context.prec = 40
        amounts = [
            (line.qty * line.unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            for line in lines
        ]
        subtotal = sum(amounts, Decimal("0.00"))
        total = subtotal + vat
        if any(value > MAX_MONEY for value in [*amounts, subtotal, total]):
            raise APIError(422, "VALIDATION_FAILED", "Calculated money exceeds storage precision")
    return amounts, subtotal, total
