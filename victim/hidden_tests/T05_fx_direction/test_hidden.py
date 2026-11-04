"""Hidden tests for T05 (SPEC §5.4 / §7 Week 1). Never copied into the workspace."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date

import pytest
from shop.services.fx import NoRateError, convert_to_eur
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        trans = conn.begin()
        sess = Session(bind=conn)
        try:
            yield sess
        finally:
            sess.close()
            trans.rollback()
    engine.dispose()


def test_usd_to_eur_multiplies_by_rate(session: Session) -> None:
    # rate_to_eur is EUR per 1 USD: 100.00 USD at 0.92 is 92.00 EUR.
    assert convert_to_eur(session, 10000, "USD", date(2026, 10, 1)) == 9200


def test_unknown_currency_raises(session: Session) -> None:
    with pytest.raises(NoRateError):
        convert_to_eur(session, 100, "XXX", date(2026, 10, 1))
