"""ORM mappings for the Zone 1 `ops` schema (DDL lives in infra/postgres/init/02_golden.sql)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"
    __table_args__ = {"schema": "ops"}

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str]
    name: Mapped[str]
    price_cents: Mapped[int]
    vat_category: Mapped[str]


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = {"schema": "ops"}

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_email: Mapped[str]
    currency: Mapped[str]
    status: Mapped[str]
    total_cents: Mapped[int]


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = {"schema": "ops"}

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("ops.orders.id"))
    amount_cents: Mapped[int]


class FxRate(Base):
    __tablename__ = "fx_rates"
    __table_args__ = {"schema": "ops"}

    id: Mapped[int] = mapped_column(primary_key=True)
    currency: Mapped[str]
    rate_to_eur: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    as_of: Mapped[date]
