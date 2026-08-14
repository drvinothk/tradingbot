"""Ops-Hardening Phase 6 (Auto-Spawner). Bridges the daily session bootstrap
(Phase 4) with actual strategy execution — Phase 4 deliberately stopped at
"today's TradingSession exists," leaving the system with nowhere to trade
until a human manually clicked Start on every strategy. This module closes
that gap: for every `is_enabled` `StrategyConfig`, resolve its underlying's
nearest listed expiry and create a committed `StrategyRun` row.

**Deliberately does not start any runner thread itself.** `app.main.
_resume_strategy_runners` already does exactly that — "find every non-STOPPED
StrategyRun on an ACTIVE session with no live runner thread, build its
Strategy, start the runner + market-data ingestion + PositionManager" — and
`session.bootstrapper.run_daily_bootstrap` already calls it immediately after
this module runs. That function is agnostic to *why* a run has no thread
(crash-restart or freshly auto-spawned look identical to it), so duplicating
the start/ingestion/position-manager dance a third time here (start_strategy
being the first, _resume_strategy_runners the second) would just be drift
risk for zero benefit.

Every failure (missing `underlying_symbol`, unknown instrument, no active
option_contracts, DTE > MAX_DTE, a real BrokerError from the snapshot call)
is a CRITICAL alert (`app.modules.alerting.manager.send_alert`) that skips
*that one* strategy_config, never the whole spawn cycle — one misconfigured
strategy must not block every other enabled one from starting.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.core.market_utils import is_trading_day
from app.domain.market.models import Instrument, OptionContract
from app.domain.ops.models import AlertSeverity
from app.domain.session.models import TradingSession
from app.domain.strategy.models import ExecutionMode, StrategyConfig, StrategyRun, StrategyRunStatus
from app.modules.alerting.manager import send_alert
from app.modules.broker_adapter.base.errors import BrokerError
from app.modules.broker_adapter.composition import get_broker
from app.modules.market_data import record_option_chain_snapshot
from app.modules.market_data.provider_composition import is_shoonya_market_data_ready

logger = logging.getLogger("app.strategy_engine.auto_spawner")

# Real NSE weekly options go illiquid fast once genuinely far out -- this is
# a hard safety ceiling, not a tuning knob (mirrors the 1-lot hardcap's own
# "defense in depth, not configurable" reasoning from Ops-Hardening Phase 5).
MAX_DTE = 7

# Matches StartStrategyRequest.interval_seconds's own default -- auto-spawned
# runs get the same cadence a human starting one manually would get.
SPAWN_INTERVAL_SECONDS = 30.0


def resolve_nearest_expiry(db: Session, instrument_id: uuid.UUID, today: date) -> date | None:
    """Nearest active, unexpired listed expiry from locally-synced
    `option_contracts` -- deliberately a DB read, not a live GetOptionChain
    call, so a 09:00 auto-spawn cycle doesn't depend on broker connectivity/
    rate limits to even begin. A stale-by-a-day scrip-master sync is an
    acceptable risk here; a broker outage silently blocking every strategy
    from ever starting is not. Returns `None` if no active contract exists
    at/after `today` for this instrument.
    """
    row = (
        db.query(OptionContract.expiry_date)
        .filter(
            OptionContract.instrument_id == instrument_id,
            OptionContract.is_active.is_(True),
            OptionContract.expiry_date >= today,
        )
        .order_by(OptionContract.expiry_date.asc())
        .first()
    )
    return row[0] if row is not None else None


def _alert(
    db: Session, trading_session: TradingSession, category: str, message: str
) -> None:
    logger.error("Auto-spawner: %s", message)
    send_alert(
        db,
        workspace_id=trading_session.workspace_id,
        severity=AlertSeverity.CRITICAL,
        category=category,
        message=message,
        trading_session_id=trading_session.id,
    )


def _spawn_one(
    db: Session,
    trading_session: TradingSession,
    config: StrategyConfig,
    today: date,
    expiry_cache: dict[uuid.UUID, date | None],
    snapshotted: set[tuple[uuid.UUID, date]],
) -> None:
    existing_run = (
        db.query(StrategyRun)
        .filter(
            StrategyRun.strategy_config_id == config.id,
            StrategyRun.status != StrategyRunStatus.STOPPED,
        )
        .one_or_none()
    )
    if existing_run is not None:
        logger.info(
            "Auto-spawner: strategy_config %s already has an active run (%s) -- skipping",
            config.id,
            existing_run.id,
        )
        return

    if not config.underlying_symbol:
        _alert(
            db,
            trading_session,
            "auto_spawn_no_underlying",
            f"strategy_config {config.id} ({config.name}) is enabled but has no "
            "underlying_symbol configured -- cannot auto-spawn. Set it via "
            "PATCH /strategies/{id}.",
        )
        return

    instrument = (
        db.query(Instrument).filter(Instrument.symbol == config.underlying_symbol).one_or_none()
    )
    if instrument is None:
        _alert(
            db,
            trading_session,
            "auto_spawn_unknown_instrument",
            f"strategy_config {config.id} ({config.name}) has underlying_symbol "
            f"{config.underlying_symbol!r}, but no matching Instrument row exists.",
        )
        return

    if instrument.id not in expiry_cache:
        expiry_cache[instrument.id] = resolve_nearest_expiry(db, instrument.id, today)
    expiry = expiry_cache[instrument.id]

    if expiry is None:
        _alert(
            db,
            trading_session,
            "auto_spawn_no_expiry",
            f"No active option_contracts found for instrument {instrument.symbol} -- "
            f"cannot resolve nearest expiry. strategy_config {config.id} ({config.name}) "
            "not spawned.",
        )
        return

    dte = (expiry - today).days
    if dte > MAX_DTE:
        _alert(
            db,
            trading_session,
            "auto_spawn_dte_exceeded",
            f"Nearest expiry for {instrument.symbol} is {expiry.isoformat()} (DTE={dte}), "
            f"which exceeds the {MAX_DTE}-day illiquid-monthly-option guard. "
            f"strategy_config {config.id} ({config.name}) not spawned.",
        )
        return

    # One immediate option-chain snapshot per (instrument, expiry) this
    # cycle, mirroring start_strategy's own "validate before creating the
    # row" discipline -- a real BrokerError here means no zombie StrategyRun
    # gets created. Skipped entirely (not attempted against whatever
    # get_broker() would otherwise silently fall back to) when Shoonya isn't
    # connected yet at 09:00 -- same precedent app.main._resume_strategy_
    # runners already established: the run still gets created and its
    # runner thread still starts, just idle, until a human reconnects.
    if is_shoonya_market_data_ready():
        snapshot_key = (instrument.id, expiry)
        if snapshot_key not in snapshotted:
            try:
                record_option_chain_snapshot(instrument.id, get_broker(), instrument.symbol, expiry)
                snapshotted.add(snapshot_key)
            except BrokerError as exc:
                _alert(
                    db,
                    trading_session,
                    "auto_spawn_broker_error",
                    f"Could not fetch option chain for {instrument.symbol} {expiry.isoformat()}: "
                    f"{exc}. strategy_config {config.id} ({config.name}) not spawned.",
                )
                return
    else:
        logger.warning(
            "Auto-spawner: Shoonya not connected -- spawning strategy_config %s (%s) idle, "
            "no ingestion until reconnect",
            config.id,
            config.name,
        )

    run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC),
        # No human triggered this -- inherit the session's own creator,
        # same precedent session.bootstrapper._bootstrap_workspace already
        # established for TradingSession.started_by_user_id rather than
        # requiring a schema change/system-user sentinel.
        started_by_user_id=trading_session.started_by_user_id,
        instrument_id=instrument.id,
        expiry_date=expiry,
        interval_seconds=SPAWN_INTERVAL_SECONDS,
    )
    db.add(run)
    db.flush()

    logger.warning(
        "Auto-spawner: created strategy_run %s for strategy_config %s (%s) on %s expiry %s "
        "(DTE=%d)",
        run.id,
        config.id,
        config.name,
        instrument.symbol,
        expiry.isoformat(),
        dte,
    )


def spawn_enabled_strategies(db: Session, trading_session: TradingSession, today: date) -> None:
    """Creates a `StrategyRun` row for every `is_enabled` `StrategyConfig` in
    `trading_session`'s workspace that doesn't already have one -- called
    once per workspace, immediately after that workspace's today's
    TradingSession is resolved (see `session.bootstrapper.run_daily_bootstrap`).
    No-ops entirely on a non-trading day.
    """
    if not is_trading_day(today):
        logger.info(
            "Auto-spawner: %s is not a trading day (weekend) -- skipping strategy spawn.",
            today.isoformat(),
        )
        return

    configs = (
        db.query(StrategyConfig)
        .filter(
            StrategyConfig.workspace_id == trading_session.workspace_id,
            StrategyConfig.is_enabled.is_(True),
        )
        .all()
    )
    if not configs:
        logger.info(
            "Auto-spawner: no enabled strategy_configs for workspace %s",
            trading_session.workspace_id,
        )
        return

    expiry_cache: dict[uuid.UUID, date | None] = {}
    snapshotted: set[tuple[uuid.UUID, date]] = set()
    for config in configs:
        try:
            _spawn_one(db, trading_session, config, today, expiry_cache, snapshotted)
        except Exception:  # noqa: BLE001 - one bad config must never abort the rest
            logger.exception(
                "Auto-spawner: unexpected error spawning strategy_config %s -- skipping", config.id
            )
