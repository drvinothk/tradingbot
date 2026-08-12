"""OI/Volume Confirmed. A rolling-window breakout — same shape as ORB, but
anchored to the last `lookback_bars` completed bars instead of a fixed,
session-start-anchored opening range — where the actual "OI/Volume confirmed"
character comes from the *strike selection*, not the entry trigger: the
strike-ranking engine runs in a chain-participation-weighted mode
(`OI_VOLUME_RANKING_CONFIG` below) that both scores open-interest/volume more
heavily than the default and hard-filters out any contract below a minimum
OI/volume floor, so a breakout only trades through a strike the market has
genuine participation in.

Like ORB, each breakout direction only fires once per run (tracked in-memory)
— a rolling window that keeps re-confirming the same ongoing move would
otherwise be able to re-signal on nearly every subsequent bar once the
"no signal while in position" guard's target/stop has been hit and a new
position opened, which this avoids the same way ORB does.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.orm import Session

from app.domain.market.models import Instrument, OptionType, PriceBar
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

# Chain-participation-weighted mode: OI/volume weighted roughly double the
# default (0.35 vs 0.20 each), spread/premium-fit/depth weighted down to
# compensate, plus a hard participation floor (min_oi/min_volume) so a
# thinly-traded contract can never be the top pick regardless of how well it
# scores otherwise. Thresholds sit comfortably inside MockBrokerAdapter's
# randint(1000, 50000) OI / randint(100, 5000) volume ranges, so this can
# actually fire in paper mode rather than being permanently starved.
OI_VOLUME_RANKING_CONFIG = StrikeRankingConfig(
    weight_spread=0.15,
    weight_volume=0.35,
    weight_oi=0.35,
    weight_premium_fit=0.10,
    weight_depth=0.05,
    min_oi=5000,
    min_volume=500,
)


class OIVolumeConfirmedStrategy(ConfirmationFilterStrategy):
    def __init__(
        self,
        instrument_id: uuid.UUID,
        expiry_date: date,
        ranking_config: StrikeRankingConfig = OI_VOLUME_RANKING_CONFIG,
        lookback_bars: int = 5,
        stop_pct: float = 0.11,
        target_pct: float = 0.18,
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
        self._fired_directions: set[OptionType] = set()

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
        close = float(latest_bar.close)

        if close > window_high and OptionType.CE not in self._fired_directions:
            option_type, structure_level = OptionType.CE, window_low
        elif close < window_low and OptionType.PE not in self._fired_directions:
            option_type, structure_level = OptionType.PE, window_high
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
        instrument = db.get(Instrument, self.instrument_id)
        tick_size = float(instrument.tick_size) if instrument is not None else 0.0
        stop_price, target_price = compute_stop_target(
            entry_price, self.stop_pct, self.target_pct, tick_size
        )

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
                "strategy": "oi_volume_confirmed",
                "window_high": window_high,
                "window_low": window_low,
                "strike_score": top.score,
                "breakdown": top.breakdown,
            },
        )
