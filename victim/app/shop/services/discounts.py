"""Discount codes. Percentages are whole percents of the subtotal."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiscountRule:
    code: str
    percent: int
    min_spend_cents: int = 0


RULES: dict[str, DiscountRule] = {
    "WELCOME10": DiscountRule("WELCOME10", 10),
    "BIG60": DiscountRule("BIG60", 60),
    "MEGA70": DiscountRule("MEGA70", 70),
    "SPEND100": DiscountRule("SPEND100", 10, min_spend_cents=10000),
}


class UnknownDiscountError(ValueError):
    pass


def apply_discounts(subtotal_cents: int, codes: list[str]) -> int:
    total = subtotal_cents
    for code in codes:
        rule = RULES.get(code)
        if rule is None:
            raise UnknownDiscountError(code)
        if subtotal_cents > rule.min_spend_cents:
            total -= subtotal_cents * rule.percent // 100
    return total
