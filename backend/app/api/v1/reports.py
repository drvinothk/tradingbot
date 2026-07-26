"""Reporting v1 API surface — the strategy graduation dashboard's two
read-only endpoints: a single session's daily report, and a strategy's
scorecard across every session it's ever run in.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.core.security.rbac import require_permission
from app.domain.identity.models import User
from app.domain.session.models import TradingSession
from app.domain.strategy.models import StrategyConfig
from app.modules.reporting.service import build_daily_report, build_scorecard

router = APIRouter(prefix="/reports", tags=["reports"])


class PerformanceStatsOut(BaseModel):
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float | None
    max_drawdown: float
    total_realized_pnl: float
    total_slippage: float
    signal_count: int
    dispatched_count: int
    filled_count: int


class DailyReportOut(PerformanceStatsOut):
    trading_session_id: uuid.UUID


class ScorecardOut(PerformanceStatsOut):
    strategy_config_id: uuid.UUID


@router.get("/sessions/{session_id}/daily", response_model=DailyReportOut)
def get_daily_report(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.view")),
) -> DailyReportOut:
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

    report = build_daily_report(db, trading_session)
    return DailyReportOut(**vars(report))


@router.get("/strategies/{strategy_config_id}/scorecard", response_model=ScorecardOut)
def get_scorecard(
    strategy_config_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.view")),
) -> ScorecardOut:
    strategy_config = (
        db.query(StrategyConfig)
        .filter(
            StrategyConfig.id == strategy_config_id,
            StrategyConfig.workspace_id == user.workspace_id,
        )
        .one_or_none()
    )
    if strategy_config is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy config not found")

    scorecard = build_scorecard(db, strategy_config_id)
    return ScorecardOut(**vars(scorecard))
