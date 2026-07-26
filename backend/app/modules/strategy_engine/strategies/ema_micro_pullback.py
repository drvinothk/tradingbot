"""EMA Micro-pullback. Trend bias comes from EMA9 vs EMA20 (EMA9 above EMA20
is bullish, below is bearish); a setup fires when the *previous* completed
bar pulls back to touch EMA9 (within `pullback_tolerance_frac`) and the
latest completed bar closes back through EMA9 in the trend direction, beyond
the pullback bar's own high/low — the tightest, fastest-firing of the three
strategies (hence the tightest stop/target), since a pullback to the fast
average is a much smaller move than an opening-range breakout or a pullback
all the way to VWAP.
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
    get_latest_indicator_value,
    get_recent_completed_bars,
)
from app.modules.strategy_engine.interface import TradeProposal
from app.modules.strategy_engine.strike_ranking.engine import (
    StrikeRankingConfig,
    rank_from_latest_snapshot,
)

QTY_LOTS = 1


class EMAMicroPullbackStrategy(ConfirmationFilterStrategy):
    def __init__(
        self,
        instrument_id: uuid.UUID,
        expiry_date: date,
        ranking_config: StrikeRankingConfig = StrikeRankingConfig(),
        pullback_tolerance_frac: float = 0.001,
        stop_pct: float = 0.08,
        target_pct: float = 0.12,
        trail_activation_fraction: float = 0.5,
        trail_lock_fraction: float = 0.5,
        timeframe: str = BAR_TIMEFRAME,
    ) -> None:
        super().__init__(instrument_id, timeframe)
        self.expiry_date = expiry_date
        self.ranking_config = ranking_config
        self.pullback_tolerance_frac = pullback_tolerance_frac
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.trail_activation_fraction = trail_activation_fraction
        self.trail_lock_fraction = trail_lock_fraction

    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        bars = get_recent_completed_bars(db, self.instrument_id, self.timeframe, limit=2)
        if len(bars) < 2:
            return None
        prev_bar = bars[0]  # bars[1] is latest_bar itself

        ema9 = get_latest_indicator_value(db, self.instrument_id, "EMA9", self.timeframe)
        ema20 = get_latest_indicator_value(db, self.instrument_id, "EMA20", self.timeframe)
        if ema9 is None or ema20 is None or ema9 == ema20:
            return None

        band = ema9 * self.pullback_tolerance_frac
        close = float(latest_bar.close)
        bullish_trend = ema9 > ema20
        bearish_trend = ema9 < ema20

        # "Touched" means the pullback bar's extreme landed within `band` of
        # EMA9 — not merely "somewhere below/above it", which would also
        # match a bar that blew straight through the average.
        touched_from_above = abs(float(prev_bar.low) - ema9) <= band
        bullish_confirmation = close > float(prev_bar.high) and close > ema9
        touched_from_below = abs(float(prev_bar.high) - ema9) <= band
        bearish_confirmation = close < float(prev_bar.low) and close < ema9

        if bullish_trend and touched_from_above and bullish_confirmation:
            option_type = OptionType.CE
        elif bearish_trend and touched_from_below and bearish_confirmation:
            option_type = OptionType.PE
        else:
            return None

        ranked = rank_from_latest_snapshot(
            db, self.instrument_id, self.expiry_date, self.ranking_config
        )
        top = next((r for r in ranked if r.option_type == option_type), None)
        if top is None:
            return None

        entry_price = top.ltp
        stop_price = round(entry_price * (1 - self.stop_pct), 2)
        target_price = round(entry_price * (1 + self.target_pct), 2)

        return TradeProposal(
            option_contract_id=top.option_contract_id,
            side=SignalSide.BUY,
            qty_lots=QTY_LOTS,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            trail_activation_fraction=self.trail_activation_fraction,
            trail_lock_fraction=self.trail_lock_fraction,
            structure_level=ema9,
            payload={
                "strategy": "ema_micro_pullback",
                "ema9": ema9,
                "ema20": ema20,
                "strike_score": top.score,
                "breakdown": top.breakdown,
            },
        )
