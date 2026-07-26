"""Read-only visibility into the real Order/Position lifecycle Phase 3
introduces — `GET /orders` and `GET /positions`, both scoped to a caller-
supplied `trading_session_id` the requesting user actually owns (same
workspace-scoping discipline every other lookup in this codebase follows).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.core.security.rbac import require_permission
from app.domain.execution.models import Order, Position
from app.domain.identity.models import User
from app.domain.session.models import TradingSession

router = APIRouter(tags=["execution"])


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


class OrderOut(BaseModel):
    id: uuid.UUID
    trading_session_id: uuid.UUID
    option_contract_id: uuid.UUID
    trade_intent_id: uuid.UUID | None
    position_id: uuid.UUID | None
    mode: str
    side: str
    order_type: str
    qty: int
    status: str
    filled_qty: int
    avg_fill_price: float | None
    broker_order_id: str
    submitted_at: datetime

    model_config = {"from_attributes": True}


class PositionOut(BaseModel):
    id: uuid.UUID
    trading_session_id: uuid.UUID
    option_contract_id: uuid.UUID
    trade_intent_id: uuid.UUID
    side: str
    qty: int
    entry_price: float
    status: str
    opened_at: datetime
    closed_at: datetime | None

    model_config = {"from_attributes": True}


@router.get("/orders", response_model=list[OrderOut])
def list_orders(
    trading_session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.view")),
) -> list[Order]:
    trading_session = _get_session_or_404(db, user, trading_session_id)
    return (
        db.query(Order)
        .filter(Order.trading_session_id == trading_session.id)
        .order_by(Order.submitted_at.desc())
        .all()
    )


@router.get("/positions", response_model=list[PositionOut])
def list_positions(
    trading_session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.view")),
) -> list[Position]:
    trading_session = _get_session_or_404(db, user, trading_session_id)
    return (
        db.query(Position)
        .filter(Position.trading_session_id == trading_session.id)
        .order_by(Position.opened_at.desc())
        .all()
    )
