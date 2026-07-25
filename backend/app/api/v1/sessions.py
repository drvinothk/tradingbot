"""Trading session lifecycle + safe-mode control. The full daily-plan form
(budget/target/loss/funding-mode entry) is Phase 2 work — this is the Phase 0
minimal surface needed to prove the mode machine and kill switch end-to-end:
create a session, read its mode, flip to kill_switch, and see a stub order
call get blocked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.db.session import get_db
from app.core.modes import ModeTransitionError, enter_kill_switch
from app.core.security.rbac import require_permission
from app.domain.audit.models import ActorType, EventCategory
from app.domain.identity.models import BrokerAccount, User
from app.domain.session.models import (
    FundingMode,
    SafeMode,
    TradingSession,
    TradingSessionStatus,
    TransitionTriggerType,
)
from app.modules.audit_service.service import record_event

router = APIRouter(prefix="/sessions", tags=["sessions"])


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


class KillSwitchRequest(BaseModel):
    reason: str = "manual kill switch"


class DailyPlanRequest(BaseModel):
    budget_amount: float = Field(gt=0)
    daily_target_profit: float = Field(gt=0)
    daily_loss_cap: float = Field(gt=0)
    funding_mode: FundingMode = FundingMode.CASH


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


@router.post("/{session_id}/stub-order")
def stub_place_order(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("papertrade.execute")),
) -> dict:
    """Placeholder standing in for Execution Service until Phase 3 — exists
    only to prove the mode gate actually blocks order placement, not just
    that the mode value changes in the DB.
    """
    trading_session = _get_session_or_404(db, user, session_id)
    if trading_session.mode == SafeMode.KILL_SWITCH:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Engine is in kill_switch — no orders may be placed"
        )
    return {"ok": True, "note": "stub only — real Execution Service arrives in Phase 3"}
