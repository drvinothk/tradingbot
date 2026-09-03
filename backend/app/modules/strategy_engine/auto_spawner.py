"""Ops-Hardening Phase 6 (Auto-Spawner). Bridges the daily session bootstrap
(Phase 4) with actual strategy execution — Phase 4 deliberately stopped at
"today's TradingSession exists," leaving the system with nowhere to trade
until a human manually clicked Start on every strategy. This module closes
that gap: for every `is_enabled` `StrategyConfig`, resolve its underlying's
nearest listed expiry and create a committed `StrategyRun` row.

**Deliberately does not start any runner thread itself.**
`strategy_engine.recovery.resume_strategy_runners` already does exactly that
— "find every non-STOPPED StrategyRun on an ACTIVE session with no live
runner thread, build its Strategy, start the runner + market-data ingestion
+ PositionManager" — and `session.bootstrapper.run_daily_bootstrap` already
calls it immediately after this module runs. That function is agnostic to
*why* a run has no thread (crash-restart or freshly auto-spawned look
identical to it), so duplicating the start/ingestion/position-manager dance
a third time here (start_strategy being the first, resume_strategy_runners
the second) would just be drift risk for zero benefit.

Every failure (missing `underlying_symbol`, unknown instrument, no active
option_contracts, DTE > MAX_DTE, a real BrokerError from the snapshot call)
is a CRITICAL alert (`app.modules.alerting.manager.send_alert`) that skips
*that one* strategy_config, never the whole spawn cycle — one misconfigured
strategy must not block every other enabled one from starting.

**2026-08-17 (Dual-Trigger Model)**: `_spawn_one` now returns a `SpawnOutcome`
instead of `None`, and gained two new skip conditions plus one new caller
shape, to make it safe to call far more often than once a day:

- `_has_run_today` -- distinct from the pre-existing `_has_active_run` check
  (any non-STOPPED run, regardless of date). A login (or any other ambient
  trigger) must never resurrect a strategy that already ran today and was
  deliberately stopped -- whether by a human or by
  `strategy_engine.runner`'s own EOD-stop (15:10 IST, zero positions). The
  *explicit* mid-day "Power toggle -> ON" path (`spawn_one_now`) is the one
  caller that deliberately ignores this (`ignore_stopped_today=True`) --
  flipping that toggle on is a direct command, not an ambient check, and
  should be able to re-arm something stopped earlier the same day.
- A hard stop at `TRADE_WINDOW_END` (15:09 IST) -- past that boundary no new
  trade can ever fire today, so spawning a fresh SCANNING run is pointless
  (and would just get immediately self-stopped by the EOD-stop logic a
  cycle later). Applies to every caller uniformly, including the mid-day
  toggle -- there's no reason for an explicit start to be allowed to spawn
  something that can never trade either.
- Both new checks are read once, cheaply, *before* the network-bound
  option-chain snapshot call (an optimization -- a login that finds every
  strategy already running or already-ran-today must not pay a broker round
  trip for each one) and re-read *inside* `LOCK_EXECUTION_SINGLETON` right
  before the insert (the actual safety net -- two near-simultaneous
  triggers, e.g. the login endpoint firing moments after a manual toggle
  click, must never both pass the early check and create two runs for the
  same strategy_config). Same lock, same "network call outside, decision +
  insert inside" shape `api.v1.strategies.start_strategy` already
  established for its own identical race.

**2026-08-20**: a third gate, `AUTO_SPAWN_EARLIEST_TIME` (08:00 IST) --
live-observed a genuine midnight login auto-spawning every enabled
strategy hours before market data even starts flowing, leaving them idle
all night for nothing. Same "ambient trigger only" shape as the other two:
`spawn_enabled_strategies` (both the login endpoint and the 09:00
scheduler) is gated; `spawn_one_now`'s explicit Power-toggle path ignores
it (`ignore_time_gate=True`), same reasoning as `ignore_stopped_today`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from enum import StrEnum

from sqlalchemy.orm import Session

from app.core.clock import IST, TRADE_WINDOW_END, now_ist
from app.core.locking import LOCK_EXECUTION_SINGLETON, advisory_lock
from app.core.market_utils import is_trading_day
from app.domain.audit.models import ActorType, EventCategory
from app.domain.market.models import Instrument, OptionContract
from app.domain.ops.models import AlertSeverity
from app.domain.session.models import TradingSession
from app.domain.strategy.models import ExecutionMode, StrategyConfig, StrategyRun, StrategyRunStatus
from app.modules.alerting.manager import send_alert
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.base.errors import BrokerError
from app.modules.broker_adapter.composition import get_broker
from app.modules.market_data import record_option_chain_snapshot
from app.modules.market_data.provider_composition import is_market_data_ready
from app.modules.strategy_engine.service import new_strategy_run

logger = logging.getLogger("app.strategy_engine.auto_spawner")

# Real NSE weekly options go illiquid fast once genuinely far out -- this is
# a hard safety ceiling, not a tuning knob, deliberately not exposed as a
# setting.
MAX_DTE = 7

# Matches StartStrategyRequest.interval_seconds's own default -- auto-spawned
# runs get the same cadence a human starting one manually would get.
SPAWN_INTERVAL_SECONDS = 30.0

# 2026-08-20: the login-triggered half of the Dual-Trigger Model
# (`api.v1.sessions.bootstrap_now`) deliberately has no time gate of its own
# -- it exists precisely so a login at any hour gets today's session ready.
# But that meant a genuinely early login (a real midnight one, live-observed)
# auto-spawned every enabled strategy hours before market data even starts
# flowing (`market_data.market_hours`'s own ~08:30 IST connectivity window),
# leaving them sitting SCANNING all night for nothing -- harmless (
# TRADE_WINDOW_START already hard-blocks any entry before 09:31 IST
# regardless of when the run itself was created, and EOD_SCANNING_STOP_TIME
# self-stops an idle one by 15:10) but wasteful. This is the matching early
# bound for that late one, applied only to the *ambient* trigger
# (spawn_enabled_strategies, i.e. both the login endpoint and the 09:00
# scheduler) -- `spawn_one_now`'s explicit Power-toggle path deliberately
# ignores it (`ignore_time_gate=True`), same "a direct human command
# overrides an ambient protection" reasoning already established there for
# `ignore_stopped_today`.
AUTO_SPAWN_EARLIEST_TIME = dt_time(8, 0)

# 2026-08-25: a third ambient trigger closes a real gap the other two never
# covered -- `spawn_enabled_strategies` failing at 09:00 (NO_EXPIRY because
# `option_contracts` wasn't synced yet, or BROKER_ERROR because the option-
# chain snapshot call genuinely failed) both trace back to Shoonya not being
# connected/valid *at that exact moment*. Neither the 09:00 scheduler nor an
# app login retries automatically once a human does the real browser OAuth
# login afterward -- `api.v1.shoonya.oauth_callback` now also calls
# `session.bootstrapper.run_daily_bootstrap()` right after a successful
# login, which is safe to call as often as this because of the exact same
# per-strategy-per-day idempotency (`_has_active_run`/`_has_run_today`) this
# module already relies on for the login trigger.


class SpawnStatus(StrEnum):
    SPAWNED = "spawned"
    ALREADY_ACTIVE = "already_active"
    ALREADY_RAN_TODAY = "already_ran_today"
    NOT_TRADING_DAY = "not_trading_day"
    TOO_EARLY = "too_early"
    TRADE_WINDOW_CLOSED = "trade_window_closed"
    NO_UNDERLYING = "no_underlying"
    UNKNOWN_INSTRUMENT = "unknown_instrument"
    NO_EXPIRY = "no_expiry"
    DTE_EXCEEDED = "dte_exceeded"
    BROKER_ERROR = "broker_error"


class SpawnOutcome:
    def __init__(self, status: SpawnStatus, detail: str, run: StrategyRun | None = None) -> None:
        self.status = status
        self.detail = detail
        self.run = run


def _has_active_run(db: Session, strategy_config_id: uuid.UUID) -> bool:
    return (
        db.query(StrategyRun.id)
        .filter(
            StrategyRun.strategy_config_id == strategy_config_id,
            StrategyRun.status != StrategyRunStatus.STOPPED,
        )
        .first()
        is not None
    )


def _has_run_today(db: Session, strategy_config_id: uuid.UUID, today: date) -> bool:
    """Any run at all -- STOPPED included -- with `started_at` falling on
    `today`'s IST calendar date. A UTC range query (not a per-row Python
    `to_ist` filter) since IST midnight doesn't line up with UTC midnight;
    computing the IST day's UTC bounds once and filtering on the indexed
    `started_at` column is both correct and doesn't require fetching this
    config's whole run history.
    """
    start_utc = datetime.combine(today, dt_time.min, tzinfo=IST).astimezone(UTC)
    end_utc = start_utc + timedelta(days=1)
    return (
        db.query(StrategyRun.id)
        .filter(
            StrategyRun.strategy_config_id == strategy_config_id,
            StrategyRun.started_at >= start_utc,
            StrategyRun.started_at < end_utc,
        )
        .first()
        is not None
    )


def _skip_reason(
    db: Session, strategy_config_id: uuid.UUID, today: date, ignore_stopped_today: bool
) -> SpawnOutcome | None:
    if _has_active_run(db, strategy_config_id):
        return SpawnOutcome(SpawnStatus.ALREADY_ACTIVE, "Strategy already has an active run.")
    if not ignore_stopped_today and _has_run_today(db, strategy_config_id, today):
        return SpawnOutcome(
            SpawnStatus.ALREADY_RAN_TODAY,
            "Strategy already ran today and was stopped -- not auto-resuming it "
            "(use the Power toggle to restart it explicitly).",
        )
    return None


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


def _alert(db: Session, trading_session: TradingSession, category: str, message: str) -> None:
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
    *,
    ignore_stopped_today: bool = False,
    ignore_time_gate: bool = False,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_id: uuid.UUID | None = None,
) -> SpawnOutcome:
    early_skip = _skip_reason(db, config.id, today, ignore_stopped_today)
    if early_skip is not None:
        return early_skip

    if not is_trading_day(today):
        return SpawnOutcome(
            SpawnStatus.NOT_TRADING_DAY, f"{today.isoformat()} is not a trading day."
        )

    if not ignore_time_gate and now_ist().time() < AUTO_SPAWN_EARLIEST_TIME:
        return SpawnOutcome(
            SpawnStatus.TOO_EARLY,
            f"Auto-spawn not allowed before {AUTO_SPAWN_EARLIEST_TIME.strftime('%H:%M')} IST -- "
            "will spawn automatically once that time passes (next login or the 09:00 scheduler).",
        )

    if now_ist().time() >= TRADE_WINDOW_END:
        return SpawnOutcome(
            SpawnStatus.TRADE_WINDOW_CLOSED,
            f"Trade entry window closed for today ({TRADE_WINDOW_END.strftime('%H:%M')} IST) -- "
            "will start automatically tomorrow.",
        )

    if not config.underlying_symbol:
        detail = (
            f"strategy_config {config.id} ({config.name}) is enabled but has no "
            "underlying_symbol configured -- cannot auto-spawn. Set it via "
            "PATCH /strategies/{id}."
        )
        _alert(db, trading_session, "auto_spawn_no_underlying", detail)
        return SpawnOutcome(SpawnStatus.NO_UNDERLYING, detail)

    instrument = (
        db.query(Instrument).filter(Instrument.symbol == config.underlying_symbol).one_or_none()
    )
    if instrument is None:
        detail = (
            f"strategy_config {config.id} ({config.name}) has underlying_symbol "
            f"{config.underlying_symbol!r}, but no matching Instrument row exists."
        )
        _alert(db, trading_session, "auto_spawn_unknown_instrument", detail)
        return SpawnOutcome(SpawnStatus.UNKNOWN_INSTRUMENT, detail)

    if instrument.id not in expiry_cache:
        expiry_cache[instrument.id] = resolve_nearest_expiry(db, instrument.id, today)
    expiry = expiry_cache[instrument.id]

    if expiry is None:
        detail = (
            f"No active option_contracts found for instrument {instrument.symbol} -- "
            f"cannot resolve nearest expiry. strategy_config {config.id} ({config.name}) "
            "not spawned."
        )
        _alert(db, trading_session, "auto_spawn_no_expiry", detail)
        return SpawnOutcome(SpawnStatus.NO_EXPIRY, detail)

    dte = (expiry - today).days
    if dte > MAX_DTE:
        detail = (
            f"Nearest expiry for {instrument.symbol} is {expiry.isoformat()} (DTE={dte}), "
            f"which exceeds the {MAX_DTE}-day illiquid-monthly-option guard. "
            f"strategy_config {config.id} ({config.name}) not spawned."
        )
        _alert(db, trading_session, "auto_spawn_dte_exceeded", detail)
        return SpawnOutcome(SpawnStatus.DTE_EXCEEDED, detail)

    # One immediate option-chain snapshot per (instrument, expiry) this
    # cycle, mirroring start_strategy's own "validate before creating the
    # row" discipline -- a real BrokerError here means no zombie StrategyRun
    # gets created. Skipped entirely (not attempted against whatever
    # get_broker() would otherwise silently fall back to) when the broker
    # isn't connected yet at 09:00 -- same precedent
    # strategy_engine.recovery.resume_strategy_runners already established:
    # the run still gets created and its runner thread still starts, just
    # idle, until a human reconnects.
    if is_market_data_ready():
        snapshot_key = (instrument.id, expiry)
        if snapshot_key not in snapshotted:
            try:
                record_option_chain_snapshot(instrument.id, get_broker(), instrument.symbol, expiry)
                snapshotted.add(snapshot_key)
            except BrokerError as exc:
                detail = (
                    f"Could not fetch option chain for {instrument.symbol} {expiry.isoformat()}: "
                    f"{exc}. strategy_config {config.id} ({config.name}) not spawned."
                )
                _alert(db, trading_session, "auto_spawn_broker_error", detail)
                return SpawnOutcome(SpawnStatus.BROKER_ERROR, detail)
    else:
        logger.warning(
            "Auto-spawner: Shoonya not connected -- spawning strategy_config %s (%s) idle, "
            "no ingestion until reconnect",
            config.id,
            config.name,
        )

    # The actual race guard -- see this module's own docstring ("Dual-Trigger
    # Model") for why an early, unlocked _skip_reason check above isn't
    # enough on its own: two near-simultaneous triggers could both pass it
    # before either commits. Re-checked here, immediately before the insert,
    # under the same lock api.v1.strategies.start_strategy already uses for
    # its own identical "at most one active run per strategy" race.
    with advisory_lock(db, LOCK_EXECUTION_SINGLETON):
        race_skip = _skip_reason(db, config.id, today, ignore_stopped_today)
        if race_skip is not None:
            return race_skip

        # actor_id may be a real human (mid-day Power toggle) or None
        # (ambient cron/login path) -- when None, inherit the session's
        # own creator, same precedent session.bootstrapper
        # ._bootstrap_workspace already established for
        # TradingSession.started_by_user_id rather than requiring a
        # schema change/system-user sentinel.
        run = new_strategy_run(
            strategy_config_id=config.id,
            trading_session_id=trading_session.id,
            execution_mode=ExecutionMode.AUTO,
            started_by_user_id=actor_id or trading_session.started_by_user_id,
            instrument_id=instrument.id,
            expiry_date=expiry,
            interval_seconds=SPAWN_INTERVAL_SECONDS,
        )
        db.add(run)
        db.flush()

        record_event(
            db,
            workspace_id=trading_session.workspace_id,
            actor_type=actor_type,
            actor_id=actor_id,
            event_category=EventCategory.STRATEGY_STATE_CHANGE,
            event_type="strategy_run.auto_spawned",
            entity_type="strategy_run",
            entity_id=run.id,
            trading_session_id=trading_session.id,
            strategy_config_id=config.id,
            payload={
                "instrument_id": str(instrument.id),
                "expiry_date": expiry.isoformat(),
                "dte": dte,
            },
        )

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
    return SpawnOutcome(SpawnStatus.SPAWNED, "Spawned.", run=run)


def spawn_one_now(
    db: Session,
    trading_session: TradingSession,
    config: StrategyConfig,
    *,
    actor_id: uuid.UUID | None = None,
) -> SpawnOutcome:
    """The mid-day "Power toggle -> ON" entry point (`api.v1.strategies`'
    `POST /strategies/{id}/power`) -- unlike `spawn_enabled_strategies` (the
    ambient cron/login path), this deliberately ignores whether the strategy
    already ran-and-stopped earlier today (`ignore_stopped_today=True`): a
    human flipping this toggle on is a direct, explicit command to start it
    now, not an ambient check that must never resurrect something already
    stopped for a reason. Audited as `ActorType.USER` under `actor_id`
    (the clicking human), not `SYSTEM` -- distinct from every other caller
    of `_spawn_one`.
    """
    today = now_ist().date()
    return _spawn_one(
        db,
        trading_session,
        config,
        today,
        {},
        set(),
        ignore_stopped_today=True,
        ignore_time_gate=True,
        actor_type=ActorType.USER,
        actor_id=actor_id,
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
    # Skip reasons that already fired their own CRITICAL alert (via _alert
    # inside _spawn_one) -- logging those again here at INFO would just be
    # noise on top of the real signal.
    _already_alerted = {
        SpawnStatus.NO_UNDERLYING,
        SpawnStatus.UNKNOWN_INSTRUMENT,
        SpawnStatus.NO_EXPIRY,
        SpawnStatus.DTE_EXCEEDED,
        SpawnStatus.BROKER_ERROR,
    }
    for config in configs:
        try:
            outcome = _spawn_one(db, trading_session, config, today, expiry_cache, snapshotted)
            if outcome.status not in (SpawnStatus.SPAWNED, *_already_alerted):
                logger.info(
                    "Auto-spawner: strategy_config %s -- %s (%s)",
                    config.id,
                    outcome.status.value,
                    outcome.detail,
                )
        except Exception:  # noqa: BLE001 - one bad config must never abort the rest
            logger.exception(
                "Auto-spawner: unexpected error spawning strategy_config %s -- skipping", config.id
            )
