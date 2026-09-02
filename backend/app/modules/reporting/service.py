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
from app.modules.reporting.costs import estimate_entry_order_cost, estimate_exit_leg_cost


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
    # Magnitude (>=0) of the single worst closed trade's realized_pnl -- same
    # sign convention as max_drawdown (a positive size, not a signed loss),
    # so the two read consistently side by side in the UI. 0.0 when there
    # are no losing trades, distinct from max_drawdown's own "no trades at
    # all" 0.0 default -- both collapse to the same value there, which is
    # fine since neither has anything to report yet.
    largest_single_loss: float
    # Magnitude of the single best closed trade's realized_pnl -- 0.0 when
    # there are no winning trades, same "no trades at all" collapse as
    # largest_single_loss/max_drawdown above.
    largest_single_win: float
    total_realized_pnl: float
    # Exit-side only (TradeOutcome.slippage) -- see total_entry_slippage
    # below for the open-side counterpart. Kept separate rather than
    # combined into one number so each leg of a trade's execution quality
    # stays independently visible.
    total_slippage: float
    # Position.entry_slippage summed across every closed trade this scope
    # covers -- open-side counterpart of total_slippage above. Read once per
    # position (identical across every exit leg of the same position, same
    # "frozen at open, never recomputed" reasoning as entry_price itself),
    # not summed per-leg like total_slippage is.
    total_entry_slippage: float
    # Approximate real brokerage/STT/exchange/SEBI/stamp/GST cost across
    # every closed trade -- see reporting.costs.estimate_trade_cost's own
    # docstring for the source and reasoning. Not part of realized_pnl
    # (which is the strategy's raw price-only P&L); this is a separate,
    # approximate figure surfaced alongside it.
    total_cost: float


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
    entry_slippage: float
    cost: float


@dataclass(frozen=True)
class _PositionAccumulator:
    """Intermediate per-position accumulator for `_collapse_to_trades` —
    tracks enough to compute the position's single entry-order cost once
    at the end (full original qty, not any one leg's own slice), separate
    from `exit_cost`, which is a running sum of each leg's own exit-order
    cost (one real broker order per leg, per `reporting.costs`'s own
    docstring)."""

    closed_at: datetime
    realized_pnl: float
    slippage: float
    entry_price: float
    # Identical across every leg (see _collapse_to_trades) -- carried
    # through the accumulator the same way entry_price is, not summed.
    entry_slippage: float
    total_qty: int
    exit_cost: float


def _collapse_to_trades(
    outcomes: list[TradeOutcome],
    entry_slippage_by_position: dict[uuid.UUID, float] | None = None,
) -> list[_TradeRow]:
    entry_slippage_by_position = entry_slippage_by_position or {}
    by_position: dict[uuid.UUID, _PositionAccumulator] = {}
    for o in outcomes:
        leg_exit_cost = estimate_exit_leg_cost(float(o.exit_price), o.qty)
        existing = by_position.get(o.position_id)
        if existing is None:
            by_position[o.position_id] = _PositionAccumulator(
                closed_at=o.closed_at,
                realized_pnl=float(o.realized_pnl),
                slippage=float(o.slippage),
                # Identical across every leg of the same position (the
                # same original entry fill, repeated per TradeOutcome row
                # by the multi-leg exit engine) -- see exit_legs.py's own
                # TradeOutcome construction.
                entry_price=float(o.entry_price),
                # 0.0 (not fabricated as "no slippage", just the safe
                # default) for a position predating the entry_slippage
                # column -- same convention as the exit side's own
                # lost-trigger-price case.
                entry_slippage=entry_slippage_by_position.get(o.position_id, 0.0),
                total_qty=o.qty,
                exit_cost=leg_exit_cost,
            )
        else:
            by_position[o.position_id] = _PositionAccumulator(
                # A staged trade's "close time" is its last leg's.
                closed_at=max(existing.closed_at, o.closed_at),
                realized_pnl=existing.realized_pnl + float(o.realized_pnl),
                slippage=existing.slippage + float(o.slippage),
                entry_price=existing.entry_price,
                entry_slippage=existing.entry_slippage,
                # Every leg's own qty is a slice of the position's one real
                # entry order's full quantity -- summing them back recovers
                # it, regardless of how many exit legs there are.
                total_qty=existing.total_qty + o.qty,
                exit_cost=existing.exit_cost + leg_exit_cost,
            )
    return [
        _TradeRow(
            closed_at=acc.closed_at,
            realized_pnl=acc.realized_pnl,
            slippage=acc.slippage,
            entry_slippage=acc.entry_slippage,
            # The position's one real entry order, priced on its full
            # original quantity, charged exactly once here -- plus every
            # exit leg's own order cost, already summed above.
            cost=acc.exit_cost + estimate_entry_order_cost(acc.entry_price, acc.total_qty),
        )
        for acc in by_position.values()
    ]


def _compute_stats(
    outcomes: list[TradeOutcome],
    entry_slippage_by_position: dict[uuid.UUID, float] | None = None,
) -> PerformanceStats:
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
            largest_single_loss=0.0,
            largest_single_win=0.0,
            total_realized_pnl=0.0,
            total_slippage=0.0,
            total_entry_slippage=0.0,
            total_cost=0.0,
        )

    ordered = sorted(
        _collapse_to_trades(outcomes, entry_slippage_by_position), key=lambda o: o.closed_at
    )
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
        largest_single_loss=abs(min(losses)) if losses else 0.0,
        largest_single_win=max(wins) if wins else 0.0,
        total_realized_pnl=sum(pnls),
        total_slippage=sum(float(o.slippage) for o in ordered),
        total_entry_slippage=sum(o.entry_slippage for o in ordered),
        total_cost=sum(o.cost for o in ordered),
    )


def _entry_slippage_by_position(
    db: Session, position_ids: list[uuid.UUID]
) -> dict[uuid.UUID, float]:
    """One extra lightweight query -- Position.entry_slippage isn't on
    TradeOutcome, so it can't be read off the outcomes list `_compute_stats`
    already has. Missing/null entries are simply absent from the dict;
    `_collapse_to_trades` already defaults a lookup miss to 0.0."""
    if not position_ids:
        return {}
    return {
        position_id: float(entry_slippage)
        for position_id, entry_slippage in db.query(Position.id, Position.entry_slippage)
        .filter(Position.id.in_(position_ids), Position.entry_slippage.isnot(None))
        .all()
    }


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

    outcomes = outcomes_query.all()
    stats = _compute_stats(
        outcomes, _entry_slippage_by_position(db, [o.position_id for o in outcomes])
    )

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
    stats = _compute_stats(
        outcomes, _entry_slippage_by_position(db, [o.position_id for o in outcomes])
    )

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
