"""Database engine, session factory, and schema bootstrap.

Kept separate from entities.py so that importing a model never has the side
effect of opening a database connection - which matters for tests, where each
test wants its own isolated in-memory database.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend import config
from backend.models.entities import Base

logger = logging.getLogger("campuspool.db")


def _make_engine(url: str) -> Engine:
    """Create an engine with SQLite-appropriate settings."""
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        # FastAPI serves requests on a threadpool; SQLite's default thread check
        # would reject those connections.
        connect_args["check_same_thread"] = False

    engine = create_engine(url, connect_args=connect_args, future=True)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_constraints(dbapi_connection, _record) -> None:  # noqa: ANN001
            """SQLite ignores foreign keys unless asked, every connection.

            Without this, a FK typo fails silently instead of raising - exactly
            the kind of silent corruption Playbook A.4 exists to prevent.
            """
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            # Two processes share this file (the API and the passive watcher).
            # A busy timeout turns a momentary lock into a short wait instead
            # of an error. Deliberately NOT WAL mode: the demo DB lives in a
            # OneDrive-synced folder, and WAL's persistent header flag plus its
            # -wal/-shm side files do not travel well across synced/mounted
            # filesystems. The watcher's writes are tiny; rollback journal +
            # busy_timeout is enough.
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


engine: Engine = _make_engine(config.DATABASE_URL)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def create_all() -> None:
    """Create any missing tables. Safe to call repeatedly.

    Adequate for a hackathon prototype. A production system would use Alembic
    migrations instead, because create_all cannot alter an existing table.
    """
    Base.metadata.create_all(bind=engine)
    logger.info("Schema ensured: %d tables", len(Base.metadata.tables))


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for scripts and services.

    Commits on success, rolls back on any exception, and always closes. The
    rollback matters: a half-applied financial change is worse than none.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
