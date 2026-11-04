"""Hidden tests for T03 (SPEC §5.4 / §7 Week 1). Never copied into the workspace."""

from __future__ import annotations

from shop.services.vat import vat_amount_cents


def test_exact_half_rounds_up_for_reduced_rate() -> None:
    assert vat_amount_cents(50, "reduced") == 5


def test_exact_half_rounds_up_for_standard_rate() -> None:
    # 21% of 150 cents is 31.5 cents, which rounds to 32.
    assert vat_amount_cents(150, "standard") == 32


def test_standard_rate_on_round_amount_is_exact() -> None:
    assert vat_amount_cents(10000, "standard") == 2100
