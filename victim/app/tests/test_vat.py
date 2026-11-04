from shop.services.vat import vat_amount_cents


def test_standard_rate_is_21_percent() -> None:
    assert vat_amount_cents(10000, "standard") == 2100


def test_reduced_rate_is_9_percent() -> None:
    assert vat_amount_cents(10000, "reduced") == 900


def test_vat_rounds_half_up() -> None:
    # 9% of 50 cents is 4.5 cents, which rounds to 5.
    assert vat_amount_cents(50, "reduced") == 5


def test_zero_rated_category_has_no_vat() -> None:
    assert vat_amount_cents(10000, "zero") == 0
