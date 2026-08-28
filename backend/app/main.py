"""FastAPI app factory. Startup sequence (in order):

1. NTP drift check + disk-space check — logged, not fatal (Scheduler in later
   phases turns a failing check into a degraded_mode transition; Phase 0 just
   needs the checks running and visible).
2. Process singleton lock — acquired once here and held for the process's
   entire lifetime via a dedicated connection, so a second instance of this
   backend can never run alongside the first believing it's also the engine.
   This is fatal: refuse to start rather than risk two execution writers.
3. Shoonya reconnect-from-cache — if a real Shoonya session was disk-cached
   by a prior `oauth_callback` login (`shoonya/session_cache.py`), attempts
   to restore it here, *before* the mock-universe sync below, so a routine
   restart during the trading day doesn't force a fresh manual browser
   OAuth login when the morning's token is still genuinely valid. Validates
   the cached token with a real API call first — a dead/expired one is
   dropped and this falls through to the mock, exactly as if no cache
   existed at all. Immediately after, `_warm_shoonya_token_cache_from_db`
   replays the persisted option `broker_token`s and resolves the two
   underlying tokens, so the strategy-resume storm in step 5 doesn't spend
   ~6 minutes failing to resolve tokens lazily under load.
4. Mock instrument universe sync — only when `get_broker()` resolves to
   `MockBrokerAdapter` (a no-op once Phase 5's real Shoonya adapter is wired
   in, including one just restored from cache by step 3): syncs
   `instruments`/`option_contracts` DB rows from the same seeded universe
   the broker singleton itself quotes against, so `get_option_chain()`/
   strike ranking have something real to find against the live server, not
   just in tests (which always construct their own explicitly seeded
   adapter). See `broker_adapter.composition.get_broker`'s own docstring for
   why this was missing until Phase 4's manual QC caught it.
5. Startup-recovery check — looks for any trading_session left ACTIVE with
   open positions from a previous run (crash/reboot), resumes each one's
   `PositionManager` (so stop/trail management picks back up instead of the
   process coming back up idle) and runs an immediate reconciliation pass
   against the broker's own book, per Phase 3.
6. Health-check scheduler — starts `HealthCheckScheduler`
   (`scheduler/health_check.py`), the repeating version of step 1's one-shot
   boot check: on a 5-minute timer, a failing NTP/disk check now writes
   `metric_series` rows and moves any `paper_plus_guarded_live`/
   `live_enabled` session to `degraded_mode`, not just a log line. Addendum
   hardening batch, promoted from "known open item" to a Phase 6
   prerequisite — see the build plan.
7. Reconciliation-lock recovery scheduler — starts
   `ReconciliationLockRecoveryScheduler`
   (`scheduler/reconciliation_lock_recovery.py`, 2026-08-25): every 60s,
   re-checks any session stuck in `reconciliation_lock` and auto-recovers
   it (including back to a live `prior_mode`, a deliberate scoped exception
   to Rule 4) once `run_full_reconciliation` comes back clean 3 checks in a
   row — see that module's own docstring and `core.modes.state_machine
   .recover_from_reconciliation_lock`'s for the full design.
8. Trade-log export scheduler — starts `TradeLogExportScheduler`
   (`reporting/export_scheduler.py`, Ops-Hardening Phase 3): once daily at
   15:35 IST, exports the day's completed trades to a per-workspace Excel
   workbook under `reports/`, one tab per (underlying, expiry) cycle.
9. Contract sync scheduler — starts `ContractSyncScheduler`
   (`scheduler/contract_sync_scheduler.py`, Ops-Hardening Phase 7): once
   daily at 08:30 IST, re-syncs `instruments`/`option_contracts` from
   Shoonya (skipped, not failed, if Shoonya isn't connected yet) so the
   09:00 auto-spawner below has fresh local expiry data to query.
10. Daily bootstrap scheduler — starts `DailyBootstrapScheduler`
   (`session/bootstrapper.py`, Ops-Hardening Phase 4): once daily at 09:00
   IST, safely closes any prior-day session left ACTIVE with no open
   positions/live runs (alerts instead of closing if it isn't empty),
   idempotently creates today's session if missing, and resumes strategy
   runners.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.orm import Session

from app.api.v1 import (
    alice_blue,
    audit,
    auth,
    execution,
    instruments,
    market_data,
    metrics,
    reports,
    sessions,
    shoonya,
    strategies,
    system_alerts,
    system_settings,
)
from app.core.clock import check_disk_space, check_ntp_drift, is_windows
from app.core.locking import LOCK_PROCESS_SINGLETON, try_advisory_lock
from app.domain.session.models import TradingSessionStatus
from app.modules.strategy_engine.recovery import resume_strategy_runners

# 2026-08-14: this app has never had a logging configuration anywhere (no
# `basicConfig`/`setLevel` — several modules' own comments already flagged
# this as a known gap, e.g. `broker_adapter/shoonya/ws_client.py`,
# `market_data/providers/angel_ws_client.py`). With no handler attached to
# the root logger, Python falls back to its "handler of last resort" —
# stderr, WARNING+ only — so every `.info()` call across the whole app
# (module-level loggers all propagate to root) has been silently invisible
# in journald this entire time. Concretely cost real diagnostic time today:
# `FailoverMarketDataProvider._check_recovery`'s "primary is back online —
# starting stabilization window" info log was firing roughly once a minute
# for 45+ minutes straight during a live Shoonya WS incident, and with only
# its paired "dropped during anti-flap window" warning visible, the
# connection looked totally silent when it was actually receiving a sparse
# trickle of ticks — a materially different, and more useful, picture.
# uvicorn configures its own named loggers (`uvicorn`/`uvicorn.error`/
# `uvicorn.access`) before importing this module but never touches the root
# logger itself, so `basicConfig` here is untouched by that and applies
# cleanly to every `app.*` logger in the codebase.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

logger = logging.getLogger("app.startup")


def _run_startup_health_checks() -> None:
    ntp = check_ntp_drift()
    if ntp.ok:
        logger.info("NTP check ok (drift=%.3fs)", ntp.drift_seconds)
    else:
        logger.warning("NTP check failed: %s", ntp.error or f"drift={ntp.drift_seconds}s")

    disk = check_disk_space("C:/" if is_windows() else "/")
    if disk.ok:
        logger.info("Disk check ok (%.1f GB free)", disk.free_gb)
    else:
        logger.warning("Disk check LOW (%.1f GB free of %.1f GB)", disk.free_gb, disk.total_gb)


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


def _attempt_shoonya_reconnect_from_cache() -> None:
    """2026-08-25 addition — closes the gap `shoonya/session_cache.py`'s own
    docstring describes: before this, every backend restart forced a fresh
    manual browser OAuth login even when the morning's Shoonya token was
    still genuinely valid. Mirrors Alice Blue's own established "cache is
    harmless if stale, the next real call just fails" philosophy
    (`market_data/providers/alice_blue_session.py`), but validates the
    cached token with one real API call *here*, at startup, rather than
    waiting for the first real call from deep inside `PositionManager`/
    market-data ingestion — the point is precisely to make
    `is_shoonya_configured()` and `GET /shoonya/status` correctly report
    "connected" right away on a successful restore, not several seconds
    later after some other call site happens to notice.

    No-ops (leaves the mock broker in place, exactly as before this existed)
    if Shoonya credentials aren't configured, or nothing is cached.
    Validation deliberately distinguishes *why* the check call failed,
    matching `rest_client.py`'s own `BrokerAuthError`/`BrokerConnectivityError`
    split: a genuinely dead/expired token (`ShoonyaSessionExpiredError`, a
    `BrokerAuthError`) drops the cache entry so it doesn't linger — the
    human gets a completely normal "Connect Shoonya" prompt, identical to
    today's behavior, never worse. Any other failure (a transient network
    blip during boot, say) does **not** discard the cache — the token itself
    is probably still fine, we just couldn't confirm it this instant — and
    still installs the adapter optimistically; if it truly is dead,
    `_AuthAwareBroker` (already wrapping every real adapter `set_broker`
    installs) catches the `BrokerAuthError` from whichever real call site
    hits it first and flips `is_shoonya_configured()` back off, the same
    self-healing path every other Shoonya auth failure already goes
    through. Deliberately does not
    replicate `api.v1.shoonya.oauth_callback`'s `sync_instrument_master`/
    `_seed_option_anchors`/`reset_for_reconnect` steps — those handle
    *first-time-this-session* instrument/market-data wiring; on a restart,
    those DB rows already exist from the prior session and the periodic
    `ContractSyncScheduler` (started later in `lifespan`) keeps them fresh.
    Because this runs before any market-data provider singleton is built,
    `get_broker()` already resolves to the real adapter by the time
    `provider_composition.get_market_data_provider()` is first constructed
    later in `lifespan` — no explicit `reset_for_reconnect()` call is needed
    here the way `oauth_callback`'s *mid-session* reconnect case needs one.
    """
    from app.config.settings import get_settings
    from app.modules.broker_adapter.composition import set_broker
    from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter
    from app.modules.broker_adapter.shoonya.session_cache import (
        get_cached_shoonya_session,
        set_cached_shoonya_session,
    )

    settings = get_settings().shoonya
    if settings.missing_required_fields():
        return

    cached = get_cached_shoonya_session()
    if cached is None:
        return

    from app.modules.broker_adapter.base.errors import BrokerAuthError

    adapter = ShoonyaBrokerAdapter(settings, cached)
    try:
        adapter.get_margin()
    except BrokerAuthError:
        logger.warning(
            "Cached Shoonya session is dead (auth failure on validation) — discarding it; "
            "a fresh manual login via /shoonya/login-url will be needed.",
            exc_info=True,
        )
        adapter.close()
        set_cached_shoonya_session(None)
        return
    except Exception:
        logger.warning(
            "Could not validate the cached Shoonya session on startup (non-auth failure, "
            "likely transient) — installing it anyway; a later auth failure will still be "
            "caught and reported normally.",
            exc_info=True,
        )

    set_broker(adapter)
    logger.info("Shoonya session restored from disk cache — reconnected without a fresh login.")


def _persisted_shoonya_option_tokens(db: Session) -> list[tuple[str, str]]:
    """`(OptionContract.symbol, broker_token)` for every currently-tradable
    NIFTY/BANKNIFTY option that already has a persisted broker token — the
    read half of `_warm_shoonya_token_cache_from_db`. Factored out so it's
    unit-testable without faking the full SQLAlchemy chain.
    """
    from datetime import date

    from app.domain.market.models import Instrument, OptionContract
    from app.modules.broker_adapter.shoonya.adapter import KNOWN_UNDERLYINGS

    rows = (
        db.query(OptionContract.symbol, OptionContract.broker_token)
        .join(Instrument, OptionContract.instrument_id == Instrument.id)
        .filter(
            Instrument.symbol.in_(KNOWN_UNDERLYINGS),
            OptionContract.is_active.is_(True),
            OptionContract.broker_token != "",
            OptionContract.expiry_date >= date.today(),
        )
        .all()
    )
    return [(symbol, token) for symbol, token in rows]


def _warm_shoonya_token_cache_from_db() -> None:
    """Runs once at startup, right after `_attempt_shoonya_reconnect_from_cache`
    and *before* the recovery/strategy-resume storm, to defeat the ~6-minute
    post-restart "no cached broker token for 'NIFTY…'" window: a fresh
    `ShoonyaBrokerAdapter` has an empty in-process `_token_by_symbol`, and the
    5-runners + PositionManager + option-chain-refresh burst that
    `_run_startup_recovery_check`/`resume_strategy_runners` kick off otherwise
    has to resolve every token lazily, under load, and fails for minutes.

    Deliberately lighter and read-only vs. the fresh-OAuth path's
    `sync_instrument_master` (no ~650KB scrip-master download, no
    `instruments`/`option_contracts` upsert): replay the already-persisted
    option `broker_token`s from the DB, then let the adapter resolve just the
    two bare-underlying NSE tokens (one `SearchScrip` each). Synchronous on
    purpose — it must finish before the storm; a backgrounded warm-up would
    reintroduce the race. Entirely best-effort: any failure is logged and
    startup continues (the `MarketDataScheduler` 300s health check remains the
    backstop for whatever this misses).
    """
    from app.core.db.session import session_scope
    from app.modules.broker_adapter.composition import (
        get_broker,
        is_shoonya_configured,
        unwrap_broker,
    )

    if not is_shoonya_configured():
        return

    from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter

    inner = unwrap_broker(get_broker())
    if not isinstance(inner, ShoonyaBrokerAdapter):
        return

    try:
        with session_scope() as db:
            pairs = _persisted_shoonya_option_tokens(db)
        inner.warm_token_cache(pairs)
        logger.info(
            "Shoonya token-cache warm-up: replayed %d persisted option token(s) + resolved "
            "NIFTY/BANKNIFTY underlyings before strategy resume.",
            len(pairs),
        )
    except Exception:
        logger.warning(
            "Shoonya token-cache warm-up failed (non-fatal) — the feed will still recover "
            "lazily, just slower.",
            exc_info=True,
        )


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
    — needs them to exist first) and before `resume_strategy_runners`
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


def _rebuild_execution_mock_position_book() -> None:
    """Reconstruct the persistent execution mock's in-memory position book
    from the durable `positions` table — a restart wipes the mock's memory
    while the DB survives, and a paper position open across the restart
    would otherwise leave the mock net short by its lost opening fill,
    firing a permanent `reconciliation_mismatch`. Must run before
    `_run_startup_recovery_check` (which reconciles) and
    `resume_strategy_runners` (which can open new positions).
    """
    from app.core.db.session import session_scope
    from app.modules.execution_engine.paper.registry import (
        rebuild_execution_mock_position_book,
    )

    with session_scope() as db:
        rebuild_execution_mock_position_book(db)


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
    from app.modules.execution_engine.paper.registry import ensure_position_manager_running
    from app.modules.reconciliation.service import run_full_reconciliation

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
            run_full_reconciliation(db, trading_session, ReconciliationTrigger.EVENT)
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
        _attempt_shoonya_reconnect_from_cache()
        _warm_shoonya_token_cache_from_db()
        _sync_mock_instrument_universe()
        _sync_angel_one_scrip_master()
        _rebuild_execution_mock_position_book()
        _run_startup_recovery_check()
        resume_strategy_runners()

        from app.modules.market_data.market_data_scheduler import (
            ensure_market_data_scheduler_running,
        )
        from app.modules.reporting.export_scheduler import (
            ensure_trade_log_export_scheduler_running,
        )
        from app.modules.scheduler.contract_sync_scheduler import (
            ensure_contract_sync_scheduler_running,
        )
        from app.modules.scheduler.health_check import ensure_health_check_scheduler_running
        from app.modules.scheduler.reconciliation_lock_recovery import (
            ensure_reconciliation_lock_recovery_scheduler_running,
        )
        from app.modules.session.bootstrapper import ensure_daily_bootstrap_scheduler_running

        ensure_health_check_scheduler_running()
        ensure_reconciliation_lock_recovery_scheduler_running()
        ensure_market_data_scheduler_running()
        ensure_trade_log_export_scheduler_running()
        ensure_contract_sync_scheduler_running()
        ensure_daily_bootstrap_scheduler_running()
    except Exception:
        from app.core.locking import release_advisory_lock

        release_advisory_lock(singleton_connection, LOCK_PROCESS_SINGLETON)
        singleton_connection.close()
        logger.exception("Startup failed after acquiring the singleton lock — released it.")
        raise

    yield

    from app.core.locking import release_advisory_lock
    from app.modules.execution_engine.paper.registry import stop_all as stop_all_position_managers
    from app.modules.market_data import diagnostic_session
    from app.modules.market_data.market_data_scheduler import stop_market_data_scheduler
    from app.modules.market_data.provider_composition import get_market_data_provider
    from app.modules.market_data.scrip_master_scheduler import (
        stop_scrip_master_refresh_scheduler,
    )
    from app.modules.reporting.export_scheduler import stop_trade_log_export_scheduler
    from app.modules.scheduler.contract_sync_scheduler import stop_contract_sync_scheduler
    from app.modules.scheduler.health_check import stop_health_check_scheduler
    from app.modules.scheduler.reconciliation_lock_recovery import (
        stop_reconciliation_lock_recovery_scheduler,
    )
    from app.modules.session.bootstrapper import stop_daily_bootstrap_scheduler

    stop_all_position_managers()
    stop_health_check_scheduler()
    stop_reconciliation_lock_recovery_scheduler()
    stop_market_data_scheduler()
    stop_scrip_master_refresh_scheduler()
    stop_trade_log_export_scheduler()
    stop_contract_sync_scheduler()
    stop_daily_bootstrap_scheduler()
    diagnostic_session.stop_all()
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
    app.include_router(market_data.router, prefix="/api/v1")
    app.include_router(system_settings.router, prefix="/api/v1")
    # No /api/v1 prefix, deliberately: SHOONYA_REDIRECT_URL (the fixed URL
    # the user registers on Shoonya's own API key form) is
    # http://127.0.0.1:5000/shoonya/callback — mounting under /api/v1 would
    # break that redirect. /login-url and /status live at the same
    # unprefixed path for consistency, not because they need to.
    app.include_router(shoonya.router)
    # Same reasoning as shoonya.router above: ALICEBLUE_REDIRECT_URL
    # (registered in Alice Blue's own portal) is
    # https://.../aliceblue/callback with no /api/v1 in it.
    app.include_router(alice_blue.router)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
