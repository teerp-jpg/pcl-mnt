"""Exact precision, half-up rounding, bounds and untrusted computed totals."""

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.money import calculate_money
from app.errors import APIError
from app.schemas.maintenance import LineInput, LineView, RecordChanges, RecordCreate


def line(**values: object) -> LineInput:
    return LineInput.model_validate(
        {
            "category_id": uuid.uuid4(),
            "line_type": "PART",
            "description": "Synthetic",
            "qty": "1.50",
            "unit_price": "33.33",
            **values,
        }
    )


def test_half_up_per_line_and_totals_ignore_client() -> None:
    value = line(amount="999999")
    amounts, subtotal, total = calculate_money([value], Decimal("105.00"))
    assert amounts == [Decimal("50.00")]
    assert subtotal == Decimal("50.00") and total == Decimal("155.00")
    assert calculate_money([line(qty="1", unit_price="1500")], Decimal("105"))[2] == Decimal(
        "1605.00"
    )
    assert calculate_money([value, value], Decimal("0"))[1] == Decimal("100.00")
    assert "amount" not in value.model_dump()


@pytest.mark.parametrize(
    "field,value",
    [
        ("unit_price", "33.335"),
        ("qty", "1.505"),
        ("unit_price", "NaN"),
        ("unit_price", "Infinity"),
        ("qty", "-Infinity"),
        ("unit_price", "bad"),
        ("unit_price", True),
        ("unit_price", 33.33),
        ("qty", 1.5),
        ("qty", "0"),
        ("qty", "-1"),
        ("unit_price", "-1"),
        ("qty", "100000000"),
        ("unit_price", "1000000000000"),
    ],
)
def test_reject_invalid_operands(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        line(**{field: value})


@pytest.mark.parametrize("value", ["1.005", "NaN", "Infinity", "-1", "1000000000000", 0.1, True])
def test_reject_invalid_vat(value: object) -> None:
    with pytest.raises(ValidationError):
        RecordChanges.model_validate({"vat_amount": value})


def test_trailing_zero_and_round_trip_serialization() -> None:
    value = line(unit_price="33.330", qty="1.5000")
    view = LineView(id=uuid.uuid4(), line_no=1, **value.model_dump(), amount=Decimal("50"))
    data = view.model_dump(mode="json")
    assert (data["qty"], data["unit_price"], data["amount"]) == ("1.50", "33.33", "50.00")
    restored = line(qty=data["qty"], unit_price=data["unit_price"])
    assert calculate_money([restored], Decimal("0"))[0] == [Decimal("50.00")]


def test_zero_price_requires_warranty_or_note() -> None:
    with pytest.raises(ValidationError):
        line(unit_price="0")
    assert line(unit_price="0", is_warranty=True).unit_price == 0
    assert line(unit_price="0", note="Synthetic warranty").unit_price == 0


def test_calculated_line_and_total_overflow_are_rejected() -> None:
    with pytest.raises(APIError) as error:
        calculate_money([line(qty="99999999.99", unit_price="999999999999.99")], Decimal("0"))
    assert error.value.status_code == 422
    with pytest.raises(APIError):
        calculate_money([line(qty="1", unit_price="999999999999.99")], Decimal("0.01"))
    with pytest.raises(APIError):
        calculate_money([line(qty="1", unit_price="999999999999.99")] * 2, Decimal("0"))


def test_client_totals_not_in_persisted_fields() -> None:
    payload = RecordCreate(
        vehicle_id=uuid.uuid4(), lines=[line()], subtotal="999", total_amount="999"
    )
    assert "subtotal" not in payload.model_dump() and "total_amount" not in payload.model_dump()


@pytest.mark.parametrize("field", ["lines", "vat_amount", "maintenance_type"])
def test_patch_rejects_explicit_null_for_required_values(field: str) -> None:
    with pytest.raises(ValidationError):
        RecordChanges.model_validate({field: None})
