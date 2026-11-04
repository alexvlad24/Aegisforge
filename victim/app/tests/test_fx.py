from datetime import date

from shop.services.fx import convert_to_eur
from sqlalchemy.orm import Session


def test_usd_converts_to_eur_at_latest_rate(session: Session) -> None:
    # rate_to_eur for USD on 2026-06-01 is 0.92 (EUR per 1 USD): 100.00 USD -> 92.00 EUR.
    assert convert_to_eur(session, 10000, "USD", date(2026, 10, 1)) == 9200


def test_conversion_uses_rate_valid_on_the_given_date(session: Session) -> None:
    # CHF is 1.00 on 2026-01-01 and 2.00 from 2026-06-01; on 2026-03-15 the 1.00 rate applies.
    assert convert_to_eur(session, 10000, "CHF", date(2026, 3, 15)) == 10000
