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
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.db.session import get_db
from app.core.modes import ModeTransitionError, enter_kill_switch
from app.core.security.rbac import require_permission
from app.domain.identity.models import User
from app.domain.session.models import (
    FundingMode,
    SafeMode,
    TradingSession,
    TransitionTriggerType,
)

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


@router.post("", response_model=SessionOut)
def create_session(
    body: CreateSessionRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.start")),
) -> TradingSession:
    defaults = get_settings().risk_defaults
    trading_session = TradingSession(
        id=uuid.uuid4(),
        workspace_id=user.workspace_id,
        broker_account_id=body.broker_account_id,
        started_by_user_id=user.id,
        mode=SafeMode.PAPER_ONLY,
        started_at=datetime.now(UTC),
        budget_amount=body.budget_amount or defaults.daily_loss_cap,
        daily_target_profit=body.daily_target_profit or defaults.daily_target_profit,
        daily_loss_cap=body.daily_loss_cap or defaults.daily_loss_cap,
        funding_mode=body.funding_mode,
    )
    db.add(trading_session)
    db.commit()
    db.refresh(trading_session)
    return trading_session


def _get_session_or_404(db: Session, session_id: uuid.UUID) -> TradingSession:
    trading_session = db.query(TradingSession).filter(TradingSession.id == session_id).one_or_none()
    if trading_session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trading session not found")
    return trading_session


@router.get("/{session_id}", response_model=SessionOut)
def get_session(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.view")),
) -> TradingSession:
    return _get_session_or_404(db, session_id)


@router.post("/{session_id}/kill-switch", response_model=SessionOut)
def trigger_kill_switch(
    session_id: uuid.UUID,
    reason: str = "manual kill switch",
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.stop")),
) -> TradingSession:
    trading_session = _get_session_or_404(db, session_id)
    try:
        enter_kill_switch(
            db, trading_session, TransitionTriggerType.MANUAL, actor_user=user, reason=reason
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
    trading_session = _get_session_or_404(db, session_id)
    if trading_session.mode == SafeMode.KILL_SWITCH:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Engine is in kill_switch — no orders may be placed"
        )
    return {"ok": True, "note": "stub only — real Execution Service arrives in Phase 3"}
