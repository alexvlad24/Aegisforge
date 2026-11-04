"""Hidden tests for T01 (SPEC §5.4 / §7 Week 1). Never copied into the workspace."""

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


def test_refund_exceeding_remaining_balance_after_prior_refund(
    session: Session, order: Order
) -> None:
    refund_order(session, order.id, 4000)
    with pytest.raises(RefundError):
        refund_order(session, order.id, 6001)


def test_refund_up_to_remaining_balance_is_allowed(session: Session, order: Order) -> None:
    refund_order(session, order.id, 4000)
    refund = refund_order(session, order.id, 6000)
    assert refund.amount_cents == 6000


def test_refund_on_unknown_order_is_rejected(session: Session) -> None:
    with pytest.raises(RefundError):
        refund_order(session, 999_999_999, 100)
