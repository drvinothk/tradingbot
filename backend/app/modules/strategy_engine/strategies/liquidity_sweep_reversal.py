"""Liquidity Sweep/Reversal. Price briefly breaks a recent level — a "sweep,"
on the theory that stop-losses/liquidity rest just beyond recent swing
highs/lows — and the *same* bar's close reverses back inside the range: a
false breakout, traded in the reversal direction rather than the break
direction. The opposite shape from a continuation breakout (ORB, OI/Volume
Confirmed): there, a close beyond the range confirms the move; here, a wick
beyond the range that the close immediately rejects is what confirms it.

Deliberately does *not* reuse `common_rules.touch_and_confirm` — that helper
is for a pullback-*to*-and-continue pattern (VWAP Pullback, EMA
Micro-pullback), a different shape from break-and-reverse. Folding the two
together would either lose this strategy's wick-vs-close distinction or
VWAP/EMA's touch-tolerance-band distinction, the same "these differ in ways
that would silently change behavior" reasoning that kept `structure_level`
strategy-owned rather than shared in Batch E.

No `_fired_directions` guard, unlike ORB/OI-Volume-Confirmed: the rolling
window continuously shifts as new bars complete, so — same as VWAP
Pullback/EMA Micro-pullback — a later, genuinely new sweep-and-reversal
setup should be allowed to fire again, not be permanently suppressed after
the first one this run.
"""

from __future__ import annotations

import uuid
from datetime import date

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


class LiquiditySweepReversalStrategy(ConfirmationFilterStrategy):
    def __init__(
        self,
        instrument_id: uuid.UUID,
        expiry_date: date,
        ranking_config: StrikeRankingConfig = StrikeRankingConfig(),
        lookback_bars: int = 10,
        stop_pct: float = 0.10,
        target_pct: float = 0.16,
        trail_activation_fraction: float = 0.5,
        trail_lock_fraction: float = 0.5,
        timeframe: str = BAR_TIMEFRAME,
    ) -> None:
        super().__init__(instrument_id, timeframe)
        self.expiry_date = expiry_date
        self.ranking_config = ranking_config
        self.lookback_bars = lookback_bars
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.trail_activation_fraction = trail_activation_fraction
        self.trail_lock_fraction = trail_lock_fraction

    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        bars = get_recent_completed_bars(
            db, self.instrument_id, self.timeframe, limit=self.lookback_bars + 1
        )
        if len(bars) < self.lookback_bars + 1:
            return None
        window_bars = bars[:-1]  # bars[-1] is latest_bar itself

        window_high, window_low = compute_range_high_low(window_bars)
        high = float(latest_bar.high)
        low = float(latest_bar.low)
        close = float(latest_bar.close)

        # Swept the recent high but closed back below it -> false breakout,
        # trade the reversal down. Swept the recent low but closed back
        # above it -> trade the reversal up. A bar that sweeps *and* closes
        # beyond the level isn't a sweep at all (that's a genuine breakout,
        # ORB/OI-Volume-Confirmed's territory), so this only fires when the
        # close actually rejects back inside the range.
        swept_high = high > window_high and close <= window_high
        swept_low = low < window_low and close >= window_low

        if swept_high:
            option_type, structure_level = OptionType.PE, window_high
        elif swept_low:
            option_type, structure_level = OptionType.CE, window_low
        else:
            return None

        ranked = rank_from_latest_snapshot(
            db, self.instrument_id, self.expiry_date, self.ranking_config
        )
        top = pick_top_by_type(ranked, option_type)
        if top is None:
            return None

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
                "strategy": "liquidity_sweep_reversal",
                "window_high": window_high,
                "window_low": window_low,
                "strike_score": top.score,
                "breakdown": top.breakdown,
            },
        )
