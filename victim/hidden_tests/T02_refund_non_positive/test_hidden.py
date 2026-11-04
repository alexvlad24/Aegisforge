"""Hidden tests for T02 (SPEC §5.4 / §7 Week 1). Never copied into the workspace."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from shop.models import Order
from shop.services.refunds import RefundError, refund_order
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
    row = Order(
        customer_email="hidden@example.com", currency="EUR", status="paid", total_cents=10000
    )
    session.add(row)
    session.flush()
    return row


def test_negative_refund_is_rejected(session: Session, order: Order) -> None:
    with pytest.raises(RefundError):
        refund_order(session, order.id, -100)


def test_full_refund_is_allowed(session: Session, order: Order) -> None:
    refund = refund_order(session, order.id, 10000)
    assert refund.amount_cents == 10000
