"""Refunds against an order. Amounts are integer cents in the order's currency."""

from __future__ import annotations

from sqlalchemy.orm import Session

from shop.models import Order, Refund


class RefundError(ValueError):
    pass


def refund_order(session: Session, order_id: int, amount_cents: int) -> Refund:
    order = session.get(Order, order_id)
    if order is None:
        raise RefundError(f"unknown order {order_id}")

    remaining = order.total_cents
    if amount_cents > remaining:
        raise RefundError(f"refund {amount_cents} exceeds remaining {remaining}")

    refund = Refund(order_id=order_id, amount_cents=amount_cents)
    session.add(refund)
    session.flush()
    return refund
