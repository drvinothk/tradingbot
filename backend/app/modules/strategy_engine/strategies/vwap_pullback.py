"""VWAP Pullback. Trend bias comes from price vs session VWAP; a setup
fires when the *previous* completed bar pulls back to touch VWAP (within
`pullback_tolerance_frac`) without breaking through it, and the latest
completed bar closes back on the trend side, beyond the pullback bar's own
high/low — the confirmation candle that says the pullback is over and the
trend has resumed, not just a random tick back above VWAP.
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


class VWAPPullbackStrategy(ConfirmationFilterStrategy):
    def __init__(
        self,
        instrument_id: uuid.UUID,
        expiry_date: date,
        ranking_config: StrikeRankingConfig = StrikeRankingConfig(),
        pullback_tolerance_frac: float = 0.0015,
        stop_pct: float = 0.10,
        target_pct: float = 0.15,
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

        vwap = get_latest_indicator_value(db, self.instrument_id, "VWAP", self.timeframe)
        if vwap is None:
            return None

        band = vwap * self.pullback_tolerance_frac
        close = float(latest_bar.close)

        # "Touched" means the pullback bar's extreme landed within `band` of
        # VWAP — either just above or just below it (testing it as
        # support/resistance) — not merely "somewhere below/above VWAP",
        # which would also match a bar that blew straight through it.
        touched_from_above = abs(float(prev_bar.low) - vwap) <= band
        bullish_confirmation = close > float(prev_bar.high) and close > vwap
        touched_from_below = abs(float(prev_bar.high) - vwap) <= band
        bearish_confirmation = close < float(prev_bar.low) and close < vwap

        if touched_from_above and bullish_confirmation:
            option_type, structure_level = OptionType.CE, float(prev_bar.low)
        elif touched_from_below and bearish_confirmation:
            option_type, structure_level = OptionType.PE, float(prev_bar.high)
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
            structure_level=structure_level,
            payload={
                "strategy": "vwap_pullback",
                "vwap": vwap,
                "strike_score": top.score,
                "breakdown": top.breakdown,
            },
        )
