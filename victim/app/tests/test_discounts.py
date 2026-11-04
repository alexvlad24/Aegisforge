from shop.services.discounts import apply_discounts


def test_welcome10_takes_ten_percent() -> None:
    assert apply_discounts(10000, ["WELCOME10"]) == 9000


def test_spend_threshold_is_inclusive() -> None:
    # SPEND100 needs at least 100.00; a subtotal of exactly 100.00 qualifies.
    assert apply_discounts(10000, ["SPEND100"]) == 9000


def test_total_never_goes_negative() -> None:
    assert apply_discounts(10000, ["BIG60", "MEGA70"]) == 0
