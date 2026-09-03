"""Shared pytest fixtures.

Every test gets a fresh in-memory SQLite database. In-memory because it is
fast and leaves no files behind; fresh per test because a test that depends on
another test's leftover rows is a test that will lie to you eventually.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.models.entities import Base


@pytest.fixture()
def db() -> Iterator[Session]:
    """An isolated in-memory database session with foreign keys enforced."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        # StaticPool keeps ONE connection, so the in-memory DB survives across
        # the session's statements instead of vanishing between them.
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
