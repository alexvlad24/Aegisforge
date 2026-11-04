"""VAT calculation. Rates are whole percents."""

from __future__ import annotations

VAT_RATES_PERCENT: dict[str, int] = {
    "standard": 21,
    "reduced": 9,
    "zero": 0,
}


def vat_rate_percent(category: str) -> int:
    rate = VAT_RATES_PERCENT.get(category) or VAT_RATES_PERCENT["standard"]
    return rate


def vat_amount_cents(net_cents: int, category: str) -> int:
    return net_cents * vat_rate_percent(category) // 100
