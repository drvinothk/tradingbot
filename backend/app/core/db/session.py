"""SQLAlchemy engine/session wiring. Synchronous by design for Phase 0 — the
safety-critical pieces (advisory locks, idempotency writes, mode transitions)
are easier to reason about without an async/sync split; WebSocket ingestion in
later phases runs in its own worker and talks to the DB through this same
session factory via a threadpool, not by making the engine async.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import get_settings

_settings = get_settings()

# Canonical alias for the "swap in a test-owned session_scope" pattern used
# throughout market_data/, execution_engine/, scheduler/, session/, and
# reporting/ — was independently redeclared identically in 12 modules.
SessionFactory = Callable[[], AbstractContextManager[Session]]

engine = create_engine(
    _settings.db.sqlalchemy_url,
    pool_size=_settings.db.pool_size,
    max_overflow=_settings.db.max_overflow,
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


def reuse_session(db: Session) -> SessionFactory:
    """A `SessionFactory` that hands back this *already-open* `db` instead of
    opening a new one — for passing an in-progress transaction into an API
    that expects a swappable `session_factory` (e.g. an option-chain refresh
    triggered mid-cycle), so the refresh stays atomic with the rest of the
    caller's own transaction rather than opening a second, independently-
    committing connection that can't see its not-yet-committed rows. Was
    independently redeclared as a local closure in 3 modules
    (`execution_engine.paper.position_manager`, `scheduler.eod_square_off`,
    `strategy_engine.runner`).
    """

    @contextmanager
    def _reuse() -> Generator[Session, None, None]:
        yield db

    return _reuse
