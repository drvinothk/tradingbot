"""Shared rules the three Phase 4 confirmation-filter strategies (ORB, VWAP
Pullback, EMA Micro-pullback) all need, implemented once here rather than
duplicated per strategy:

- **full-candle completion**: a strategy must only ever evaluate a signal
  once per newly-*completed* bar, never mid-bar and never twice for the same
  bar. `ConfirmationFilterStrategy.evaluate` enforces this generically by
  tracking the last bar `bucket_start` it acted on.
- **no signal while already in a position**: `get_open_position_for_run`
  backs both this guard and the generalized runner's (Phase 4 Step 5)
  `StrategyRunStatus.IN_POSITION` refresh — one query, two uses.

Deliberately does *not* try to hand every strategy a single shared lookback
window of bars: ORB needs a fixed window anchored to `strategy_run
.started_at` (which can be much older than "the last N bars" once the market
has moved on), while VWAP Pullback/EMA Micro-pullback only need a handful of
recent bars. `get_recent_completed_bars` supports both via `since`/`until`
(a fixed window) or `limit` (a trailing window) — each strategy's
`check_setup` calls it with whatever shape it actually needs.
"""

from __future__ import annotations

import uuid
from abc import abstractmethod
from datetime import datetime

from sqlalchemy.orm import Session

from app.domain.execution.models import Position, PositionStatus
from app.domain.market.models import IndicatorSnapshot, PriceBar
from app.domain.strategy.models import StrategyRun, TradeIntent
from app.modules.strategy_engine.interface import Strategy, TradeProposal

# Matches the timeframe string market_data.ingestion writes
# (f"{IndicatorEngine.timeframe_seconds}s") for the system-wide 60s bar —
# the only timeframe anything in this codebase persists.
BAR_TIMEFRAME = "60s"


def get_open_position_for_run(db: Session, strategy_run: StrategyRun) -> Position | None:
    return (
        db.query(Position)
        .join(TradeIntent, Position.trade_intent_id == TradeIntent.id)
        .filter(
            TradeIntent.strategy_run_id == strategy_run.id,
            Position.status == PositionStatus.OPEN,
        )
        .one_or_none()
    )


def get_recent_completed_bars(
    db: Session,
    instrument_id: uuid.UUID,
    timeframe: str = BAR_TIMEFRAME,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
) -> list[PriceBar]:
    """Ascending by `bucket_start` (oldest first) regardless of which
    filters are supplied — every `price_bars` row is already a *completed*
    bar (see market_data.ingestion), so there's no separate "is this bar
    done yet" check needed here.
    """
    query = db.query(PriceBar).filter(
        PriceBar.instrument_id == instrument_id, PriceBar.timeframe == timeframe
    )
    if since is not None:
        query = query.filter(PriceBar.bucket_start >= since)
    if until is not None:
        query = query.filter(PriceBar.bucket_start < until)
    query = query.order_by(PriceBar.bucket_start.desc())
    if limit is not None:
        query = query.limit(limit)
    return list(reversed(query.all()))


def get_latest_indicator_value(
    db: Session,
    instrument_id: uuid.UUID,
    indicator_name: str,
    timeframe: str = BAR_TIMEFRAME,
) -> float | None:
    """VWAP Pullback (VWAP) and EMA Micro-pullback (EMA9/EMA20) both need
    the latest persisted scalar for the underlying — `None` means "not
    warmed up yet", same convention `IndicatorEngine`/`EMACalculator`
    already use, not an error.
    """
    row = (
        db.query(IndicatorSnapshot)
        .filter(
            IndicatorSnapshot.instrument_id == instrument_id,
            IndicatorSnapshot.indicator_name == indicator_name,
            IndicatorSnapshot.timeframe == timeframe,
        )
        .order_by(IndicatorSnapshot.ts.desc())
        .first()
    )
    return float(row.value) if row is not None else None


class ConfirmationFilterStrategy(Strategy):
    """Template method: `evaluate()` applies the two generic guards above,
    then delegates the actual setup logic to `check_setup`, which receives
    the single latest completed bar (already the one `evaluate` gated on) —
    any additional history a strategy needs, it fetches itself via
    `get_recent_completed_bars`.
    """

    def __init__(self, instrument_id: uuid.UUID, timeframe: str = BAR_TIMEFRAME) -> None:
        self.instrument_id = instrument_id
        self.timeframe = timeframe
        self._last_seen_bucket_start: datetime | None = None

    def evaluate(self, db: Session, strategy_run: StrategyRun) -> TradeProposal | None:
        if get_open_position_for_run(db, strategy_run) is not None:
            return None

        latest = get_recent_completed_bars(db, self.instrument_id, self.timeframe, limit=1)
        if not latest:
            return None
        bar = latest[0]

        already_seen = (
            self._last_seen_bucket_start is not None
            and bar.bucket_start <= self._last_seen_bucket_start
        )
        if already_seen:
            return None
        self._last_seen_bucket_start = bar.bucket_start

        return self.check_setup(db, strategy_run, bar)

    @abstractmethod
    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        """Called at most once per newly-completed bar, only when this run
        has no open position — implement the strategy-specific entry logic
        here, using `get_recent_completed_bars` for any extra history."""
