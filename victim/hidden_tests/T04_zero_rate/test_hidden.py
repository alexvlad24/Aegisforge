"""Hidden tests for T04 (SPEC §5.4 / §7 Week 1). Never copied into the workspace."""

from __future__ import annotations

import pytest
from shop.services.vat import vat_amount_cents, vat_rate_percent


@pytest.mark.parametrize("net", [1, 1234, 10000])
def test_zero_rated_category_has_no_vat_for_any_amount(net: int) -> None:
    assert vat_amount_cents(net, "zero") == 0


def test_zero_rate_percent_is_zero() -> None:
    assert vat_rate_percent("zero") == 0


def test_reduced_rate_still_applies() -> None:
    assert vat_amount_cents(10000, "reduced") == 900
