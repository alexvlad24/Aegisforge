"""Visible test fixtures. Each test runs inside a transaction that is rolled back.

DATABASE_URL points at the per-run database provisioned by the harness.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from shop.models import Order
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


@pytest.fixture
def order(session: Session) -> Order:
    row = Order(customer_email="test@example.com", currency="EUR", status="paid", total_cents=10000)
    session.add(row)
    session.flush()
    return row
