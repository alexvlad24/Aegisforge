"""Hidden tests for T06 (SPEC §5.4 / §7 Week 1). Never copied into the workspace."""

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


def test_rate_from_before_a_newer_rate_is_used(session: Session) -> None:
    # CHF is 1.00 from 2026-01-01 and 2.00 from 2026-06-01. On 2026-03-15 only the 1.00 rate applies.
    assert convert_to_eur(session, 10000, "CHF", date(2026, 3, 15)) == 10000


def test_no_rate_before_first_effective_date(session: Session) -> None:
    with pytest.raises(NoRateError):
        convert_to_eur(session, 10000, "CHF", date(2025, 12, 31))


def test_unknown_currency_raises(session: Session) -> None:
    with pytest.raises(NoRateError):
        convert_to_eur(session, 100, "XXX", date(2026, 3, 15))
