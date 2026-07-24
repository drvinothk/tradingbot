"""FastAPI app factory. Startup sequence (in order):

1. NTP drift check + disk-space check — logged, not fatal (Scheduler in later
   phases turns a failing check into a degraded_mode transition; Phase 0 just
   needs the checks running and visible).
2. Process singleton lock — acquired once here and held for the process's
   entire lifetime via a dedicated connection, so a second instance of this
   backend can never run alongside the first believing it's also the engine.
   This is fatal: refuse to start rather than risk two execution writers.
3. Startup-recovery check — looks for any trading_session left ACTIVE with
   open positions from a previous run (crash/reboot) and would resume
   reconciliation + stop/trail management. No-op-safe in Phase 0 since
   positions don't exist yet; Phase 3 is what actually exercises this path.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import auth, sessions
from app.core.clock import check_disk_space, check_ntp_drift
from app.core.locking import LOCK_PROCESS_SINGLETON, try_advisory_lock
from app.domain.session.models import TradingSessionStatus

logger = logging.getLogger("app.startup")


def _run_startup_health_checks() -> None:
    ntp = check_ntp_drift()
    if ntp.ok:
        logger.info("NTP check ok (drift=%.3fs)", ntp.drift_seconds)
    else:
        logger.warning("NTP check failed: %s", ntp.error or f"drift={ntp.drift_seconds}s")

    disk = check_disk_space("C:/" if _is_windows() else "/")
    if disk.ok:
        logger.info("Disk check ok (%.1f GB free)", disk.free_gb)
    else:
        logger.warning("Disk check LOW (%.1f GB free of %.1f GB)", disk.free_gb, disk.total_gb)


def _is_windows() -> bool:
    import sys

    return sys.platform == "win32"


def _acquire_process_singleton_lock():
    """Returns a live Connection holding the lock, or raises RuntimeError if
    another process already holds it. Caller must keep the returned
    connection open for the process lifetime and close it on shutdown.
    """
    from app.core.db.session import engine

    connection = engine.connect()
    acquired = try_advisory_lock(connection, LOCK_PROCESS_SINGLETON)
    if not acquired:
        connection.close()
        raise RuntimeError(
            "Another instance already holds the engine process singleton lock — "
            "refusing to start a second execution engine."
        )
    return connection


def _run_startup_recovery_check() -> None:
    """No-op-safe in Phase 0: there are no positions/orders tables yet, so
    this only checks for sessions left ACTIVE and logs them. Phase 3 extends
    this to actually trigger reconciliation + resume stop/trail management.
    """
    from app.core.db.session import session_scope
    from app.domain.session.models import TradingSession

    with session_scope() as db:
        stale_active = (
            db.query(TradingSession)
            .filter(TradingSession.status == TradingSessionStatus.ACTIVE)
            .all()
        )
        if stale_active:
            logger.warning(
                "Found %d trading_session(s) still ACTIVE at startup — "
                "Phase 3+ will resume reconciliation/position management here: %s",
                len(stale_active),
                [str(s.id) for s in stale_active],
            )
        else:
            logger.info("Startup recovery check: no stale active sessions found.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _run_startup_health_checks()

    singleton_connection = _acquire_process_singleton_lock()
    app.state.singleton_connection = singleton_connection
    logger.info("Process singleton lock acquired.")

    # If anything between here and `yield` raises, the cleanup code after
    # `yield` never runs — an async generator's post-yield code only
    # executes once yield is actually reached, not on an exception before
    # it. Postgres would still release a session-scoped advisory lock once
    # the OS closes the socket on process exit, but that's relying on an
    # implicit side effect rather than an explicit, logged release — this
    # try/except makes the failure path do the same clean shutdown as the
    # success path instead.
    try:
        _run_startup_recovery_check()
    except Exception:
        from app.core.locking import release_advisory_lock

        release_advisory_lock(singleton_connection, LOCK_PROCESS_SINGLETON)
        singleton_connection.close()
        logger.exception("Startup failed after acquiring the singleton lock — released it.")
        raise

    yield

    from app.core.locking import release_advisory_lock

    release_advisory_lock(singleton_connection, LOCK_PROCESS_SINGLETON)
    singleton_connection.close()
    logger.info("Process singleton lock released; shutdown complete.")


def create_app() -> FastAPI:
    app = FastAPI(title="Trading Bot Backend", version="0.1.0", lifespan=lifespan)

    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(sessions.router, prefix="/api/v1")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
