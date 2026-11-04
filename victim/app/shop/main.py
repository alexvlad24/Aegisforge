"""FastAPI entry point for the shop. Thin HTTP layer over shop.services."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from shop.db import get_session
from shop.services.discounts import UnknownDiscountError, apply_discounts
from shop.services.fx import NoRateError, convert_to_eur
from shop.services.refunds import RefundError, refund_order
from shop.services.vat import vat_amount_cents

app = FastAPI(title="Shop")


class RefundIn(BaseModel):
    amount_cents: int


class RefundOut(BaseModel):
    id: int
    order_id: int
    amount_cents: int


@app.get("/vat")
def get_vat(net_cents: int, category: str) -> dict[str, int]:
    return {"vat_cents": vat_amount_cents(net_cents, category)}


@app.get("/fx/to-eur")
def get_fx(
    session: Annotated[Session, Depends(get_session)],
    amount_cents: int,
    currency: str,
    on: date,
) -> dict[str, int]:
    try:
        return {"eur_cents": convert_to_eur(session, amount_cents, currency, on)}
    except NoRateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/discounts/apply")
def get_discounted_total(
    subtotal_cents: int,
    codes: Annotated[list[str], Query()] = [],  # noqa: B006 - FastAPI query list default
) -> dict[str, int]:
    try:
        return {"total_cents": apply_discounts(subtotal_cents, codes)}
    except UnknownDiscountError as exc:
        raise HTTPException(status_code=422, detail=f"unknown code {exc}") from exc


@app.post("/orders/{order_id}/refunds", response_model=RefundOut)
def post_refund(
    order_id: int,
    body: RefundIn,
    session: Annotated[Session, Depends(get_session)],
) -> RefundOut:
    try:
        refund = refund_order(session, order_id, body.amount_cents)
    except RefundError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RefundOut(id=refund.id, order_id=refund.order_id, amount_cents=refund.amount_cents)
