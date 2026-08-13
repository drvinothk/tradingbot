"""VWAP Pullback. Trend bias comes from price vs session VWAP; a setup
fires when the *previous* completed bar pulls back to touch VWAP (within
`pullback_tolerance_frac`) without breaking through it, and the latest
completed bar closes back on the trend side, beyond the pullback bar's own
high/low — the confirmation candle that says the pullback is over and the
trend has resumed, not just a random tick back above VWAP.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time
from typing import Literal

from sqlalchemy.orm import Session

from app.core.clock import IST
from app.domain.market.models import Instrument, OptionType, PriceBar
from app.domain.strategy.models import SignalSide, StrategyRun
from app.modules.strategy_engine.common_rules import (
    BAR_TIMEFRAME,
    ConfirmationFilterStrategy,
    compute_stop_target,
    get_latest_indicator_value,
    get_recent_completed_bars,
    touch_and_confirm,
)
from app.modules.strategy_engine.env_metrics import get_latest_env_metrics
from app.modules.strategy_engine.interface import EnvPayload, TradeProposal
from app.modules.strategy_engine.strike_ranking.engine import (
    StrikeRankingConfig,
    pick_top_by_type,
    rank_from_latest_snapshot,
)

QTY_LOTS = 1

logger = logging.getLogger("app.strategy_engine.vwap_pullback")


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
        trend_lookback_bars: int = 20,
        max_vwap_crosses_in_lookback: int = 3,
        min_trend_side_fraction: float = 0.70,
    ) -> None:
        super().__init__(instrument_id, timeframe)
        self.expiry_date = expiry_date
        self.ranking_config = ranking_config
        self.pullback_tolerance_frac = pullback_tolerance_frac
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.trail_activation_fraction = trail_activation_fraction
        self.trail_lock_fraction = trail_lock_fraction
        self.trend_lookback_bars = trend_lookback_bars
        self.max_vwap_crosses_in_lookback = max_vwap_crosses_in_lookback
        self.min_trend_side_fraction = min_trend_side_fraction

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

        direction = touch_and_confirm(prev_bar, latest_bar, vwap, self.pullback_tolerance_frac)
        if direction is None:
            return None

        trend = self._trend_direction(db, vwap, latest_bar)
        if trend != direction:
            logger.info(
                "run %s: vwap trend/choppiness filter blocked a %s setup (trend=%s)",
                strategy_run.id,
                direction,
                trend,
            )
            return None

        if direction == "bullish":
            option_type, structure_level = OptionType.CE, float(prev_bar.low)
        else:
            option_type, structure_level = OptionType.PE, float(prev_bar.high)

        ranked = rank_from_latest_snapshot(
            db, self.instrument_id, self.expiry_date, self.ranking_config
        )
        top = pick_top_by_type(ranked, option_type)
        if top is None:
            return None

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
                "strategy": "vwap_pullback",
                "vwap": vwap,
                "strike_score": top.score,
                "breakdown": top.breakdown,
                "env": self._env_metrics(db),
            },
        )

    def _trend_direction(
        self, db: Session, current_vwap: float, latest_bar: PriceBar
    ) -> Literal["bullish", "bearish"] | None:
        """Which side of VWAP the last `trend_lookback_bars` bars mostly sat
        on, or `None` if there isn't enough history yet or the market's too
        choppy to call a trend. Compares each bar's close against *today's
        current* VWAP, not the VWAP that was actually live when that bar
        closed — VWAP is only ever queryable as a single latest scalar
        (`common_rules.get_latest_indicator_value`), no historical per-bar
        series exists. A reasonable approximation over a lookback this
        short (VWAP moves slowly relative to price), not an exact one.

        Bars are bounded to `latest_bar`'s own IST calendar day (`since=
        day_start`, derived from the bar's own timestamp, same "anchor to
        the bar, not wall-clock now()" reasoning `ORBStrategy` uses for its
        opening-range window) — without this, `limit=N` alone would pull
        bars from the *previous* trading day for the first ~N minutes of
        every day after the instrument's first, comparing yesterday's
        closes against today's freshly-relevant VWAP. `len(bars) <
        trend_lookback_bars` then correctly means "today doesn't have N
        bars yet," not just "this instrument has never had N bars."
        """
        day_start = datetime.combine(
            latest_bar.bucket_start.astimezone(IST).date(), time.min, tzinfo=IST
        )
        bars = get_recent_completed_bars(
            db, self.instrument_id, self.timeframe, since=day_start, limit=self.trend_lookback_bars
        )
        if len(bars) < self.trend_lookback_bars:
            return None

        sides = ["bullish" if float(b.close) > current_vwap else "bearish" for b in bars]
        crosses = sum(1 for i in range(1, len(sides)) if sides[i] != sides[i - 1])
        if crosses > self.max_vwap_crosses_in_lookback:
            return None

        bullish_frac = sides.count("bullish") / len(sides)
        bearish_frac = sides.count("bearish") / len(sides)
        if bullish_frac >= self.min_trend_side_fraction:
            return "bullish"
        if bearish_frac >= self.min_trend_side_fraction:
            return "bearish"
        return None

    def _env_metrics(self, db: Session) -> EnvPayload:
        env = get_latest_env_metrics(db, self.instrument_id)
        if env is None:
            self._log_once(
                logger, "env_metrics", "VWAP env metrics unavailable; env filters disabled"
            )
            return {}
        return env
