"""Trading session lifecycle + safe-mode control. The full daily-plan form
(budget/target/loss/funding-mode entry) is Phase 2 work; Phase 3 adds manual
square-off/reconcile triggers alongside the mode machine and kill switch.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.clock import now_ist
from app.core.db.session import get_db
from app.core.locking import LOCK_EXECUTION_SINGLETON, advisory_lock
from app.core.modes import ModeTransitionError, enter_kill_switch, transition_mode
from app.core.security.rbac import require_permission
from app.domain.audit.models import ActorType, EventCategory
from app.domain.broker.models import BrokerSyncState, ReconciliationRun, ReconciliationTrigger
from app.domain.execution.models import Position, PositionStatus
from app.domain.identity.models import BrokerAccount, User
from app.domain.session.models import (
    FundingMode,
    SafeMode,
    TradingSession,
    TradingSessionStatus,
    TransitionTriggerType,
)
from app.domain.strategy.models import StrategyRun, StrategyRunStatus
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.composition import get_execution_broker
from app.modules.reconciliation.service import run_reconciliation
from app.modules.scheduler.eod_square_off import run_eod_square_off

router = APIRouter(prefix="/sessions", tags=["sessions"])
broker_accounts_router = APIRouter(prefix="/broker-accounts", tags=["broker-accounts"])


class CreateSessionRequest(BaseModel):
    broker_account_id: uuid.UUID
    budget_amount: float | None = None
    daily_target_profit: float | None = None
    daily_loss_cap: float | None = None
    funding_mode: FundingMode = FundingMode.CASH


class SessionOut(BaseModel):
    id: uuid.UUID
    mode: str
    status: str
    broker_account_id: uuid.UUID

    model_config = {"from_attributes": True}


class BrokerAccountOut(BaseModel):
    id: uuid.UUID
    broker_type: str
    label: str
    status: str

    model_config = {"from_attributes": True}


class KillSwitchRequest(BaseModel):
    reason: str = "manual kill switch"


class DailyPlanRequest(BaseModel):
    budget_amount: float = Field(gt=0)
    daily_target_profit: float = Field(gt=0)
    daily_loss_cap: float = Field(gt=0)
    funding_mode: FundingMode = FundingMode.CASH


@broker_accounts_router.get("", response_model=list[BrokerAccountOut])
def list_broker_accounts(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.start")),
) -> list[BrokerAccount]:
    return (
        db.query(BrokerAccount)
        .filter(BrokerAccount.workspace_id == user.workspace_id)
        .order_by(BrokerAccount.label)
        .all()
    )


@router.get("", response_model=list[SessionOut])
def list_sessions(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.start")),
) -> list[TradingSession]:
    return (
        db.query(TradingSession)
        .filter(TradingSession.workspace_id == user.workspace_id)
        .order_by(TradingSession.started_at.desc())
        .all()
    )


@router.post("", response_model=SessionOut)
def create_session(
    body: CreateSessionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.start")),
) -> TradingSession:
    broker_account = (
        db.query(BrokerAccount)
        .filter(
            BrokerAccount.id == body.broker_account_id,
            BrokerAccount.workspace_id == user.workspace_id,
        )
        .one_or_none()
    )
    if broker_account is None:
        # Validated explicitly rather than letting an unknown/foreign
        # broker_account_id fall through to the FK constraint — that would
        # otherwise surface as an unhandled 500 (IntegrityError) instead of
        # a clean 404.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Broker account not found")

    # "At most one ACTIVE session per broker account, per day" is a
    # check-then-act, same class of race this codebase always serializes
    # explicitly (see core/locking.py's docstring) — reused
    # LOCK_EXECUTION_SINGLETON rather than a new named lock, same reasoning
    # start_strategy already uses it for "at most one active run per
    # strategy". This is the actual invariant
    # reconciliation.service.run_reconciliation's unscoped
    # broker.get_positions() comparison silently assumes: without it, two
    # concurrently ACTIVE sessions on the same account would each see the
    # other's positions as phantom mismatches.
    #
    # Scoped to "today" (IST), not "ever": nothing in this codebase
    # currently transitions TradingSession.status to ENDED (no
    # end-session action exists yet — session status is set once at
    # creation and never revisited), so an unscoped ACTIVE check would
    # permanently block a broker account from ever starting a second
    # session after its first one, which is a real regression this
    # session-per-day scoping avoids while still catching the actual
    # collision case (two sessions genuinely trading the same account on
    # the same day).
    today_ist = now_ist().date()
    with advisory_lock(db, LOCK_EXECUTION_SINGLETON):
        existing_active_today = (
            db.query(TradingSession)
            .filter(
                TradingSession.broker_account_id == broker_account.id,
                TradingSession.status == TradingSessionStatus.ACTIVE,
            )
            .all()
        )
        if any(s.started_at.date() == today_ist for s in existing_active_today):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Broker account already has an active trading session today",
            )

        defaults = get_settings().risk_defaults
        trading_session = TradingSession(
            id=uuid.uuid4(),
            workspace_id=user.workspace_id,
            broker_account_id=broker_account.id,
            started_by_user_id=user.id,
            mode=SafeMode.PAPER_ONLY,
            started_at=datetime.now(UTC),
            budget_amount=body.budget_amount or defaults.default_budget,
            daily_target_profit=body.daily_target_profit or defaults.daily_target_profit,
            daily_loss_cap=body.daily_loss_cap or defaults.daily_loss_cap,
            funding_mode=body.funding_mode,
        )
        db.add(trading_session)
        db.commit()
        db.refresh(trading_session)
        return trading_session


@router.post("/{session_id}/daily-plan", response_model=SessionOut)
def set_daily_plan(
    session_id: uuid.UUID,
    body: DailyPlanRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.start")),
) -> TradingSession:
    """Sets budget/target/max-loss/funding-mode for the day — editable any
    time the session is still active, not just at creation, since the plan
    is meant to be revisable during the day (e.g. tightening the loss cap
    after a rough morning), not a one-shot form.
    """
    trading_session = _get_session_or_404(db, user, session_id)
    if trading_session.status != TradingSessionStatus.ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT, "Trading session is not active")

    previous = {
        "budget_amount": float(trading_session.budget_amount),
        "daily_target_profit": float(trading_session.daily_target_profit),
        "daily_loss_cap": float(trading_session.daily_loss_cap),
        "funding_mode": str(trading_session.funding_mode),
    }

    trading_session.budget_amount = body.budget_amount
    trading_session.daily_target_profit = body.daily_target_profit
    trading_session.daily_loss_cap = body.daily_loss_cap
    trading_session.funding_mode = body.funding_mode
    db.add(trading_session)
    db.flush()

    record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        event_category=EventCategory.MANUAL_OVERRIDE,
        event_type="daily_plan.updated",
        entity_type="trading_session",
        entity_id=trading_session.id,
        trading_session_id=trading_session.id,
        broker_account_id=trading_session.broker_account_id,
        payload={
            "previous": previous,
            "new": {
                "budget_amount": body.budget_amount,
                "daily_target_profit": body.daily_target_profit,
                "daily_loss_cap": body.daily_loss_cap,
                "funding_mode": body.funding_mode.value,
            },
        },
    )
    db.commit()
    db.refresh(trading_session)
    return trading_session


def _get_session_or_404(db: Session, user: User, session_id: uuid.UUID) -> TradingSession:
    trading_session = (
        db.query(TradingSession)
        .filter(
            TradingSession.id == session_id,
            TradingSession.workspace_id == user.workspace_id,
        )
        .one_or_none()
    )
    if trading_session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trading session not found")
    return trading_session


@router.get("/{session_id}", response_model=SessionOut)
def get_session(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.view")),
) -> TradingSession:
    return _get_session_or_404(db, user, session_id)


@router.post("/{session_id}/end", response_model=SessionOut)
def end_session(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.stop")),
) -> TradingSession:
    """2026-08-11 addition, closing a real gap: nothing in this codebase
    ever transitioned `TradingSession.status` to ENDED before this endpoint
    existed (see `create_session`'s own comment on that gap) -- sessions
    accumulated forever with no way to retire one, which is exactly what
    let several genuinely-done paper sessions from earlier days keep
    showing up indistinguishably next to the one actually in use, in every
    Session picker in the app. Refuses (409) rather than silently stopping
    anything on the caller's behalf if the session still has live activity
    -- stop every strategy run and close every position first, the same
    explicit-before-implicit discipline `PositionManager`/execution locking
    already apply elsewhere in this codebase.
    """
    trading_session = _get_session_or_404(db, user, session_id)
    if trading_session.status == TradingSessionStatus.ENDED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Session is already ended")

    live_runs = (
        db.query(StrategyRun)
        .filter(
            StrategyRun.trading_session_id == session_id,
            StrategyRun.status != StrategyRunStatus.STOPPED,
        )
        .count()
    )
    if live_runs:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{live_runs} strategy run(s) still active on this session -- stop them first",
        )

    open_positions = (
        db.query(Position)
        .filter(
            Position.trading_session_id == session_id,
            Position.status == PositionStatus.OPEN,
        )
        .count()
    )
    if open_positions:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{open_positions} open position(s) still on this session -- square off first",
        )

    trading_session.status = TradingSessionStatus.ENDED
    trading_session.ended_at = datetime.now(UTC)
    record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        event_category=EventCategory.MANUAL_OVERRIDE,
        event_type="session.ended",
        entity_type="trading_session",
        entity_id=session_id,
        payload={},
    )
    db.commit()
    db.refresh(trading_session)
    return trading_session


@router.post("/{session_id}/recover-from-kill-switch", response_model=SessionOut)
def recover_from_kill_switch(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("risk.override")),
) -> TradingSession:
    """2026-08-11 addition, closing a real gap found live: `kill_switch ->
    paper_only` was already a legal edge in `transitions.py` (kill_switch
    can only ever resume to paper_only, never straight to a live mode), but
    nothing in `api/v1` ever exposed it -- the only way to *enter*
    kill_switch had a button; recovering from it didn't, anywhere. Found
    when kill-switching a session turned out to be the only stop-like
    control visible in the UI and got used on a session that still had 6
    live paper strategy runs under it, silently blocking every one of them
    from ever dispatching a new trade (`risk_engine.evaluate_trade_intent`
    rejects new entries outright while `mode == kill_switch`) with no way
    back except this now-added endpoint. Gated on `risk.override`, not
    `session.stop` -- matches `transitions.py`'s own required_permission for
    this exact edge, deliberately a higher bar than entering kill_switch
    needs, since clearing one is the more consequential direction.
    """
    trading_session = _get_session_or_404(db, user, session_id)
    try:
        transition_mode(
            db,
            trading_session,
            SafeMode.PAPER_ONLY,
            TransitionTriggerType.MANUAL,
            actor_user=user,
            reason="manual recovery from kill_switch",
        )
    except ModeTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    db.refresh(trading_session)
    return trading_session


@router.post("/{session_id}/kill-switch", response_model=SessionOut)
def trigger_kill_switch(
    session_id: uuid.UUID,
    body: KillSwitchRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.stop")),
) -> TradingSession:
    trading_session = _get_session_or_404(db, user, session_id)
    try:
        enter_kill_switch(
            db, trading_session, TransitionTriggerType.MANUAL, actor_user=user, reason=body.reason
        )
    except ModeTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    db.refresh(trading_session)
    return trading_session


@router.post("/{session_id}/square-off")
def manual_square_off(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.stop")),
) -> dict:
    """Manual trigger for the same EOD force-flatten `PositionManager` runs
    automatically past `cutoff_time` — there's no live scheduler daemon
    wired into `app.main` yet (every Scheduler job in this codebase so far
    is callable-function-plus-test, same pattern here), so this is what
    makes the phase's "done when" EOD criterion exercisable on demand
    instead of waiting for real wall-clock IST to cross cutoff_time.
    """
    trading_session = _get_session_or_404(db, user, session_id)
    outcomes = run_eod_square_off(db, get_execution_broker(trading_session), trading_session)
    db.commit()
    return {"closed_count": len(outcomes)}


@router.post("/{session_id}/reconcile")
def manual_reconcile(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.stop")),
) -> dict:
    """Manual trigger for a reconciliation pass — `PositionManager` already
    runs one periodically on its own poll cadence; this makes it
    exercisable on demand (e.g. right after suspecting/injecting a
    mismatch), same reasoning as `manual_square_off` above.
    """
    trading_session = _get_session_or_404(db, user, session_id)
    run = run_reconciliation(
        db, get_execution_broker(trading_session), trading_session, ReconciliationTrigger.EVENT
    )
    db.commit()
    return {"mismatches_found": run.mismatches_found, "action_taken": run.action_taken}


class ReconciliationRunOut(BaseModel):
    id: uuid.UUID
    trigger_type: str
    mismatches_found: int
    action_taken: str
    started_at: datetime
    finished_at: datetime

    model_config = {"from_attributes": True}


class BrokerSyncStateOut(BaseModel):
    option_contract_id: uuid.UUID
    local_qty: int
    broker_qty: int
    is_mismatched: bool
    checked_at: datetime

    model_config = {"from_attributes": True}


class ReconciliationHistoryOut(BaseModel):
    runs: list[ReconciliationRunOut]
    current_mismatches: list[BrokerSyncStateOut]


@router.get("/{session_id}/reconciliation-runs", response_model=ReconciliationHistoryOut)
def list_reconciliation_runs(
    session_id: uuid.UUID,
    limit: int = 20,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.view")),
) -> ReconciliationHistoryOut:
    """Recovery-panel read path — `ReconciliationRun` (the append-only pass
    log) and `BrokerSyncState` (the latest per-contract snapshot, overwritten
    each pass, not history) both had no read API before this; the manual
    `/reconcile` endpoint above only ever returns the single run it just
    performed, not this session's history.
    """
    trading_session = _get_session_or_404(db, user, session_id)
    runs = (
        db.query(ReconciliationRun)
        .filter(ReconciliationRun.trading_session_id == trading_session.id)
        .order_by(ReconciliationRun.started_at.desc())
        .limit(limit)
        .all()
    )
    mismatches = (
        db.query(BrokerSyncState)
        .filter(
            BrokerSyncState.trading_session_id == trading_session.id,
            BrokerSyncState.is_mismatched.is_(True),
        )
        .order_by(BrokerSyncState.checked_at.desc())
        .all()
    )
    return ReconciliationHistoryOut(
        runs=[ReconciliationRunOut.model_validate(r) for r in runs],
        current_mismatches=[BrokerSyncStateOut.model_validate(m) for m in mismatches],
    )
