"""Strategy lifecycle + trade-approval decisions. `_RUNNERS` is a plain
in-process dict, not a DB table — safe because this backend only ever runs
as a single process (the process-singleton lock in app.main enforces that),
so there is exactly one place a `StrategyRunner` thread could be tracked. A
restart loses the in-memory registry the same way it loses any other
in-process thread; `strategy_runs` rows left non-stopped after a crash are
the DB-visible signal of that, same shape as the existing startup-recovery
check for trading_sessions.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.clock import now_ist, to_ist
from app.core.db.base import utcnow as _utcnow
from app.core.db.session import get_db
from app.core.locking import LOCK_EXECUTION_SINGLETON, advisory_lock
from app.core.security.rbac import require_permission
from app.core.sleep_inhibitor import get_sleep_inhibitor
from app.domain.audit.models import ActorType, EventCategory
from app.domain.identity.models import User
from app.domain.market.models import Instrument, QuoteTick
from app.domain.session.models import TradingSession, TradingSessionStatus
from app.domain.strategy.models import (
    ApprovalStatus,
    ExecutionMode,
    PendingTradeApproval,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    StrategyRuntimeMode,
    StrategyStatus,
    TradeIntent,
    TradeIntentStatus,
)
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.base.errors import BrokerError
from app.modules.broker_adapter.composition import get_broker
from app.modules.execution_engine.paper.registry import ensure_position_manager_running
from app.modules.execution_engine.paper.service import dispatch_trade_intent
from app.modules.market_data import record_option_chain_snapshot
from app.modules.market_data.freshness import (
    PRICE_DRIFT_TOLERANCE_PCT,
    check_price_drift,
    classify_latest_tick,
    classify_option_chain,
    worse_of,
)
from app.modules.market_data.provider_composition import is_market_data_ready
from app.modules.market_data.registry import ensure_ingestion_running
from app.modules.strategy_engine.auto_spawner import SpawnStatus, spawn_one_now
from app.modules.strategy_engine.common_rules import get_open_position_for_run
from app.modules.strategy_engine.interface import Strategy
from app.modules.strategy_engine.runner import StrategyRunner
from app.modules.strategy_engine.service import new_strategy_run
from app.modules.strategy_engine.strategies import (
    ATR_BREAKOUT_PARAM_KEYS,
    CONVICTION_PARAM_KEYS,
    ATRBreakoutStrategy,
    EMAMicroPullbackStrategy,
    LiquiditySweepReversalStrategy,
    OIVolumeConfirmedStrategy,
    ORBConvictionStrategy,
    ORBStrategy,
    SyntheticStrategy,
    VWAPPullbackStrategy,
)

router = APIRouter(tags=["strategies"])

# strategy_run_id -> live runner thread. See module docstring.
_RUNNERS: dict[uuid.UUID, StrategyRunner] = {}


ORB_PARAM_KEYS = {
    "qty_lots",
    "or_minutes",
    "stop_pct",
    "target_pct",
    "trail_activation_fraction",
    "trail_lock_fraction",
    "orb_entry_cutoff_time",
    "min_or_range_nifty_points",
    "max_or_range_nifty_points",
    "min_or_range_banknifty_points",
    "max_or_range_banknifty_points",
    "structure_break_atr_multiplier",
    "structure_break_persistence_seconds",
}
# Deliberately NOT in the allowlist above: enabled_on_expiry_day,
# expiry_orb_entry_cutoff_time, expiry_strike_bias. Phase 2 stores these in
# strategy_configs.params inertly for Phase 3 to read later -- ORBStrategy's
# constructor has no matching kwargs for them, so forwarding them here would
# raise a TypeError at start_strategy time instead of leaving them as inert
# config, defeating the point.

# orb_conviction = every ORB param plus the conviction-gate tunables that
# subclass adds (CONVICTION_PARAM_KEYS is that subclass's own explicit
# literal, imported so the two never drift).
ORB_CONVICTION_PARAM_KEYS = ORB_PARAM_KEYS | CONVICTION_PARAM_KEYS

VWAP_PULLBACK_PARAM_KEYS = {
    "qty_lots",
    "pullback_tolerance_frac",
    "stop_pct",
    "target_pct",
    "trail_activation_fraction",
    "trail_lock_fraction",
    "trend_lookback_bars",
    "max_vwap_crosses_in_lookback",
    "min_trend_side_fraction",
    "structure_break_atr_multiplier",
    "structure_break_persistence_seconds",
}
# Its own explicit literal, not `= VWAP_PULLBACK_PARAM_KEYS` — that alias
# was only ever safe because both strategies happened to accept an
# identical key set. EMAMicroPullbackStrategy doesn't accept the three
# VWAP-only trend/choppiness keys above; aliasing would let those leak into
# an EMA strategy_config's allowlist and raise a TypeError at
# start_strategy time. No `pullback_tolerance_frac` here (unlike VWAP) --
# EMAMicroPullbackStrategy's Bone Zone pullback replaced the old
# touch_and_confirm-based entry logic, and there's no tolerance band left
# to configure.
EMA_MICRO_PULLBACK_PARAM_KEYS = {
    "qty_lots",
    "stop_pct",
    "target_pct",
    "trail_activation_fraction",
    "trail_lock_fraction",
    "ema_expansion_lookback",
    "min_body_ratio",
    "ema_morning_window_start",
    "ema_morning_window_end",
    "ema_afternoon_window_start",
    "ema_afternoon_window_end",
    "ema_max_trades_per_session",
    "structure_break_atr_multiplier",
    "structure_break_persistence_seconds",
}
# Deliberately NOT in the allowlist above: ema_expiry_time_decay_exit,
# ema_expiry_time_decay_bars, ema_expiry_quick_exit_rr -- stored in
# strategy_configs.params inertly for a future phase to read, same
# "no matching constructor kwarg, so don't forward it" reasoning as ORB's
# own expiry-day-only config hooks above.
OI_VOLUME_CONFIRMED_PARAM_KEYS = {
    "qty_lots",
    "lookback_bars",
    "stop_pct",
    "target_pct",
    "trail_activation_fraction",
    "trail_lock_fraction",
    "oi_use_futures_volume_confirmation",
    "oi_futures_volume_multiplier",
    "oi_use_atm_oi_buildup",
    "min_range_nifty_points",
    "max_range_nifty_points",
    "min_range_banknifty_points",
    "max_range_banknifty_points",
    "min_body_ratio",
    "oi_morning_window_start",
    "oi_morning_window_end",
    "oi_afternoon_window_start",
    "oi_afternoon_window_end",
    "oi_max_trades_per_session",
    "structure_break_atr_multiplier",
    "structure_break_persistence_seconds",
}
# Its own explicit literal, not `= OI_VOLUME_CONFIRMED_PARAM_KEYS` -- that
# alias was only ever safe because both strategies happened to accept an
# identical 5-key set. LiquiditySweepReversalStrategy is a genuinely
# different pattern (break-and-reverse, no _fired_directions cap -- see its
# own module docstring); its range-width/distance/body-ratio/time-window
# params below are its own independent config (same pick_by_underlying
# *shape* as OI/Volume Confirmed's, different key names and defaults, since
# a 10-bar rolling window behaves differently from a 5-bar one) -- aliasing
# either strategy's key set to the other's would leak keys the other
# constructor doesn't accept and raise a TypeError at start_strategy time.
LIQUIDITY_SWEEP_REVERSAL_PARAM_KEYS = {
    "qty_lots",
    "lookback_bars",
    "stop_pct",
    "target_pct",
    "trail_activation_fraction",
    "trail_lock_fraction",
    "min_sweep_distance_nifty_points",
    "min_sweep_distance_banknifty_points",
    "sweep_min_range_width_nifty_points",
    "sweep_max_range_width_nifty_points",
    "sweep_min_range_width_banknifty_points",
    "sweep_max_range_width_banknifty_points",
    "min_body_ratio",
    "sweep_morning_window_start",
    "sweep_morning_window_end",
    "sweep_afternoon_window_start",
    "sweep_afternoon_window_end",
    "sweep_max_trades_per_session",
    "structure_break_atr_multiplier",
    "structure_break_persistence_seconds",
}
# Deliberately NOT in the allowlist above: any sweep_expiry_* hooks a
# future phase adds -- same "inert JSON, no matching constructor kwarg"
# reasoning as ORB's own expiry-day-only config hooks.


# 2026-08-24: qty_lots was a hardcoded `QTY_LOTS = 1` module constant in
# every strategy file -- no config surface at all, unlike every other
# tunable (stop_pct, target_pct, ...) which already flows through
# `strategy_config.params`. Removed in favor of a real, per-strategy
# `qty_lots` param (added to each *_PARAM_KEYS allowlist above) with a
# mode-aware default so an operator who never touches it keeps today's
# conservative live behavior automatically -- explicit user request:
# "default will be 1 lot for live trading, and 10 lots for paper trading...
# if I dont edit, 1 lot stays as default, hence the risk is also managed
# there." Mirrors the "overrides only ever restrict, never expand"
# philosophy `StrategyRuntimeMode`/`SafeMode` already use elsewhere in this
# codebase: err toward the paper (larger-lot, but risk-service-exempt --
# see risk_engine.service's mode-aware rule) default whenever a strategy
# isn't unambiguously graduated to real-money LIVE status, rather than try
# to perfectly resolve live-ness from the session's own mode machinery too.
_DEFAULT_QTY_LOTS_LIVE = 1
_DEFAULT_QTY_LOTS_PAPER = 10


def _default_qty_lots(strategy_config: StrategyConfig) -> int:
    is_paper = (
        strategy_config.runtime_mode == StrategyRuntimeMode.FORCE_PAPER
        or strategy_config.status != StrategyStatus.LIVE
    )
    return _DEFAULT_QTY_LOTS_PAPER if is_paper else _DEFAULT_QTY_LOTS_LIVE


def _build_strategy(
    strategy_config: StrategyConfig, instrument_id: uuid.UUID, expiry_date: date
) -> Strategy:
    """Maps `strategy_config.strategy_type` to its `Strategy` class, reading
    that strategy's own tunables from `strategy_config.params` (missing keys
    fall back to each strategy's own constructor defaults, except
    `qty_lots` which falls back to `_default_qty_lots` instead of each
    strategy class's own conservative `1` -- see that function's own
    docstring) — the only place in the codebase that needs to know all six
    concrete strategy types.
    """
    params = dict(strategy_config.params or {})
    params.setdefault("qty_lots", _default_qty_lots(strategy_config))
    strategy_type = strategy_config.strategy_type

    if strategy_type == "synthetic":
        return SyntheticStrategy(instrument_id=instrument_id, expiry_date=expiry_date)
    if strategy_type == "orb":
        return ORBStrategy(
            instrument_id=instrument_id,
            expiry_date=expiry_date,
            **{k: v for k, v in params.items() if k in ORB_PARAM_KEYS},
        )
    if strategy_type == "orb_conviction":
        return ORBConvictionStrategy(
            instrument_id=instrument_id,
            expiry_date=expiry_date,
            **{k: v for k, v in params.items() if k in ORB_CONVICTION_PARAM_KEYS},
        )
    if strategy_type == "atr_breakout":
        return ATRBreakoutStrategy(
            instrument_id=instrument_id,
            expiry_date=expiry_date,
            **{k: v for k, v in params.items() if k in ATR_BREAKOUT_PARAM_KEYS},
        )
    if strategy_type == "vwap_pullback":
        return VWAPPullbackStrategy(
            instrument_id=instrument_id,
            expiry_date=expiry_date,
            **{k: v for k, v in params.items() if k in VWAP_PULLBACK_PARAM_KEYS},
        )
    if strategy_type == "ema_micro_pullback":
        return EMAMicroPullbackStrategy(
            instrument_id=instrument_id,
            expiry_date=expiry_date,
            **{k: v for k, v in params.items() if k in EMA_MICRO_PULLBACK_PARAM_KEYS},
        )
    if strategy_type == "oi_volume_confirmed":
        return OIVolumeConfirmedStrategy(
            instrument_id=instrument_id,
            expiry_date=expiry_date,
            **{k: v for k, v in params.items() if k in OI_VOLUME_CONFIRMED_PARAM_KEYS},
        )
    if strategy_type == "liquidity_sweep_reversal":
        return LiquiditySweepReversalStrategy(
            instrument_id=instrument_id,
            expiry_date=expiry_date,
            **{k: v for k, v in params.items() if k in LIQUIDITY_SWEEP_REVERSAL_PARAM_KEYS},
        )
    raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown strategy_type '{strategy_type}'")


KNOWN_STRATEGY_TYPES = {
    "synthetic",
    "orb",
    "orb_conviction",
    "atr_breakout",
    "vwap_pullback",
    "ema_micro_pullback",
    "oi_volume_confirmed",
    "liquidity_sweep_reversal",
}


def _start_runner_thread(
    run: StrategyRun,
    strategy_config: StrategyConfig,
    trading_session: TradingSession,
    instrument: Instrument,
    expiry_date: date,
    interval_seconds: float,
) -> None:
    """The post-commit half of starting a strategy run -- sleep inhibitor,
    market-data ingestion, the actual runner thread, PositionManager.
    Shared by `start_strategy` (human-supplied instrument/expiry/session)
    and `set_strategy_power`'s auto-resolved "Power toggle -> ON" path, so
    the two can never drift out of sync on what "actually running"
    requires. `run` must already be committed -- both callers commit their
    own row-creation transaction before calling this.
    """
    # Sleep inhibitor: "actively scanning" half of the two overlapping
    # lifecycles core/sleep_inhibitor.py's own docstring describes (the
    # other half is an open position, acquired/released around
    # _open_position_from_fill/close_position). Reference-counted, so a
    # session with several concurrent runs stays awake until every one of
    # them has stopped.
    get_sleep_inhibitor().acquire(f"strategy_run:{run.id}")

    # MarketDataIngestionService/IndicatorEngine were built in Phase 1 but
    # nothing ever actually started one outside tests — real strategies
    # (unlike the synthetic stub) need genuinely live price_bars/
    # indicator_snapshots for their underlying. One shared service for the
    # whole process (see market_data.registry's own docstring for why: a
    # broker connection is a single shared stream, not one per instrument),
    # idempotent per symbol so several concurrent runs on the same or
    # different underlyings all share it.
    ensure_ingestion_running(instrument.symbol)

    strategy = _build_strategy(strategy_config, instrument.id, expiry_date)
    runner = StrategyRunner(
        strategy,
        run.id,
        interval_seconds=interval_seconds,
        on_self_stop=lambda: _RUNNERS.pop(run.id, None),
    )
    runner.start()
    _RUNNERS[run.id] = runner

    # PositionManager is per trading_session, not per strategy_run — a
    # session that already has one running (e.g. a second strategy started
    # against it) is left alone; ensure_position_manager_running no-ops in
    # that case. It's deliberately not stopped by _stop_active_run below: an
    # already-open position from this run must keep being managed to its
    # stop/target even after the strategy that opened it stops scanning.
    ensure_position_manager_running(trading_session.id)


def _stop_active_run(
    db: Session,
    run: StrategyRun,
    strategy_config: StrategyConfig,
    *,
    actor_type: ActorType,
    actor_id: uuid.UUID | None,
) -> None:
    """Shared by `stop_strategy` (human clicks Stop) and
    `set_strategy_power`'s "Power toggle -> OFF" path. Caller commits
    afterward.
    """
    runner = _RUNNERS.pop(run.id, None)
    if runner is not None:
        runner.stop()

    # Releases this run's half of the sleep inhibitor's reference count —
    # see the matching acquire in _start_runner_thread. Safe even if this
    # run never acquired it (e.g. a process restart between start and
    # stop): SleepInhibitor.release on an absent reason is a no-op.
    get_sleep_inhibitor().release(f"strategy_run:{run.id}")

    run.status = StrategyRunStatus.STOPPED
    run.stopped_at = _utcnow()
    db.add(run)
    db.flush()

    record_event(
        db,
        workspace_id=strategy_config.workspace_id,
        actor_type=actor_type,
        actor_id=actor_id,
        event_category=EventCategory.STRATEGY_STATE_CHANGE,
        event_type="strategy_run.stopped",
        entity_type="strategy_run",
        entity_id=run.id,
        trading_session_id=run.trading_session_id,
        strategy_config_id=strategy_config.id,
    )


class StrategyConfigOut(BaseModel):
    id: uuid.UUID
    name: str
    strategy_type: str
    params: dict
    status: str
    is_enabled: bool
    runtime_mode: str | None
    underlying_symbol: str | None

    model_config = {"from_attributes": True}


def _validate_underlying_symbol_or_404(db: Session, underlying_symbol: str) -> None:
    exists = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one_or_none()
    if exists is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"No Instrument found for underlying_symbol {underlying_symbol!r}",
        )


class CreateStrategyRequest(BaseModel):
    # Optional as of 2026-08-20 -- a caller that omits it gets a
    # server-generated default (see _generate_strategy_name below);
    # anyone who still passes it explicitly is accepted as-is, unchanged,
    # for backward compatibility.
    name: str | None = None
    strategy_type: str = "synthetic"
    params: dict = {}
    underlying_symbol: str | None = None


@router.get("/strategies", response_model=list[StrategyConfigOut])
def list_strategies(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.view")),
) -> list[StrategyConfig]:
    return (
        db.query(StrategyConfig)
        .filter(StrategyConfig.workspace_id == user.workspace_id)
        .order_by(StrategyConfig.name)
        .all()
    )


def _generate_strategy_name(db: Session, workspace_id: uuid.UUID, strategy_type: str) -> str:
    """Server-side default when a caller omits `name` entirely -- derived
    from `strategy_type` plus a short random suffix for uniqueness (not a
    sequential counter, which would need a read-then-write that could race
    under concurrent creates). Loops on the vanishingly unlikely collision
    rather than trusting the suffix alone, same defensive style as this
    endpoint's own explicit-name 409 check below.
    """
    for _ in range(10):
        candidate = f"{strategy_type}-{uuid.uuid4().hex[:8]}"
        exists = (
            db.query(StrategyConfig)
            .filter(StrategyConfig.workspace_id == workspace_id, StrategyConfig.name == candidate)
            .one_or_none()
        )
        if exists is None:
            return candidate
    # Practically unreachable -- fall back to a full UUID, guaranteed unique.
    return f"{strategy_type}-{uuid.uuid4()}"


@router.post("/strategies", response_model=StrategyConfigOut)
def create_strategy(
    body: CreateStrategyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.edit")),
) -> StrategyConfig:
    if body.strategy_type not in KNOWN_STRATEGY_TYPES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unknown strategy_type '{body.strategy_type}'"
        )

    if body.name is None:
        name = _generate_strategy_name(db, user.workspace_id, body.strategy_type)
    else:
        existing = (
            db.query(StrategyConfig)
            .filter(
                StrategyConfig.workspace_id == user.workspace_id,
                StrategyConfig.name == body.name,
            )
            .one_or_none()
        )
        if existing is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "A strategy with this name already exists"
            )
        name = body.name

    if body.underlying_symbol is not None:
        _validate_underlying_symbol_or_404(db, body.underlying_symbol)

    config = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=user.workspace_id,
        name=name,
        strategy_type=body.strategy_type,
        params=body.params,
        underlying_symbol=body.underlying_symbol,
        # Master-switch feature: default every new strategy to Paper
        # (force_paper), not the column's own NULL default -- "Live" must
        # always be something a human opts into via the Mode dropdown,
        # never an inherited default. See the matching backfill migration
        # 0019 for the same fix applied to strategies that already existed.
        runtime_mode=StrategyRuntimeMode.FORCE_PAPER,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


def _get_strategy_config_or_404(db: Session, user: User, strategy_id: uuid.UUID) -> StrategyConfig:
    config = (
        db.query(StrategyConfig)
        .filter(StrategyConfig.id == strategy_id, StrategyConfig.workspace_id == user.workspace_id)
        .one_or_none()
    )
    if config is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy config not found")
    return config


class UpdateStrategyRequest(BaseModel):
    is_enabled: bool | None = None
    runtime_mode: StrategyRuntimeMode | None = None
    underlying_symbol: str | None = None


@router.patch("/strategies/{strategy_id}", response_model=StrategyConfigOut)
def update_strategy(
    strategy_id: uuid.UUID,
    body: UpdateStrategyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.edit")),
) -> StrategyConfig:
    """Ops-Hardening Phase 1 (`is_enabled`/`runtime_mode`) + Phase 6
    (`underlying_symbol`). `status` (the graduation ladder) and
    `strategy_type`/`params` are untouched here, deliberately not folded
    into one catch-all PATCH.

    `runtime_mode: null`/`underlying_symbol: null` explicitly clear the
    field, distinct from omitting it entirely (which leaves it untouched) —
    distinguished via `model_fields_set` since both parse to the same
    Python `None` otherwise. `is_enabled` has no such "clear" case (it's a
    plain, non-nullable bool), so a plain `is not None` check is enough.

    `runtime_mode` feeds `broker_adapter.composition.get_execution_broker`'s
    live-routing check (Phase 6); `underlying_symbol` feeds the daily
    auto-spawner (`strategy_engine.auto_spawner`, also Phase 6) — an
    `is_enabled` config with no `underlying_symbol` set is skipped there,
    alerted rather than guessed. This endpoint only ever updates the DB row;
    a currently-running `StrategyRun` started before this call is
    completely unaffected by it.
    """
    config = _get_strategy_config_or_404(db, user, strategy_id)
    fields_set = body.model_fields_set

    if body.underlying_symbol is not None:
        _validate_underlying_symbol_or_404(db, body.underlying_symbol)

    changes: dict[str, object] = {}
    if body.is_enabled is not None and body.is_enabled != config.is_enabled:
        changes["is_enabled"] = body.is_enabled
        config.is_enabled = body.is_enabled

    if "runtime_mode" in fields_set and body.runtime_mode != config.runtime_mode:
        changes["runtime_mode"] = body.runtime_mode.value if body.runtime_mode is not None else None
        config.runtime_mode = body.runtime_mode

    if "underlying_symbol" in fields_set and body.underlying_symbol != config.underlying_symbol:
        changes["underlying_symbol"] = body.underlying_symbol
        config.underlying_symbol = body.underlying_symbol

    if changes:
        db.flush()
        record_event(
            db,
            workspace_id=user.workspace_id,
            actor_type=ActorType.USER,
            actor_id=user.id,
            event_category=EventCategory.STRATEGY_STATE_CHANGE,
            event_type="strategy_config.updated",
            entity_type="strategy_config",
            entity_id=config.id,
            strategy_config_id=config.id,
            payload=changes,
        )
        db.commit()
        db.refresh(config)
    return config


class BulkRuntimeModeRequest(BaseModel):
    mode: StrategyRuntimeMode | None


class BulkRuntimeModeOut(BaseModel):
    updated_count: int
    strategy_ids: list[uuid.UUID]


@router.post("/strategies/bulk-runtime-mode", response_model=BulkRuntimeModeOut)
def bulk_set_runtime_mode(
    body: BulkRuntimeModeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.edit")),
) -> BulkRuntimeModeOut:
    """The "master switch" confirm dialog's bulk-apply action -- sets every
    workspace strategy's `runtime_mode` to `body.mode` in one pass
    (`body.mode=None` means "Live"/no override, `force_paper` means
    "Paper", the same two values `update_strategy` already accepts one
    strategy at a time). Deliberately a separate endpoint rather than the
    frontend looping `PATCH /strategies/{id}` per row: one DB transaction
    (all-or-nothing) and the audit trail records this as one deliberate
    bulk action, not N indistinguishable individual edits.
    """
    configs = (
        db.query(StrategyConfig).filter(StrategyConfig.workspace_id == user.workspace_id).all()
    )
    changed = [config for config in configs if config.runtime_mode != body.mode]
    for config in changed:
        config.runtime_mode = body.mode

    if changed:
        db.flush()
        for config in changed:
            record_event(
                db,
                workspace_id=user.workspace_id,
                actor_type=ActorType.USER,
                actor_id=user.id,
                event_category=EventCategory.STRATEGY_STATE_CHANGE,
                event_type="strategy_config.updated",
                entity_type="strategy_config",
                entity_id=config.id,
                strategy_config_id=config.id,
                payload={
                    "runtime_mode": body.mode.value if body.mode is not None else None,
                    "bulk": True,
                },
            )
        db.commit()

    return BulkRuntimeModeOut(
        updated_count=len(changed), strategy_ids=[config.id for config in changed]
    )


class StartStrategyRequest(BaseModel):
    trading_session_id: uuid.UUID
    instrument_id: uuid.UUID
    expiry_date: date
    execution_mode: ExecutionMode = ExecutionMode.AUTO
    interval_seconds: float = 30.0


class StrategyRunOut(BaseModel):
    strategy_run_id: uuid.UUID
    status: str
    execution_mode: str


@router.post("/strategies/{strategy_id}/start", response_model=StrategyRunOut)
def start_strategy(
    strategy_id: uuid.UUID,
    body: StartStrategyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.edit")),
) -> dict:
    strategy_config = _get_strategy_config_or_404(db, user, strategy_id)

    trading_session = (
        db.query(TradingSession)
        .filter(
            TradingSession.id == body.trading_session_id,
            TradingSession.workspace_id == user.workspace_id,
        )
        .one_or_none()
    )
    if trading_session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trading session not found")

    instrument = db.get(Instrument, body.instrument_id)
    if instrument is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Instrument not found")

    # 2026-08-14: reject before any writes, same "validate first" discipline
    # this function already applies to the option-chain snapshot below --
    # without this, get_broker() a few lines down silently falls back to
    # the mock (MARKET_DATA_PROVIDER=shoonya, but no human has connected it
    # yet this process lifetime), so record_option_chain_snapshot "succeeds"
    # against entirely fabricated data instead of raising BrokerError, and
    # ensure_ingestion_running further below would start real ingestion
    # against that same mock-wrapped provider — the same bug class fixed in
    # resume_strategy_runners/MarketDataScheduler, just human-triggered
    # (starting a new strategy before reconnecting) rather than
    # restart-triggered. Angel One/TrueData/mock configurations are
    # unaffected -- is_market_data_ready() is always True for them.
    if not is_market_data_ready():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Shoonya is not connected — connect Shoonya before starting a strategy",
        )

    # One immediate option-chain snapshot so the first evaluate() cycle has
    # something to rank against, rather than waiting on whatever polling
    # cadence (Scheduler, on-demand) later refreshes it — record_option_
    # chain_snapshot is designed to be called this way (build plan: "called
    # on a schedule or on demand"). Also the side effect ensure_ingestion_
    # running below depends on: against a real broker adapter, subscribe_
    # quotes needs the underlying's broker token already cached
    # (ShoonyaBrokerAdapter._resolve_token), and that cache is only
    # populated as a side effect of get_option_chain's own underlying-token
    # resolution.
    #
    # **Deliberately runs before the StrategyRun row is created/committed
    # below, not after.** Live-found bug: this used to run after commit, so
    # a broker failure here (e.g. `ShoonyaApiError` for a requested expiry
    # that doesn't exist for this underlying) left a `StrategyRun` row
    # already committed with status SCANNING — a "zombie" run visible in
    # `GET /strategies/running` with a working Stop button, even though
    # nothing was actually scanning. Validating first means a failure here
    # creates nothing: no run row, no audit event, no sleep-inhibitor
    # acquisition. Translated to a clean 502 rather than an unhandled 500 —
    # broker-agnostic (`BrokerError`), not Shoonya-specific, since any real
    # adapter can fail here.
    try:
        record_option_chain_snapshot(
            instrument.id, get_broker(), instrument.symbol, body.expiry_date
        )
    except BrokerError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not fetch option chain: {exc}"
        ) from exc

    # "at most one active run per strategy" is a check-then-act, same class
    # of race the rest of this codebase always serializes explicitly (see
    # core/locking.py's docstring) — two concurrent start requests for the
    # same strategy could otherwise both pass the existing_run check before
    # either commits, ending up with two live runner threads for one
    # strategy_config. Reuses LOCK_EXECUTION_SINGLETON rather than a new
    # named lock, same reasoning mode transitions already use it for:
    # strategy-run lifecycle is adjacent to execution control.
    with advisory_lock(db, LOCK_EXECUTION_SINGLETON):
        existing_run = (
            db.query(StrategyRun)
            .filter(
                StrategyRun.strategy_config_id == strategy_id,
                StrategyRun.status != StrategyRunStatus.STOPPED,
            )
            .one_or_none()
        )
        if existing_run is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Strategy already has an active run")

        # instrument_id/expiry_date/interval_seconds persisted so a restart
        # can rebuild this run's Strategy object (see strategy_engine
        # .recovery.resume_strategy_runners) — previously these were
        # request-only params, never recorded anywhere once the runner
        # thread was built, which is exactly why a restart could never
        # resume a run even in principle.
        run = new_strategy_run(
            strategy_config_id=strategy_id,
            trading_session_id=trading_session.id,
            execution_mode=body.execution_mode,
            started_by_user_id=user.id,
            instrument_id=body.instrument_id,
            expiry_date=body.expiry_date,
            interval_seconds=body.interval_seconds,
        )
        db.add(run)
        db.flush()

        record_event(
            db,
            workspace_id=user.workspace_id,
            actor_type=ActorType.USER,
            actor_id=user.id,
            event_category=EventCategory.STRATEGY_STATE_CHANGE,
            event_type="strategy_run.started",
            entity_type="strategy_run",
            entity_id=run.id,
            trading_session_id=trading_session.id,
            strategy_config_id=strategy_config.id,
            payload={
                "execution_mode": body.execution_mode.value,
                "instrument_id": str(body.instrument_id),
            },
        )
        db.commit()
        db.refresh(run)

    _start_runner_thread(
        run, strategy_config, trading_session, instrument, body.expiry_date, body.interval_seconds
    )

    return {"strategy_run_id": run.id, "status": run.status, "execution_mode": run.execution_mode}


@router.post("/strategies/{strategy_id}/stop")
def stop_strategy(
    strategy_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.edit")),
) -> dict:
    strategy_config = _get_strategy_config_or_404(db, user, strategy_id)

    run = (
        db.query(StrategyRun)
        .filter(
            StrategyRun.strategy_config_id == strategy_config.id,
            StrategyRun.status != StrategyRunStatus.STOPPED,
        )
        .order_by(StrategyRun.started_at.desc())
        .first()
    )
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active run for this strategy")

    _stop_active_run(db, run, strategy_config, actor_type=ActorType.USER, actor_id=user.id)
    db.commit()
    return {"ok": True}


def _todays_active_session(db: Session, workspace_id: uuid.UUID) -> TradingSession | None:
    today_ist = now_ist().date()
    sessions = (
        db.query(TradingSession)
        .filter(
            TradingSession.workspace_id == workspace_id,
            TradingSession.status == TradingSessionStatus.ACTIVE,
        )
        .all()
    )
    return next((s for s in sessions if to_ist(s.started_at).date() == today_ist), None)


class SetStrategyPowerRequest(BaseModel):
    is_enabled: bool


class SetStrategyPowerOut(BaseModel):
    is_enabled: bool
    run_started: bool
    run_stopped: bool
    run_id: uuid.UUID | None
    detail: str


@router.post("/strategies/{strategy_id}/power", response_model=SetStrategyPowerOut)
def set_strategy_power(
    strategy_id: uuid.UUID,
    body: SetStrategyPowerRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.edit")),
) -> SetStrategyPowerOut:
    """The "Power" toggle's dedicated route (Dual-Trigger Model, 2026-08-17)
    -- unlike `PATCH /strategies/{id}`, which only ever persists the
    `is_enabled` flag for tomorrow's cron to pick up, this makes the toggle
    immediately actionable: `true` auto-resolves today's session/instrument/
    expiry (reusing `strategy_engine.auto_spawner.spawn_one_now`, the same
    logic the cron/login paths use, just with `ignore_stopped_today=True` --
    a deliberate toggle-on is allowed to re-arm something stopped earlier
    today, unlike the ambient paths) and starts its runner thread right
    away; `false` stops whatever run is currently active for this config,
    same as `stop_strategy`.

    Always returns 200 with an explicit `run_started`/`run_stopped`/`detail`
    outcome, never a silent no-op -- a click that doesn't actually start
    anything (trade window closed, no active session today, a broker error)
    must never look identical to one that did. `is_enabled` itself is
    persisted regardless of the spawn outcome: that flag is the user's
    standing intent, not contingent on today's specific spawn attempt
    succeeding, so a real skip today still leaves it correctly enabled for
    tomorrow's bootstrap.
    """
    strategy_config = _get_strategy_config_or_404(db, user, strategy_id)

    if strategy_config.is_enabled != body.is_enabled:
        strategy_config.is_enabled = body.is_enabled
        db.flush()
        record_event(
            db,
            workspace_id=user.workspace_id,
            actor_type=ActorType.USER,
            actor_id=user.id,
            event_category=EventCategory.STRATEGY_STATE_CHANGE,
            event_type="strategy_config.updated",
            entity_type="strategy_config",
            entity_id=strategy_config.id,
            strategy_config_id=strategy_config.id,
            payload={"is_enabled": body.is_enabled},
        )

    run_started = False
    run_stopped = False
    run_id: uuid.UUID | None = None
    detail = "No change."

    if body.is_enabled:
        trading_session = _todays_active_session(db, user.workspace_id)
        if trading_session is None:
            detail = "No active trading session found for today -- start one first."
            db.commit()
        else:
            outcome = spawn_one_now(db, trading_session, strategy_config, actor_id=user.id)
            detail = outcome.detail
            if outcome.status == SpawnStatus.SPAWNED and outcome.run is not None:
                db.commit()
                db.refresh(outcome.run)
                instrument = db.get(Instrument, outcome.run.instrument_id)
                if instrument is not None and outcome.run.expiry_date is not None:
                    _start_runner_thread(
                        outcome.run,
                        strategy_config,
                        trading_session,
                        instrument,
                        outcome.run.expiry_date,
                        outcome.run.interval_seconds or 30.0,
                    )
                    run_started = True
                    run_id = outcome.run.id
            else:
                db.commit()
    else:
        run = (
            db.query(StrategyRun)
            .filter(
                StrategyRun.strategy_config_id == strategy_config.id,
                StrategyRun.status != StrategyRunStatus.STOPPED,
            )
            .order_by(StrategyRun.started_at.desc())
            .first()
        )
        if run is not None:
            _stop_active_run(db, run, strategy_config, actor_type=ActorType.USER, actor_id=user.id)
            run_stopped = True
            detail = "Strategy stopped."
        else:
            detail = "No active run to stop."
        db.commit()

    return SetStrategyPowerOut(
        is_enabled=body.is_enabled,
        run_started=run_started,
        run_stopped=run_stopped,
        run_id=run_id,
        detail=detail,
    )


class RunningPositionOut(BaseModel):
    position_id: uuid.UUID
    option_contract_id: uuid.UUID
    side: str
    qty: int
    entry_price: float


class PendingApprovalOut(BaseModel):
    approval_id: uuid.UUID
    trade_intent_id: uuid.UUID
    option_contract_id: uuid.UUID
    side: str
    qty_lots: int
    entry_price: float
    expires_at: datetime


class RunningStrategyOut(BaseModel):
    strategy_run_id: uuid.UUID
    strategy_config_id: uuid.UUID
    strategy_name: str
    strategy_type: str
    trading_session_id: uuid.UUID
    execution_mode: str
    status: str
    started_at: datetime
    open_position: RunningPositionOut | None
    pending_approvals: list[PendingApprovalOut]
    data_freshness: str | None


@router.get("/strategies/running", response_model=list[RunningStrategyOut])
def list_running_strategies(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.view")),
) -> list[RunningStrategyOut]:
    """Every non-STOPPED `StrategyRun` in the workspace, the first point in
    Phase 4 where multiple concurrent real runs need a single place to see
    them all — read-only, no new write path. `open_position`/
    `pending_approvals` are read fresh from the DB on every call rather
    than cached, so this is always ground truth regardless of which runner
    thread produced it. `pending_approvals` carries full rows (not just a
    count) so the frontend's inline Approve/Reject buttons have an
    `approval_id` to act on without a separate lookup.
    """
    runs = (
        db.query(StrategyRun)
        .join(StrategyConfig, StrategyRun.strategy_config_id == StrategyConfig.id)
        .filter(
            StrategyConfig.workspace_id == user.workspace_id,
            StrategyRun.status != StrategyRunStatus.STOPPED,
        )
        .order_by(StrategyRun.started_at.desc())
        .all()
    )

    result: list[RunningStrategyOut] = []
    for run in runs:
        strategy_config = db.get(StrategyConfig, run.strategy_config_id)
        if strategy_config is None:
            continue

        position = get_open_position_for_run(db, run)
        pending_rows = (
            db.query(PendingTradeApproval, TradeIntent)
            .join(TradeIntent, PendingTradeApproval.trade_intent_id == TradeIntent.id)
            .filter(
                PendingTradeApproval.strategy_run_id == run.id,
                PendingTradeApproval.status == ApprovalStatus.PENDING,
            )
            .all()
        )

        # Read-only classification (no refresh here — that only happens
        # inside the runner's own cycle, see market_data.freshness) against
        # whichever instrument/expiry the live runner thread is tracking.
        # `None` (not "dead") when no live runner is registered for this run
        # (e.g. right after a restart, before startup-recovery resumes it) —
        # there's nothing to classify freshness *of* in that case.
        runner = _RUNNERS.get(run.id)
        data_freshness = (
            worse_of(
                classify_latest_tick(db, runner.instrument_id),
                classify_option_chain(db, runner.instrument_id, runner.expiry_date),
            ).value
            if runner is not None
            else None
        )

        result.append(
            RunningStrategyOut(
                strategy_run_id=run.id,
                strategy_config_id=strategy_config.id,
                strategy_name=strategy_config.name,
                strategy_type=strategy_config.strategy_type,
                trading_session_id=run.trading_session_id,
                # str(), not .value: these are String columns typed with a
                # StrEnum hint (Mapped[ExecutionMode] etc.), not an actual
                # sqlalchemy.Enum column — a row loaded fresh from the DB
                # (any session other than the one that just wrote it, i.e.
                # every real request) comes back as a plain str with no
                # .value attribute. str() is safe for both: StrEnum's own
                # __str__ returns its .value, and a plain str returns itself.
                execution_mode=str(run.execution_mode),
                status=str(run.status),
                started_at=run.started_at,
                open_position=(
                    RunningPositionOut(
                        position_id=position.id,
                        option_contract_id=position.option_contract_id,
                        side=str(position.side),
                        qty=position.qty,
                        entry_price=float(position.entry_price),
                    )
                    if position is not None
                    else None
                ),
                pending_approvals=[
                    PendingApprovalOut(
                        approval_id=approval.id,
                        trade_intent_id=trade_intent.id,
                        option_contract_id=trade_intent.option_contract_id,
                        side=str(trade_intent.side),
                        qty_lots=trade_intent.qty_lots,
                        entry_price=float(trade_intent.entry_price),
                        expires_at=approval.expires_at,
                    )
                    for approval, trade_intent in pending_rows
                ],
                data_freshness=data_freshness,
            )
        )

    return result


def _get_pending_approval_or_404(
    db: Session, user: User, approval_id: uuid.UUID
) -> PendingTradeApproval:
    """Scoped by workspace via a join through TradeIntent — PendingTradeApproval
    has no workspace_id column of its own, but every other lookup in this
    module filters by `user.workspace_id`, and this one must too: without it,
    a user could approve/reject another workspace's pending trade just by
    knowing (or guessing) its UUID.
    """
    approval = (
        db.query(PendingTradeApproval)
        .join(TradeIntent, PendingTradeApproval.trade_intent_id == TradeIntent.id)
        .filter(
            PendingTradeApproval.id == approval_id, TradeIntent.workspace_id == user.workspace_id
        )
        .one_or_none()
    )
    if approval is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pending trade approval not found")
    return approval


@router.post("/trade-approvals/{approval_id}/approve")
def approve_trade_approval(
    approval_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("papertrade.execute")),
) -> dict:
    """Wrapped in `LOCK_EXECUTION_SINGLETON` for the same reason
    `start_strategy` is: the `approval.status != PENDING` check below is a
    check-then-act, and two concurrent Approve calls for the same approval
    (a double-click, or a retried request) could otherwise both pass it
    before either commits. Found live, via manual browser QC: two rapid
    clicks produced a genuine Postgres deadlock between the two requests'
    `pending_trade_approvals` UPDATEs and a `PositionManager` background
    poll — Postgres's own deadlock detector aborted one (a 500, not a clean
    409), and no double-dispatch occurred, but the endpoint had no business
    depending on that detector as its only safety net. Reentrant with the
    same lock `dispatch_trade_intent` takes internally, per
    `core/locking.py`'s own docstring.
    """
    with advisory_lock(db, LOCK_EXECUTION_SINGLETON):
        approval = _get_pending_approval_or_404(db, user, approval_id)
        if approval.status != ApprovalStatus.PENDING:
            raise HTTPException(status.HTTP_409_CONFLICT, f"approval already {approval.status}")

        trade_intent = db.get(TradeIntent, approval.trade_intent_id)
        if trade_intent is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Trade intent not found")

        now = _utcnow()
        if approval.expires_at < now:
            approval.status = ApprovalStatus.EXPIRED
            trade_intent.status = TradeIntentStatus.EXPIRED
            db.add(approval)
            db.add(trade_intent)
            db.flush()
            record_event(
                db,
                workspace_id=user.workspace_id,
                actor_type=ActorType.USER,
                actor_id=user.id,
                event_category=EventCategory.RISK_DECISION,
                event_type="pending_trade_approval.expired",
                entity_type="trade_intent",
                entity_id=trade_intent.id,
            )
            db.commit()
            raise HTTPException(status.HTTP_409_CONFLICT, "Approval window has expired")

        # Lightweight freshness re-check — a click is a stale instruction if the
        # market moved materially while the human was deciding. Surfaces as 409
        # rather than silently dispatching the original, now-stale numbers; the
        # approval stays PENDING so re-clicking Approve retries this check.
        # Shared with evaluate_trade_intent's AUTO-mode equivalent via
        # market_data.freshness.check_price_drift.
        latest_tick = (
            db.query(QuoteTick)
            .filter(QuoteTick.option_contract_id == trade_intent.option_contract_id)
            .order_by(QuoteTick.ts.desc())
            .first()
        )
        if latest_tick is not None and check_price_drift(
            float(latest_tick.ltp),
            float(trade_intent.entry_price),
            tolerance_pct=PRICE_DRIFT_TOLERANCE_PCT,
        ):
            drift = abs(float(latest_tick.ltp) - float(trade_intent.entry_price)) / float(
                trade_intent.entry_price
            )
            record_event(
                db,
                workspace_id=user.workspace_id,
                actor_type=ActorType.USER,
                actor_id=user.id,
                event_category=EventCategory.RISK_DECISION,
                event_type="pending_trade_approval.stale",
                entity_type="trade_intent",
                entity_id=trade_intent.id,
                payload={"drift_pct": drift},
            )
            db.commit()
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Conditions changed — re-approve to confirm"
            )

        approval.status = ApprovalStatus.APPROVED
        approval.decided_by_user_id = user.id
        approval.decided_at = now
        trade_intent.status = TradeIntentStatus.DISPATCHED
        trade_intent.dispatched_at = now
        db.add(approval)
        db.add(trade_intent)
        db.flush()

        record_event(
            db,
            workspace_id=user.workspace_id,
            actor_type=ActorType.USER,
            actor_id=user.id,
            event_category=EventCategory.RISK_DECISION,
            event_type="pending_trade_approval.approved",
            entity_type="trade_intent",
            entity_id=trade_intent.id,
        )

        # Hands off to the real Execution Service, same as the auto-execute path
        # (strategy_engine.service.submit_signal) — without this, an approved
        # intent would sit DISPATCHED forever, permanently holding a concurrency
        # slot and a same-strike lock for the rest of the session.
        trading_session = db.get(TradingSession, trade_intent.trading_session_id)
        if trading_session is not None:
            dispatch_trade_intent(db, trading_session, trade_intent)

        db.commit()
        return {"ok": True, "trade_intent_status": trade_intent.status}


@router.post("/trade-approvals/{approval_id}/reject")
def reject_trade_approval(
    approval_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("papertrade.execute")),
) -> dict:
    """Same `LOCK_EXECUTION_SINGLETON` reasoning as `approve_trade_approval`
    above — the `approval.status != PENDING` check is check-then-act and
    must not race a concurrent approve/reject on the same approval.
    """
    with advisory_lock(db, LOCK_EXECUTION_SINGLETON):
        approval = _get_pending_approval_or_404(db, user, approval_id)
        if approval.status != ApprovalStatus.PENDING:
            raise HTTPException(status.HTTP_409_CONFLICT, f"approval already {approval.status}")

        trade_intent = db.get(TradeIntent, approval.trade_intent_id)
        if trade_intent is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Trade intent not found")

        now = _utcnow()
        approval.status = ApprovalStatus.REJECTED
        approval.decided_by_user_id = user.id
        approval.decided_at = now
        trade_intent.status = TradeIntentStatus.HUMAN_REJECTED
        db.add(approval)
        db.add(trade_intent)
        db.flush()

        record_event(
            db,
            workspace_id=user.workspace_id,
            actor_type=ActorType.USER,
            actor_id=user.id,
            event_category=EventCategory.RISK_DECISION,
            event_type="pending_trade_approval.rejected",
            entity_type="trade_intent",
            entity_id=trade_intent.id,
        )
        db.commit()
        return {"ok": True}
