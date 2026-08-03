"""Opening Range Breakout. The opening range is anchored to `strategy_run
.started_at` (not "the first bar this process happened to observe in
memory") specifically so restarting the runner mid-session doesn't shift the
range — a crash-safe anchor already available on every StrategyRun row,
matching the same reasoning `PositionManager`'s resume-on-restart hook uses
elsewhere in this codebase.

Once `or_minutes` of completed bars have passed, the first subsequent bar
that *closes* beyond the range (not just wicks through it) fires a
breakout in that direction; each direction only fires once per run (tracked
in-memory, same durability class as everything else a `Strategy` instance
holds — a process restart loses this the same way it loses the runner
thread itself), but the opposite direction can still fire later, since a
stop-out-then-reverse is a real pattern this shouldn't suppress.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.domain.market.models import OptionType, PriceBar
from app.domain.strategy.models import SignalSide, StrategyRun
from app.modules.strategy_engine.common_rules import (
    BAR_TIMEFRAME,
    ConfirmationFilterStrategy,
    compute_range_high_low,
    compute_stop_target,
    get_recent_completed_bars,
)
from app.modules.strategy_engine.interface import TradeProposal
from app.modules.strategy_engine.strike_ranking.engine import (
    StrikeRankingConfig,
    pick_top_by_type,
    rank_from_latest_snapshot,
)

QTY_LOTS = 1


class ORBStrategy(ConfirmationFilterStrategy):
    def __init__(
        self,
        instrument_id: uuid.UUID,
        expiry_date: date,
        ranking_config: StrikeRankingConfig = StrikeRankingConfig(),
        or_minutes: int = 15,
        stop_pct: float = 0.12,
        target_pct: float = 0.20,
        trail_activation_fraction: float = 0.6,
        trail_lock_fraction: float = 0.4,
        timeframe: str = BAR_TIMEFRAME,
    ) -> None:
        super().__init__(instrument_id, timeframe)
        self.expiry_date = expiry_date
        self.ranking_config = ranking_config
        self.or_minutes = or_minutes
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.trail_activation_fraction = trail_activation_fraction
        self.trail_lock_fraction = trail_lock_fraction
        self._fired_directions: set[OptionType] = set()

    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        or_start = strategy_run.started_at
        or_end = or_start + timedelta(minutes=self.or_minutes)
        if latest_bar.bucket_start < or_end:
            return None  # still inside (or before) the opening range window

        or_bars = get_recent_completed_bars(
            db, self.instrument_id, self.timeframe, since=or_start, until=or_end
        )
        if not or_bars:
            return None

        or_high, or_low = compute_range_high_low(or_bars)
        close = float(latest_bar.close)

        if close > or_high and OptionType.CE not in self._fired_directions:
            option_type, structure_level = OptionType.CE, or_low
        elif close < or_low and OptionType.PE not in self._fired_directions:
            option_type, structure_level = OptionType.PE, or_high
        else:
            return None

        ranked = rank_from_latest_snapshot(
            db, self.instrument_id, self.expiry_date, self.ranking_config
        )
        top = pick_top_by_type(ranked, option_type)
        if top is None:
            return None

        self._fired_directions.add(option_type)

        entry_price = top.ltp
        stop_price, target_price = compute_stop_target(entry_price, self.stop_pct, self.target_pct)

        return TradeProposal(
            option_contract_id=top.option_contract_id,
            side=SignalSide.BUY,
            qty_lots=QTY_LOTS,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            trail_activation_fraction=self.trail_activation_fraction,
            trail_lock_fraction=self.trail_lock_fraction,
            structure_level=structure_level,
            payload={
                "strategy": "orb",
                "or_high": or_high,
                "or_low": or_low,
                "strike_score": top.score,
                "breakdown": top.breakdown,
            },
        )
