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
            # A synced folder (OneDrive) or a second reader can hold the file
            # for a moment; wait it out instead of failing the request.
            # Seen 2026-09-04: "database is locked" -> 500 on the first audit
            # write of a chat turn, with the demo DB inside OneDrive.
            cursor.execute("PRAGMA busy_timeout=15000")

            # Phase 8 item 7: WAL is the real concurrency fix here. In the
            # default rollback-journal mode a single writer blocks every
            # reader, and this app writes on a timer (the reconciliation
            # sweeper and the Agentic Card monitor) while the browser polls
            # /api/state every few seconds - so readers were queueing behind
            # a background write for no reason. Under WAL readers never block
            # the writer and the writer never blocks readers.
            #
            # Skipped for in-memory databases: WAL needs real files, and every
            # test uses sqlite:// (no path), where the pragma is meaningless.
            #
            # NOT set for a database inside a synced folder without thought:
            # WAL adds -wal and -shm sidecar files, and a sync client that
            # copies the main file without them can produce a torn snapshot.
            # The README tells you to keep the DB outside OneDrive; that
            # instruction is load-bearing for this pragma.
            if not url.endswith(":memory:") and url != "sqlite://":
                cursor.execute("PRAGMA journal_mode=WAL")
                # Durability trade: WAL + NORMAL fsyncs at checkpoints rather
                # than every commit. A power cut can lose the last commits but
                # cannot corrupt the file. Correct for a demo whose database is
                # reproducible from seed in seconds; a real deployment holding
                # the only copy of financial history would use FULL.
                cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


def _ensure_parent_dir(url: str) -> None:
    """Let DATABASE_URL point somewhere that doesn't exist yet (e.g. a folder
    outside OneDrive); SQLite creates the file but never the directory."""
    if url.startswith("sqlite:///") and not url.endswith(":memory:"):
        from pathlib import Path

        Path(url[len("sqlite:///"):]).expanduser().parent.mkdir(parents=True, exist_ok=True)


_ensure_parent_dir(config.DATABASE_URL)
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
