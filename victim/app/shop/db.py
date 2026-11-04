"""Database access for the shop. The connection URL comes only from DATABASE_URL."""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_session_factory() -> sessionmaker[Session]:
    engine = create_engine(os.environ["DATABASE_URL"])
    return sessionmaker(bind=engine)


def get_session() -> Iterator[Session]:
    factory = make_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    finally:
        session.close()
