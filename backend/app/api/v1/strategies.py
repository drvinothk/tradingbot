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
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.core.locking import LOCK_EXECUTION_SINGLETON, advisory_lock
from app.core.security.rbac import require_permission
from app.domain.audit.models import ActorType, EventCategory
from app.domain.identity.models import User
from app.domain.market.models import Instrument, QuoteTick
from app.domain.session.models import TradingSession
from app.domain.strategy.models import (
    ApprovalStatus,
    ExecutionMode,
    PendingTradeApproval,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    TradeIntent,
    TradeIntentStatus,
)
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.composition import get_broker
from app.modules.execution_engine.paper.registry import ensure_position_manager_running
from app.modules.execution_engine.paper.service import dispatch_trade_intent
from app.modules.market_data import record_option_chain_snapshot
from app.modules.market_data.registry import ensure_ingestion_running
from app.modules.strategy_engine.common_rules import get_open_position_for_run
from app.modules.strategy_engine.interface import Strategy
from app.modules.strategy_engine.runner import StrategyRunner
from app.modules.strategy_engine.strategies import (
    EMAMicroPullbackStrategy,
    ORBStrategy,
    SyntheticStrategy,
    VWAPPullbackStrategy,
)

router = APIRouter(tags=["strategies"])

# strategy_run_id -> live runner thread. See module docstring.
_RUNNERS: dict[uuid.UUID, StrategyRunner] = {}

# How much the underlying's premium may have moved since the intent was
# generated before an Approve click is treated as stale — see the build
# plan's "Auto-execute vs Approval-required" section.
FRESHNESS_TOLERANCE_PCT = 0.03


def _utcnow() -> datetime:
    return datetime.now(UTC)


ORB_PARAM_KEYS = {
    "or_minutes",
    "stop_pct",
    "target_pct",
    "trail_activation_fraction",
    "trail_lock_fraction",
}
VWAP_PULLBACK_PARAM_KEYS = {
    "pullback_tolerance_frac",
    "stop_pct",
    "target_pct",
    "trail_activation_fraction",
    "trail_lock_fraction",
}
EMA_MICRO_PULLBACK_PARAM_KEYS = VWAP_PULLBACK_PARAM_KEYS


def _build_strategy(
    strategy_config: StrategyConfig, instrument_id: uuid.UUID, expiry_date: date
) -> Strategy:
    """Maps `strategy_config.strategy_type` to its `Strategy` class, reading
    that strategy's own tunables from `strategy_config.params` (missing keys
    fall back to each strategy's own constructor defaults) — the only place
    in the codebase that needs to know all four concrete strategy types.
    """
    params = strategy_config.params or {}
    strategy_type = strategy_config.strategy_type

    if strategy_type == "synthetic":
        return SyntheticStrategy(instrument_id=instrument_id, expiry_date=expiry_date)
    if strategy_type == "orb":
        return ORBStrategy(
            instrument_id=instrument_id,
            expiry_date=expiry_date,
            **{k: v for k, v in params.items() if k in ORB_PARAM_KEYS},
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
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST, f"unknown strategy_type '{strategy_type}'"
    )


KNOWN_STRATEGY_TYPES = {"synthetic", "orb", "vwap_pullback", "ema_micro_pullback"}


class StrategyConfigOut(BaseModel):
    id: uuid.UUID
    name: str
    strategy_type: str
    params: dict
    status: str

    model_config = {"from_attributes": True}


class CreateStrategyRequest(BaseModel):
    name: str
    strategy_type: str = "synthetic"
    params: dict = {}


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

    existing = (
        db.query(StrategyConfig)
        .filter(StrategyConfig.workspace_id == user.workspace_id, StrategyConfig.name == body.name)
        .one_or_none()
    )
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A strategy with this name already exists")

    config = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=user.workspace_id,
        name=body.name,
        strategy_type=body.strategy_type,
        params=body.params,
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

        run = StrategyRun(
            id=uuid.uuid4(),
            strategy_config_id=strategy_id,
            trading_session_id=trading_session.id,
            execution_mode=body.execution_mode,
            status=StrategyRunStatus.SCANNING,
            started_at=_utcnow(),
            started_by_user_id=user.id,
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

    # MarketDataIngestionService/IndicatorEngine were built in Phase 1 but
    # nothing ever actually started one outside tests — real strategies
    # (unlike the synthetic stub) need genuinely live price_bars/
    # indicator_snapshots for their underlying. One shared service for the
    # whole process (see market_data.registry's own docstring for why: a
    # broker connection is a single shared stream, not one per instrument),
    # idempotent per symbol so several concurrent runs on the same or
    # different underlyings all share it.
    ensure_ingestion_running(instrument.symbol)

    # One immediate option-chain snapshot so the first evaluate() cycle has
    # something to rank against, rather than waiting on whatever polling
    # cadence (Scheduler, on-demand) later refreshes it — record_option_
    # chain_snapshot is designed to be called this way (build plan: "called
    # on a schedule or on demand").
    record_option_chain_snapshot(instrument.id, get_broker(), instrument.symbol, body.expiry_date)

    strategy = _build_strategy(strategy_config, body.instrument_id, body.expiry_date)
    runner = StrategyRunner(strategy, run.id, interval_seconds=body.interval_seconds)
    runner.start()
    _RUNNERS[run.id] = runner

    # PositionManager is per trading_session, not per strategy_run — a
    # session that already has one running (e.g. a second strategy started
    # against it) is left alone; ensure_position_manager_running no-ops in
    # that case. It's deliberately not stopped by stop_strategy below: an
    # already-open position from this run must keep being managed to its
    # stop/target even after the strategy that opened it stops scanning.
    ensure_position_manager_running(trading_session.id)

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

    runner = _RUNNERS.pop(run.id, None)
    if runner is not None:
        runner.stop()

    run.status = StrategyRunStatus.STOPPED
    run.stopped_at = _utcnow()
    db.add(run)
    db.flush()

    record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        event_category=EventCategory.STRATEGY_STATE_CHANGE,
        event_type="strategy_run.stopped",
        entity_type="strategy_run",
        entity_id=run.id,
        trading_session_id=run.trading_session_id,
        strategy_config_id=strategy_config.id,
    )
    db.commit()
    return {"ok": True}


class RunningPositionOut(BaseModel):
    position_id: uuid.UUID
    option_contract_id: uuid.UUID
    side: str
    qty: int
    entry_price: float


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
    pending_approval_count: int


@router.get("/strategies/running", response_model=list[RunningStrategyOut])
def list_running_strategies(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.view")),
) -> list[RunningStrategyOut]:
    """Every non-STOPPED `StrategyRun` in the workspace, the first point in
    Phase 4 where multiple concurrent real runs need a single place to see
    them all — read-only, no new write path. `open_position`/
    `pending_approval_count` are read fresh from the DB on every call rather
    than cached, so this is always ground truth regardless of which runner
    thread produced it.
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
        pending_count = (
            db.query(PendingTradeApproval)
            .filter(
                PendingTradeApproval.strategy_run_id == run.id,
                PendingTradeApproval.status == ApprovalStatus.PENDING,
            )
            .count()
        )

        result.append(
            RunningStrategyOut(
                strategy_run_id=run.id,
                strategy_config_id=strategy_config.id,
                strategy_name=strategy_config.name,
                strategy_type=strategy_config.strategy_type,
                trading_session_id=run.trading_session_id,
                execution_mode=run.execution_mode.value,
                status=run.status.value,
                started_at=run.started_at,
                open_position=(
                    RunningPositionOut(
                        position_id=position.id,
                        option_contract_id=position.option_contract_id,
                        side=position.side.value,
                        qty=position.qty,
                        entry_price=float(position.entry_price),
                    )
                    if position is not None
                    else None
                ),
                pending_approval_count=pending_count,
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
    latest_tick = (
        db.query(QuoteTick)
        .filter(QuoteTick.option_contract_id == trade_intent.option_contract_id)
        .order_by(QuoteTick.ts.desc())
        .first()
    )
    if latest_tick is not None and float(trade_intent.entry_price) > 0:
        drift = abs(float(latest_tick.ltp) - float(trade_intent.entry_price)) / float(
            trade_intent.entry_price
        )
        if drift > FRESHNESS_TOLERANCE_PCT:
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
