"""Reporting v1 — the strategy performance dashboard's data layer. Both
`build_daily_report` (one trading_session) and `build_scorecard` (one
strategy_config, across every session it's ever run in) compute the same
shape of stats from real `trade_outcomes` rows — win rate, avg win/loss,
profit factor, max drawdown, slippage — plus a signal-vs-execution count
(`signals` generated vs `trade_intents` actually dispatched vs `positions`
actually filled), which is what makes a strategy's paper performance
legible enough to decide whether it's ready to run live.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.domain.execution.models import Order, OrderMode, Position, TradeOutcome
from app.domain.session.models import TradingSession
from app.domain.strategy.models import Signal, StrategyRun, TradeIntent, TradeIntentStatus


@dataclass(frozen=True)
class PerformanceStats:
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    avg_win: float
    avg_loss: float
    # None when there are no losing trades to divide by — "undefined", not 0
    # or infinite, since either would misleadingly imply a real ratio.
    profit_factor: float | None
    max_drawdown: float
    total_realized_pnl: float
    total_slippage: float


@dataclass(frozen=True)
class DailyReport(PerformanceStats):
    trading_session_id: uuid.UUID
    signal_count: int
    dispatched_count: int
    filled_count: int


@dataclass(frozen=True)
class Scorecard(PerformanceStats):
    strategy_config_id: uuid.UUID
    signal_count: int
    dispatched_count: int
    filled_count: int


@dataclass(frozen=True)
class _TradeRow:
    """One completed *trade* for stats purposes — either a legacy single
    `TradeOutcome`, or all legs of a staged (multi-leg) exit collapsed into
    one net row (QC finding 2: a 3-leg trade must count as one trade in win
    rate / profit factor / drawdown, not three)."""

    closed_at: datetime
    realized_pnl: float
    slippage: float


def _collapse_to_trades(outcomes: list[TradeOutcome]) -> list[_TradeRow]:
    by_position: dict[uuid.UUID, _TradeRow] = {}
    for o in outcomes:
        existing = by_position.get(o.position_id)
        if existing is None:
            by_position[o.position_id] = _TradeRow(
                closed_at=o.closed_at,
                realized_pnl=float(o.realized_pnl),
                slippage=float(o.slippage),
            )
        else:
            by_position[o.position_id] = _TradeRow(
                # A staged trade's "close time" is its last leg's.
                closed_at=max(existing.closed_at, o.closed_at),
                realized_pnl=existing.realized_pnl + float(o.realized_pnl),
                slippage=existing.slippage + float(o.slippage),
            )
    return list(by_position.values())


def _compute_stats(outcomes: list[TradeOutcome]) -> PerformanceStats:
    if not outcomes:
        return PerformanceStats(
            trade_count=0,
            win_count=0,
            loss_count=0,
            win_rate=0.0,
            avg_win=0.0,
            avg_loss=0.0,
            profit_factor=None,
            max_drawdown=0.0,
            total_realized_pnl=0.0,
            total_slippage=0.0,
        )

    ordered = sorted(_collapse_to_trades(outcomes), key=lambda o: o.closed_at)
    pnls = [o.realized_pnl for o in ordered]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    # Walk the equity curve in closing order to find the largest peak-to-
    # trough drop — a single realized_pnl total tells you nothing about how
    # bad the ride there was.
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    return PerformanceStats(
        trade_count=len(pnls),
        win_count=len(wins),
        loss_count=len(losses),
        win_rate=len(wins) / len(pnls),
        avg_win=(gross_profit / len(wins)) if wins else 0.0,
        avg_loss=(sum(losses) / len(losses)) if losses else 0.0,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        max_drawdown=max_drawdown,
        total_realized_pnl=sum(pnls),
        total_slippage=sum(float(o.slippage) for o in ordered),
    )


def build_daily_report(
    db: Session, trading_session: TradingSession, *, mode: OrderMode | None = None
) -> DailyReport:
    """`mode=None` (the default, and the only behavior before 2026-09-01) is
    the full session, unfiltered -- what `ReportsPage`'s session-picker view
    needs and has always shown. `mode=OrderMode.LIVE`/`PAPER` scopes trade
    stats to real vs paper fills only, via `Position.opening_order_id`'s own
    `Order.mode` (the same ground-truth field `TradeRow.mode` already reads
    on the frontend, and `fetch_completed_trades_for_day` already joins the
    same way) -- **not** the session's or strategy's current config, which
    can drift after the fact.

    Added because a single `live_enabled` session can hold both live-routed
    and `force_paper` strategies together since the 2026-08-28
    `paper_plus_guarded_live` retirement -- Control Room's "Today's Activity
    (Live)" card was pulling this endpoint unfiltered, so a force_paper
    strategy's own paper trades were silently blending into stats labeled
    "Live" (trade_count/win_rate/max_drawdown all affected; the P&L figure
    itself was already correct, since that one's computed client-side per-
    trade). `signal_count`/`dispatched_count` stay session-wide regardless of
    `mode` -- a `Signal` fires before any live/paper routing decision exists,
    so there's no honest way to retroactively scope it. `filled_count` *is*
    scoped when `mode` is given, via the same Position->Order join, since it
    represents the same "how many positions actually opened" population
    `trade_count` does.
    """
    outcomes_query = db.query(TradeOutcome).filter(
        TradeOutcome.trading_session_id == trading_session.id
    )
    filled_query = db.query(Position).filter(
        Position.trading_session_id == trading_session.id
    )
    if mode is not None:
        outcomes_query = outcomes_query.join(
            Position, TradeOutcome.position_id == Position.id
        ).join(Order, Position.opening_order_id == Order.id).filter(Order.mode == mode)
        filled_query = filled_query.join(
            Order, Position.opening_order_id == Order.id
        ).filter(Order.mode == mode)

    stats = _compute_stats(outcomes_query.all())

    signal_count = (
        db.query(Signal).filter(Signal.trading_session_id == trading_session.id).count()
    )
    dispatched_count = (
        db.query(TradeIntent)
        .filter(
            TradeIntent.trading_session_id == trading_session.id,
            TradeIntent.status == TradeIntentStatus.DISPATCHED,
        )
        .count()
    )
    filled_count = filled_query.count()

    return DailyReport(
        **vars(stats),
        trading_session_id=trading_session.id,
        signal_count=signal_count,
        dispatched_count=dispatched_count,
        filled_count=filled_count,
    )


def build_scorecard(db: Session, strategy_config_id: uuid.UUID) -> Scorecard:
    outcomes = (
        db.query(TradeOutcome)
        .join(TradeIntent, TradeOutcome.trade_intent_id == TradeIntent.id)
        .join(StrategyRun, TradeIntent.strategy_run_id == StrategyRun.id)
        .filter(StrategyRun.strategy_config_id == strategy_config_id)
        .all()
    )
    stats = _compute_stats(outcomes)

    signal_count = (
        db.query(Signal).filter(Signal.strategy_config_id == strategy_config_id).count()
    )
    dispatched_count = (
        db.query(TradeIntent)
        .join(StrategyRun, TradeIntent.strategy_run_id == StrategyRun.id)
        .filter(
            StrategyRun.strategy_config_id == strategy_config_id,
            TradeIntent.status == TradeIntentStatus.DISPATCHED,
        )
        .count()
    )
    filled_count = (
        db.query(Position)
        .join(TradeIntent, Position.trade_intent_id == TradeIntent.id)
        .join(StrategyRun, TradeIntent.strategy_run_id == StrategyRun.id)
        .filter(StrategyRun.strategy_config_id == strategy_config_id)
        .count()
    )

    return Scorecard(
        **vars(stats),
        strategy_config_id=strategy_config_id,
        signal_count=signal_count,
        dispatched_count=dispatched_count,
        filled_count=filled_count,
    )
