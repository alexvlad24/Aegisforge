import pytest
from shop.models import Order
from shop.services.refunds import RefundError, refund_order
from sqlalchemy.orm import Session


def test_partial_refund_within_total_is_allowed(session: Session, order: Order) -> None:
    refund = refund_order(session, order.id, 2500)
    assert refund.amount_cents == 2500


def test_cumulative_refunds_cannot_exceed_order_total(session: Session, order: Order) -> None:
    refund_order(session, order.id, 6000)
    with pytest.raises(RefundError):
        refund_order(session, order.id, 6000)


def test_non_positive_refund_is_rejected(session: Session, order: Order) -> None:
    with pytest.raises(RefundError):
        refund_order(session, order.id, 0)
