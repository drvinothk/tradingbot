"""FastAPI app factory. Startup sequence (in order):

1. NTP drift check + disk-space check — logged, not fatal (Scheduler in later
   phases turns a failing check into a degraded_mode transition; Phase 0 just
   needs the checks running and visible).
2. Process singleton lock — acquired once here and held for the process's
   entire lifetime via a dedicated connection, so a second instance of this
   backend can never run alongside the first believing it's also the engine.
   This is fatal: refuse to start rather than risk two execution writers.
3. Mock instrument universe sync — only when `get_broker()` resolves to
   `MockBrokerAdapter` (a no-op once Phase 5's real Shoonya adapter is wired
   in): syncs `instruments`/`option_contracts` DB rows from the same seeded
   universe the broker singleton itself quotes against, so
   `get_option_chain()`/strike ranking have something real to find against
   the live server, not just in tests (which always construct their own
   explicitly seeded adapter). See `broker_adapter.composition.get_broker`'s
   own docstring for why this was missing until Phase 4's manual QC caught it.
4. Startup-recovery check — looks for any trading_session left ACTIVE with
   open positions from a previous run (crash/reboot), resumes each one's
   `PositionManager` (so stop/trail management picks back up instead of the
   process coming back up idle) and runs an immediate reconciliation pass
   against the broker's own book, per Phase 3.
5. Health-check scheduler — starts `HealthCheckScheduler`
   (`scheduler/health_check.py`), the repeating version of step 1's one-shot
   boot check: on a 5-minute timer, a failing NTP/disk check now writes
   `metric_series` rows and moves any `paper_plus_guarded_live`/
   `live_enabled` session to `degraded_mode`, not just a log line. Addendum
   hardening batch, promoted from "known open item" to a Phase 6
   prerequisite — see the build plan.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import (
    audit,
    auth,
    execution,
    instruments,
    metrics,
    reports,
    sessions,
    shoonya,
    strategies,
    system_alerts,
)
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


def _sync_mock_instrument_universe() -> None:
    """No-op once Phase 5's real Shoonya adapter is configured — that
    adapter syncs its own instrument master from the exchange. For the mock
    adapter, `get_broker()` already seeds a synthetic universe in-memory;
    this just makes sure the DB's `instruments`/`option_contracts` rows match
    what that same instance actually quotes, so `get_option_chain()` calls
    against the live singleton (not a test's own explicitly seeded adapter)
    resolve to something real.

    2026-08-12: **real bug found and fixed here.** This used to run
    unconditionally on every startup whenever `get_broker()` resolved to
    the mock — correct for a genuinely fresh dev/test DB, but on a real
    deployment, *every* restart starts with the mock broker (before a user
    manually reconnects Shoonya), so this fired again even after a real
    Shoonya OAuth login had already populated real NIFTY/BANKNIFTY option
    data in a prior session. It upserts into the *same* `Instrument`/
    `OptionContract` rows (matched by symbol/exchange) real Shoonya data
    uses, so it silently reactivated/inserted the mock's own synthetic
    "nearest Thursday" expiry on top of already-correct real data —
    live-confirmed: a routine restart reactivated 84 wrong `2026-08-13`
    rows over the correct, already-synced `2026-08-18`/`2026-08-25` data,
    caught during a post-deploy QC check, not by any test (nothing
    previously exercised this function's real logic against a
    non-empty DB). Now skipped outright whenever either known underlying
    already has an `Instrument` row — this function's only real job is
    seeding a DB that has never seen a real sync at all.
    """
    from app.core.db.session import session_scope
    from app.domain.market.models import Instrument
    from app.modules.broker_adapter.composition import get_broker
    from app.modules.broker_adapter.mock.adapter import MockBrokerAdapter
    from app.modules.scheduler.instrument_sync import sync_instrument_master

    broker = get_broker()
    if not isinstance(broker, MockBrokerAdapter):
        return

    with session_scope() as db:
        already_synced = (
            db.query(Instrument).filter(Instrument.symbol.in_(("NIFTY", "BANKNIFTY"))).count()
        )
        if already_synced:
            logger.info(
                "Skipping mock instrument universe sync — NIFTY/BANKNIFTY instrument "
                "rows already exist (real data from a prior broker sync, most likely)."
            )
            return
        log = sync_instrument_master(db, broker, ["NFO"])
        logger.info(
            "Mock instrument universe synced: status=%s instruments_updated=%d "
            "contracts_added=%d contracts_expired=%d",
            log.status,
            log.instruments_updated,
            log.contracts_added,
            log.contracts_expired,
        )


def _sync_angel_one_scrip_master() -> None:
    """No-op unless `MARKET_DATA_PROVIDER=angel_one` — no point fetching
    Angel's master file (or starting its refresh scheduler) when it isn't
    the active provider, same gating reasoning
    `_sync_mock_instrument_universe` already applies for the mock adapter.
    Runs after `_sync_mock_instrument_universe`/instrument sync (this sync
    matches Angel rows against *existing* `Instrument`/`OptionContract` rows
    — needs them to exist first) and before `_resume_strategy_runners`
    (resumed ingestion needs the symbol/token mapping ready before it can
    subscribe). Tolerant of failure — logs and continues, same as every
    other startup step; a failed sync is recorded in `scrip_master_sync_log`
    via `ScripMasterService.sync_to_db` itself, never raised past it.
    """
    from app.config.settings import get_settings
    from app.core.db.session import session_scope
    from app.modules.market_data.provider_composition import get_scrip_master
    from app.modules.market_data.scrip_master_scheduler import (
        ensure_scrip_master_refresh_scheduler_running,
    )

    if get_settings().market_data.provider != "angel_one":
        return

    scrip_master = get_scrip_master()
    try:
        rows_parsed = scrip_master.fetch_and_parse()
        with session_scope() as db:
            log = scrip_master.sync_to_db(db)
            # Read while the session is still open — session_scope() closes
            # on exit, and with the default expire_on_commit=True, touching
            # an attribute afterward re-triggers a DB load against an
            # already-closed session (DetachedInstanceError). Same trap
            # record_option_chain_snapshot's own docstring already documents
            # for this exact codebase.
            status = log.status
            rows_mapped = log.rows_mapped
        logger.info(
            "Angel One scrip master synced: status=%s rows_parsed=%d rows_mapped=%d",
            status,
            rows_parsed,
            rows_mapped,
        )
    except Exception:
        logger.exception("Angel One scrip master sync failed at startup — will retry hourly")

    ensure_scrip_master_refresh_scheduler_running(scrip_master)


def _run_startup_recovery_check() -> None:
    """For every trading_session left ACTIVE with at least one open Position
    (the signature of a crash/reboot mid-position, not a clean shutdown):
    resume its `PositionManager` — this is what actually closes the "comes
    back up idle" gap the whole startup-recovery hook exists for — and run
    one immediate reconciliation pass against the broker's own book, so a
    stale local/broker mismatch from before the crash is caught right away
    rather than waiting for the manager's own poll cadence.

    A session with no open positions is left alone — nothing to resume.
    """
    from app.core.db.session import session_scope
    from app.domain.broker.models import ReconciliationTrigger
    from app.domain.execution.models import Position, PositionStatus
    from app.domain.session.models import TradingSession
    from app.modules.broker_adapter.composition import get_execution_broker
    from app.modules.execution_engine.paper.registry import ensure_position_manager_running
    from app.modules.reconciliation.service import run_reconciliation

    with session_scope() as db:
        active_sessions = (
            db.query(TradingSession)
            .filter(TradingSession.status == TradingSessionStatus.ACTIVE)
            .all()
        )
        if not active_sessions:
            logger.info("Startup recovery check: no stale active sessions found.")
            return

        resumed = []
        for trading_session in active_sessions:
            has_open_position = (
                db.query(Position.id)
                .filter(
                    Position.trading_session_id == trading_session.id,
                    Position.status == PositionStatus.OPEN,
                )
                .first()
                is not None
            )
            if not has_open_position:
                continue

            ensure_position_manager_running(trading_session.id)
            run_reconciliation(
                db,
                get_execution_broker(trading_session),
                trading_session,
                ReconciliationTrigger.EVENT,
            )
            resumed.append(trading_session.id)

        if resumed:
            logger.warning(
                "Resumed PositionManager + ran reconciliation for %d trading_session(s) "
                "found ACTIVE with open positions at startup: %s",
                len(resumed),
                [str(s) for s in resumed],
            )
        else:
            logger.info(
                "Startup recovery check: %d active session(s) found, none with open positions.",
                len(active_sessions),
            )


def _resume_strategy_runners() -> None:
    """For every `StrategyRun` left non-`STOPPED` on a `trading_session`
    still `ACTIVE` (the signature of a crash/restart mid-scan, not a clean
    `stop_strategy` call): rebuild its `Strategy` object and resume its
    `StrategyRunner` thread. Without this, `strategy_runs.status` stays
    `scanning` forever after any restart — an in-process
    `threading.Thread` (`api.v1.strategies._RUNNERS`) with nothing durable
    behind it — while nothing is actually happening: no market-data
    ingestion, no evaluate() cycles, no signals. `GET /strategies/running`
    keeps reporting it as live regardless, since it reads `strategy_runs`
    rows, not runner liveness. Found live: three real restarts in one
    session (deploying the Shoonya WS diagnostic patch) each silently
    zombied every running strategy this same way.

    Only possible because `StrategyRun.instrument_id`/`expiry_date` are now
    persisted (see that column's own docstring) — before, that information
    only ever lived in the in-memory `Strategy` object inside the runner
    thread itself, so a resume was impossible even in principle. Runs where
    those are still `NULL` predate the column and are skipped, not
    resumed — they need a manual stop + restart via the API, same as before
    this fix existed.

    A `trading_session` that isn't `ACTIVE` (kill_switch/degraded_mode/
    reconciliation_lock/ended) is deliberately not resumed — same
    "don't silently reanimate a session no longer in a tradeable state"
    reasoning as the `PositionManager` resume above. One run's failure
    (a stale `strategy_type`, a deleted `Instrument`) is caught and skipped
    rather than aborting every other run's resume or startup itself.

    2026-08-14: `ensure_ingestion_running` is skipped entirely (not just
    deferred a cycle) when `market_data.provider_composition.
    is_shoonya_market_data_ready()` is `False` — this function runs before
    any human has had a chance to reconnect Shoonya, so calling it
    unconditionally used to permanently cache the market-data provider
    singleton wrapping the mock broker, silently writing fabricated prices
    into `price_bars`/`quote_ticks` under a real-looking "shoonya"
    configuration until a human happened to reconnect. The `StrategyRunner`
    thread still starts either way — it just sits idle (no bars, no
    signal) until `market_data.registry.reset_for_reconnect` starts
    ingestion for real once Shoonya connects.
    """
    from app.api.v1.strategies import _RUNNERS, _build_strategy
    from app.core.db.session import session_scope
    from app.core.sleep_inhibitor import get_sleep_inhibitor
    from app.domain.market.models import Instrument
    from app.domain.session.models import TradingSession
    from app.domain.strategy.models import StrategyConfig, StrategyRun, StrategyRunStatus
    from app.modules.execution_engine.paper.registry import ensure_position_manager_running
    from app.modules.market_data.provider_composition import is_shoonya_market_data_ready
    from app.modules.market_data.registry import ensure_ingestion_running
    from app.modules.strategy_engine.runner import StrategyRunner

    with session_scope() as db:
        runs = (
            db.query(StrategyRun)
            .join(TradingSession, StrategyRun.trading_session_id == TradingSession.id)
            .filter(
                StrategyRun.status != StrategyRunStatus.STOPPED,
                TradingSession.status == TradingSessionStatus.ACTIVE,
            )
            .all()
        )
        if not runs:
            logger.info("Strategy-runner recovery check: no stale active runs found.")
            return

        ingestion_ready = is_shoonya_market_data_ready()
        if not ingestion_ready:
            logger.warning(
                "Shoonya not connected yet — deferring market-data ingestion for all "
                "resumed runs until reconnect (see market_data.registry.reset_for_reconnect)."
            )

        resumed: list[uuid.UUID] = []
        skipped_no_instrument: list[uuid.UUID] = []
        for run in runs:
            if run.instrument_id is None or run.expiry_date is None:
                skipped_no_instrument.append(run.id)
                continue

            try:
                strategy_config = db.get(StrategyConfig, run.strategy_config_id)
                instrument = db.get(Instrument, run.instrument_id)
                if strategy_config is None or instrument is None:
                    logger.warning(
                        "strategy_run %s references a missing config/instrument — "
                        "skipping resume",
                        run.id,
                    )
                    continue

                strategy = _build_strategy(strategy_config, run.instrument_id, run.expiry_date)
                interval = run.interval_seconds if run.interval_seconds is not None else 30.0
                runner = StrategyRunner(strategy, run.id, interval_seconds=interval)
                runner.start()
                _RUNNERS[run.id] = runner

                get_sleep_inhibitor().acquire(f"strategy_run:{run.id}")
                if ingestion_ready:
                    ensure_ingestion_running(instrument.symbol)
                ensure_position_manager_running(run.trading_session_id)

                resumed.append(run.id)
            except Exception:
                logger.exception(
                    "Failed to resume strategy_run %s — leaving it non-stopped but idle; "
                    "stop and restart it manually via the API",
                    run.id,
                )

        if resumed:
            logger.warning(
                "Resumed %d strategy runner(s) found active at startup: %s",
                len(resumed),
                [str(r) for r in resumed],
            )
        if skipped_no_instrument:
            logger.warning(
                "%d strategy_run(s) left non-stopped but predate instrument_id/expiry_date "
                "and cannot be resumed — stop and restart them via the API: %s",
                len(skipped_no_instrument),
                [str(r) for r in skipped_no_instrument],
            )


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
        _sync_mock_instrument_universe()
        _sync_angel_one_scrip_master()
        _run_startup_recovery_check()
        _resume_strategy_runners()

        from app.modules.market_data.market_data_scheduler import (
            ensure_market_data_scheduler_running,
        )
        from app.modules.scheduler.health_check import ensure_health_check_scheduler_running

        ensure_health_check_scheduler_running()
        ensure_market_data_scheduler_running()
    except Exception:
        from app.core.locking import release_advisory_lock

        release_advisory_lock(singleton_connection, LOCK_PROCESS_SINGLETON)
        singleton_connection.close()
        logger.exception("Startup failed after acquiring the singleton lock — released it.")
        raise

    yield

    from app.core.locking import release_advisory_lock
    from app.modules.execution_engine.paper.registry import stop_all as stop_all_position_managers
    from app.modules.market_data.market_data_scheduler import stop_market_data_scheduler
    from app.modules.market_data.provider_composition import get_market_data_provider
    from app.modules.market_data.scrip_master_scheduler import (
        stop_scrip_master_refresh_scheduler,
    )
    from app.modules.scheduler.health_check import stop_health_check_scheduler

    stop_all_position_managers()
    stop_health_check_scheduler()
    stop_market_data_scheduler()
    stop_scrip_master_refresh_scheduler()
    # 2026-08-11: found missing during a live-WS troubleshooting audit —
    # `stop_market_data_scheduler()` only stops that class's own polling
    # thread; it never tears down the actual provider connection.
    # `MarketDataScheduler._handle_transition` only calls `disconnect()` on
    # a MarketPhase.CLOSED transition, never on an explicit app shutdown, so
    # every prior restart left any open WS connection (Angel One's
    # `AngelWSClient`, if one had ever gotten far enough to hold one open)
    # to die abruptly when the process exited instead of via a clean
    # `close_connection()` call — indistinguishable, from the broker's own
    # side, from a real network failure, and a plausible (unconfirmed)
    # contributor if Angel enforces a per-account concurrent-connection cap.
    # Guarded with getattr, same defensive pattern `set_market_data_provider`/
    # `reset_for_tests` already use — "mock" has no close() at all, and a
    # provider that was never actually connected (a fresh mock-only test
    # run) shouldn't need special-casing here.
    close = getattr(get_market_data_provider(), "close", None)
    if callable(close):
        close()
    release_advisory_lock(singleton_connection, LOCK_PROCESS_SINGLETON)
    singleton_connection.close()
    logger.info("Process singleton lock released; shutdown complete.")


def create_app() -> FastAPI:
    app = FastAPI(title="Trading Bot Backend", version="0.1.0", lifespan=lifespan)

    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(sessions.router, prefix="/api/v1")
    app.include_router(sessions.broker_accounts_router, prefix="/api/v1")
    app.include_router(strategies.router, prefix="/api/v1")
    app.include_router(execution.router, prefix="/api/v1")
    app.include_router(reports.router, prefix="/api/v1")
    app.include_router(instruments.router, prefix="/api/v1")
    app.include_router(audit.router, prefix="/api/v1")
    app.include_router(metrics.router, prefix="/api/v1")
    app.include_router(system_alerts.router, prefix="/api/v1")
    # No /api/v1 prefix, deliberately: SHOONYA_REDIRECT_URL (the fixed URL
    # the user registers on Shoonya's own API key form) is
    # http://127.0.0.1:5000/shoonya/callback — mounting under /api/v1 would
    # break that redirect. /login-url and /status live at the same
    # unprefixed path for consistency, not because they need to.
    app.include_router(shoonya.router)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
