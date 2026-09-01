"""Reporting v1 API surface — the strategy performance dashboard's two
read-only endpoints: a single session's daily report, and a strategy's
scorecard across every session it's ever run in.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime, time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.clock import IST
from app.core.db.session import get_db
from app.core.security.rbac import require_permission
from app.domain.execution.models import OrderMode
from app.domain.identity.models import User
from app.domain.ops.models import MarketDataDiagnosticRun, MarketDataDiagnosticSnapshot
from app.domain.session.models import TradingSession
from app.domain.strategy.models import StrategyConfig
from app.modules.reporting.exporter import REPORTS_DIR
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
    # Optional, defaults to the full session (unfiltered) -- unchanged
    # behavior for ReportsPage's own session-picker view. Control Room's
    # "Today's Activity" card passes this explicitly to scope a mixed
    # live+force_paper session's stats to just the mode it's actually
    # displaying -- see build_daily_report's own docstring for why.
    mode: str | None = Query(None),
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

    parsed_mode: OrderMode | None = None
    if mode is not None:
        try:
            parsed_mode = OrderMode(mode)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"Invalid mode: {mode!r}"
            ) from exc

    report = build_daily_report(db, trading_session, mode=parsed_mode)
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


@router.get("/trade-log-export")
def download_trade_log(
    user: User = Depends(require_permission("strategy.view")),
) -> FileResponse:
    """Streams the existing per-workspace Excel workbook
    `TradeLogExportScheduler` already writes daily
    (`reports/trade_log_<workspace_id>.xlsx`, see `exporter.py`'s own
    docstring) — this endpoint adds no new report-building logic, only the
    download plumbing that was missing (previously pulled by hand over SSH).
    """
    path = REPORTS_DIR / f"trade_log_{user.workspace_id}.xlsx"
    if not path.exists():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No trade-log export exists yet for this workspace -- it's written once a day "
            "by the scheduled export job, after the first completed trade.",
        )
    return FileResponse(
        path,
        filename=f"trade_log_{user.workspace_id}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/ws-quality-export")
def download_ws_quality_report(
    on: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.view")),
) -> StreamingResponse:
    """CSV of every `MarketDataDiagnosticSnapshot` row recorded for this
    workspace on `on` (default: today, IST) — see
    `market_data.diagnostic_session`'s own module docstring for how those
    rows get written (`Test Default`/`Test Failback`/`Both` on Market
    Terminal). Built fresh from the DB on every request, not cached --
    the underlying row count for one day (roughly 2 symbols x ~780
    snapshots x up to 2 concurrent roles) is trivial to query and format
    on demand.
    """
    target_date = on or datetime.now(IST).date()
    day_start = datetime.combine(target_date, time.min, tzinfo=IST)
    day_end = datetime.combine(target_date, time.max, tzinfo=IST)

    rows = (
        db.query(MarketDataDiagnosticSnapshot, MarketDataDiagnosticRun)
        .join(
            MarketDataDiagnosticRun,
            MarketDataDiagnosticSnapshot.run_id == MarketDataDiagnosticRun.id,
        )
        .filter(
            MarketDataDiagnosticRun.workspace_id == user.workspace_id,
            MarketDataDiagnosticSnapshot.recorded_at >= day_start,
            MarketDataDiagnosticSnapshot.recorded_at <= day_end,
        )
        .order_by(MarketDataDiagnosticSnapshot.recorded_at)
        .all()
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["recorded_at_ist", "role", "provider", "symbol", "connected", "ltp", "tick_ts_ist"]
    )
    for snapshot, run in rows:
        writer.writerow(
            [
                snapshot.recorded_at.astimezone(IST).isoformat(),
                run.role,
                run.provider,
                snapshot.symbol,
                snapshot.connected,
                snapshot.ltp,
                snapshot.tick_ts.astimezone(IST).isoformat() if snapshot.tick_ts else "",
            ]
        )
    buffer.seek(0)

    filename = f"ws_quality_{target_date.isoformat()}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
