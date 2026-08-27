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
    DEFAULT_STRUCTURE_BREAK_ATR_MULTIPLIER,
    DEFAULT_STRUCTURE_BREAK_PERSISTENCE_SECONDS,
    ConfirmationFilterStrategy,
    compute_stop_target,
    get_latest_indicator_value_with_ts,
    get_recent_completed_bars,
    resolve_structure_break_buffer,
    touch_and_confirm,
)
from app.modules.strategy_engine.env_metrics import get_latest_env_metrics
from app.modules.strategy_engine.interface import EnvPayload, TradeProposal
from app.modules.strategy_engine.strike_ranking.engine import (
    StrikeRankingConfig,
    pick_top_by_type,
    rank_from_latest_snapshot,
)

logger = logging.getLogger("app.strategy_engine.vwap_pullback")

# Under healthy WS ingestion VWAP updates every tick (sub-second). If the
# latest persisted VWAP is older than this relative to the bar being
# evaluated, the live volume-weighted VWAP feed has effectively stopped —
# `get_latest_indicator_value` would otherwise keep returning a stale,
# possibly days-old frozen scalar (real incident 2026-08-27: the underlying
# switched to an index feed with no traded volume, so VWAP could never
# accumulate and the strategy traded a 2-day-old constant). Better to sit
# out than to trade a bias derived from a dead indicator.
DEFAULT_VWAP_MAX_STALENESS_SECONDS = 300.0


class VWAPPullbackStrategy(ConfirmationFilterStrategy):
    def __init__(
        self,
        instrument_id: uuid.UUID,
        expiry_date: date,
        ranking_config: StrikeRankingConfig = StrikeRankingConfig(),
        qty_lots: int = 1,
        pullback_tolerance_frac: float = 0.0015,
        stop_pct: float = 0.10,
        target_pct: float = 0.15,
        trail_activation_fraction: float = 0.5,
        trail_lock_fraction: float = 0.5,
        timeframe: str = BAR_TIMEFRAME,
        trend_lookback_bars: int = 20,
        max_vwap_crosses_in_lookback: int = 3,
        min_trend_side_fraction: float = 0.70,
        structure_break_atr_multiplier: float = DEFAULT_STRUCTURE_BREAK_ATR_MULTIPLIER,
        structure_break_persistence_seconds: float = DEFAULT_STRUCTURE_BREAK_PERSISTENCE_SECONDS,
        vwap_max_staleness_seconds: float = DEFAULT_VWAP_MAX_STALENESS_SECONDS,
    ) -> None:
        super().__init__(instrument_id, timeframe)
        self.expiry_date = expiry_date
        self.ranking_config = ranking_config
        self.qty_lots = qty_lots
        self.pullback_tolerance_frac = pullback_tolerance_frac
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.trail_activation_fraction = trail_activation_fraction
        self.trail_lock_fraction = trail_lock_fraction
        self.trend_lookback_bars = trend_lookback_bars
        self.max_vwap_crosses_in_lookback = max_vwap_crosses_in_lookback
        self.min_trend_side_fraction = min_trend_side_fraction
        self.structure_break_atr_multiplier = structure_break_atr_multiplier
        self.structure_break_persistence_seconds = structure_break_persistence_seconds
        self.vwap_max_staleness_seconds = vwap_max_staleness_seconds

    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        bars = get_recent_completed_bars(db, self.instrument_id, self.timeframe, limit=2)
        if len(bars) < 2:
            return None
        prev_bar = bars[0]  # bars[1] is latest_bar itself

        vwap_row = get_latest_indicator_value_with_ts(
            db, self.instrument_id, "VWAP", self.timeframe
        )
        if vwap_row is None:
            return None
        vwap, vwap_ts = vwap_row
        vwap_age_seconds = (latest_bar.bucket_start - vwap_ts).total_seconds()
        if vwap_age_seconds > self.vwap_max_staleness_seconds:
            self._log_once(
                logger,
                "vwap_stale",
                "run %s: latest VWAP is %.0fs stale (> %.0fs) — sitting out until a "
                "live volume-bearing VWAP feed is restored",
                strategy_run.id,
                vwap_age_seconds,
                self.vwap_max_staleness_seconds,
            )
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
            qty_lots=self.qty_lots,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            trail_activation_fraction=self.trail_activation_fraction,
            trail_lock_fraction=self.trail_lock_fraction,
            structure_level=structure_level,
            structure_break_buffer=resolve_structure_break_buffer(
                db, self.instrument_id, self.structure_break_atr_multiplier, self.timeframe
            ),
            structure_break_persistence_seconds=self.structure_break_persistence_seconds,
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
        env = get_latest_env_metrics(db, self.instrument_id, self.expiry_date)
        if env is None:
            self._log_once(
                logger, "env_metrics", "VWAP env metrics unavailable; env filters disabled"
            )
            return {}
        return env
