"""Currency conversion. `rate_to_eur` is the number of EUR for one unit of the currency."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from shop.models import FxRate


class NoRateError(LookupError):
    pass


def rate_as_of(session: Session, currency: str, on: date) -> Decimal:
    row = session.scalar(
        select(FxRate).where(FxRate.currency == currency).order_by(FxRate.as_of.desc()).limit(1)
    )
    if row is None:
        raise NoRateError(f"no FX rate for {currency}")
    return row.rate_to_eur


def convert_to_eur(session: Session, amount_cents: int, currency: str, on: date) -> int:
    rate = rate_as_of(session, currency, on)
    eur = Decimal(amount_cents) / rate
    return int(eur.quantize(Decimal(1), rounding=ROUND_HALF_UP))
