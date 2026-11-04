"""Hidden tests for T07 (SPEC §5.4 / §7 Week 1). Never copied into the workspace."""

from __future__ import annotations

from shop.services.discounts import apply_discounts


def test_stacked_discounts_over_100_percent_clamp_to_zero() -> None:
    assert apply_discounts(10000, ["MEGA70", "BIG60"]) == 0


def test_stacked_discounts_clamp_on_small_subtotal() -> None:
    assert apply_discounts(5000, ["MEGA70", "BIG60"]) == 0


def test_single_discount_is_unchanged() -> None:
    assert apply_discounts(10000, ["WELCOME10"]) == 9000
