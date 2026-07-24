"""SQLAlchemy engine/session wiring. Synchronous by design for Phase 0 — the
safety-critical pieces (advisory locks, idempotency writes, mode transitions)
are easier to reason about without an async/sync split; WebSocket ingestion in
later phases runs in its own worker and talks to the DB through this same
session factory via a threadpool, not by making the engine async.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import get_settings

_settings = get_settings()

engine = create_engine(
    _settings.db.sqlalchemy_url,
    pool_size=_settings.db.pool_size,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: one session per request, closed after."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """For use outside request handlers (workers, startup hooks, scripts)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
