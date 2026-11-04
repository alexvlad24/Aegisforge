"""Hidden tests for T08 (SPEC §5.4 / §7 Week 1). Never copied into the workspace."""

from __future__ import annotations

import pytest
from shop.services.discounts import UnknownDiscountError, apply_discounts


def test_subtotal_exactly_at_threshold_qualifies() -> None:
    assert apply_discounts(10000, ["SPEND100"]) == 9000


def test_subtotal_below_threshold_gets_no_discount() -> None:
    assert apply_discounts(9999, ["SPEND100"]) == 9999


def test_unknown_code_is_rejected() -> None:
    with pytest.raises(UnknownDiscountError):
        apply_discounts(10000, ["NOPE"])
