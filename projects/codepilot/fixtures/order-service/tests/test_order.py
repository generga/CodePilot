import pytest

from order.service import validate_amount


def test_validate_amount_accepts_positive_value():
    assert validate_amount("12.50") == 12.5


def test_validate_amount_rejects_negative_value():
    with pytest.raises(ValueError, match="amount must be non-negative"):
        validate_amount(-1)
