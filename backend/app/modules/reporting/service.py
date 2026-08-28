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

from sqlalchemy.orm import Session

from app.domain.execution.models import Position, TradeOutcome
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

    ordered = sorted(outcomes, key=lambda o: o.closed_at)
    pnls = [float(o.realized_pnl) for o in ordered]
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


def build_daily_report(db: Session, trading_session: TradingSession) -> DailyReport:
    outcomes = (
        db.query(TradeOutcome)
        .filter(TradeOutcome.trading_session_id == trading_session.id)
        .all()
    )
    stats = _compute_stats(outcomes)

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
    filled_count = (
        db.query(Position).filter(Position.trading_session_id == trading_session.id).count()
    )

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
