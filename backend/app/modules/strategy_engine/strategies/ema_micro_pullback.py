"""EMA Micro-pullback. Trend bias comes from EMA9 vs EMA20; a setup only
fires on a genuine, accelerating trend confirmed by four independent gates,
all of which must pass on the same bar:

1. **Expansion filter** — the last `ema_expansion_lookback` EMA9-EMA20
   spreads must be same-signed and monotonically widening (bullish:
   `spread > 0` and increasing every step; bearish: `spread < 0` and
   *more* negative every step, i.e. `abs(spread)` increasing) — a flat or
   narrowing spread means the trend isn't actually accelerating, whatever
   the raw EMA9-vs-EMA20 ordering says. Generalized to any
   `ema_expansion_lookback`, not hardcoded to 3 comparisons, so the
   parameter actually changes behavior rather than just how much data gets
   fetched.
2. **Bone Zone pullback** — the setup bar's low (bullish) / high (bearish)
   must land *between* EMA9 and EMA20 (the "bone zone" — the gap between
   the fast and slow average), while the bar still *closes* back beyond
   EMA20 in the trend direction (a shallow pullback that respects the
   trend, not a break of the slower average); the next bar's close beyond
   the setup bar's own high/low is the confirmation. This replaces the
   single-level `touch_and_confirm` (against EMA9 alone) VWAP Pullback
   still uses — a two-level zone check needs its own logic, not a shared
   helper with a single `reference_level`.
3. **Time windows & session trade cap** — entries only fire inside
   `ema_morning_window`/`ema_afternoon_window` (a deliberate midday gap,
   since this is the fastest-firing, most chop-sensitive of the three
   Phase 4 strategies), and stop once `ema_max_trades_per_session` trades
   have fired this run. `trades_fired_count` resets if `strategy_run.id`
   ever changes on this instance — defensive: under the current
   architecture a `Strategy` instance is always constructed fresh per
   `StrategyRun` (see `api.v1.strategies._build_strategy`), so this reset
   can't actually trigger today, but costs nothing to have in place if
   that ever changes.
4. **Candle body ratio** — `common_rules.compute_body_ratio` (shared with
   OI/Volume Confirmed, which needs the identical computation over its own
   bar window) over the last 10 bars must be at least `min_body_ratio` — a
   chop/indecision filter (small bodies relative to range = wicky,
   indecisive candles).

Each skip reason logs once per run via `ConfirmationFilterStrategy
._log_once` (see its own docstring) — `check_setup` runs on every new
completed bar for the rest of the session once triggered, so an un-gated
skip log would repeat for hours on a filtered-out day. A real fire logs
unconditionally, every time (entry/stop/target/structure_level), since
those are inherently rare, meaningful events.

`pullback_tolerance_frac` from the old touch_and_confirm-based version is
gone — Bone Zone's zone-membership check replaces it, there's no tolerance
band left to configure.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, time
from typing import Literal

from sqlalchemy.orm import Session

from app.core.clock import to_ist
from app.domain.market.models import Instrument, OptionType, PriceBar
from app.domain.strategy.models import SignalSide, StrategyRun
from app.modules.strategy_engine.common_rules import (
    BAR_TIMEFRAME,
    DEFAULT_STRUCTURE_BREAK_ATR_MULTIPLIER,
    DEFAULT_STRUCTURE_BREAK_PERSISTENCE_SECONDS,
    ConfirmationFilterStrategy,
    _parse_hhmm,
    compute_body_ratio,
    compute_stop_target,
    get_recent_completed_bars,
    get_recent_indicator_values,
    resolve_structure_break_buffer,
)
from app.modules.strategy_engine.env_metrics import get_latest_env_metrics
from app.modules.strategy_engine.interface import TradeProposal
from app.modules.strategy_engine.strike_ranking.engine import (
    StrikeRankingConfig,
    pick_top_by_type,
    rank_from_latest_snapshot,
)

QTY_LOTS = 1
BODY_RATIO_LOOKBACK_BARS = 10

logger = logging.getLogger("app.strategy_engine.ema_micro_pullback")


def _bone_zone_pullback(
    setup_bar: PriceBar, entry_bar: PriceBar, ema9: float, ema20: float, *, bullish: bool
) -> bool:
    zone_low, zone_high = min(ema9, ema20), max(ema9, ema20)
    if bullish:
        return (
            zone_low <= float(setup_bar.low) <= zone_high
            and float(setup_bar.close) > ema20
            and float(entry_bar.close) > float(setup_bar.high)
        )
    return (
        zone_low <= float(setup_bar.high) <= zone_high
        and float(setup_bar.close) < ema20
        and float(entry_bar.close) < float(setup_bar.low)
    )


class EMAMicroPullbackStrategy(ConfirmationFilterStrategy):
    def __init__(
        self,
        instrument_id: uuid.UUID,
        expiry_date: date,
        ranking_config: StrikeRankingConfig = StrikeRankingConfig(),
        stop_pct: float = 0.08,
        target_pct: float = 0.12,
        trail_activation_fraction: float = 0.5,
        trail_lock_fraction: float = 0.5,
        timeframe: str = BAR_TIMEFRAME,
        ema_expansion_lookback: int = 3,
        min_body_ratio: float = 0.40,
        ema_morning_window_start: str = "09:31",
        ema_morning_window_end: str = "11:00",
        ema_afternoon_window_start: str = "13:00",
        ema_afternoon_window_end: str = "15:00",
        ema_max_trades_per_session: int = 3,
        structure_break_atr_multiplier: float = DEFAULT_STRUCTURE_BREAK_ATR_MULTIPLIER,
        structure_break_persistence_seconds: float = DEFAULT_STRUCTURE_BREAK_PERSISTENCE_SECONDS,
    ) -> None:
        super().__init__(instrument_id, timeframe)
        self.expiry_date = expiry_date
        self.ranking_config = ranking_config
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.trail_activation_fraction = trail_activation_fraction
        self.trail_lock_fraction = trail_lock_fraction
        self.ema_expansion_lookback = ema_expansion_lookback
        self.min_body_ratio = min_body_ratio
        self.ema_morning_window_start = _parse_hhmm(ema_morning_window_start)
        self.ema_morning_window_end = _parse_hhmm(ema_morning_window_end)
        self.ema_afternoon_window_start = _parse_hhmm(ema_afternoon_window_start)
        self.ema_afternoon_window_end = _parse_hhmm(ema_afternoon_window_end)
        self.ema_max_trades_per_session = ema_max_trades_per_session
        self.structure_break_atr_multiplier = structure_break_atr_multiplier
        self.structure_break_persistence_seconds = structure_break_persistence_seconds
        self.trades_fired_count = 0
        self._current_run_id: uuid.UUID | None = None

    def _within_trade_windows(self, t: time) -> bool:
        return (
            self.ema_morning_window_start <= t <= self.ema_morning_window_end
            or self.ema_afternoon_window_start <= t <= self.ema_afternoon_window_end
        )

    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        if self._current_run_id != strategy_run.id:
            self._current_run_id = strategy_run.id
            self.trades_fired_count = 0

        bar_time = to_ist(latest_bar.bucket_start).time()
        if not self._within_trade_windows(bar_time):
            self._log_once(
                logger, "time_window",
                "run %s: bar time %s outside EMA morning/afternoon windows",
                strategy_run.id, bar_time.strftime("%H:%M"),
            )
            return None

        if self.trades_fired_count >= self.ema_max_trades_per_session:
            self._log_once(
                logger, "max_trades",
                "run %s: max trades per session reached (%d)",
                strategy_run.id, self.ema_max_trades_per_session,
            )
            return None

        bars = get_recent_completed_bars(
            db, self.instrument_id, self.timeframe, limit=BODY_RATIO_LOOKBACK_BARS
        )
        if len(bars) < BODY_RATIO_LOOKBACK_BARS:
            return None  # not enough bars yet for the body-ratio window

        body_ratio = compute_body_ratio(bars)
        if body_ratio < self.min_body_ratio:
            self._log_once(
                logger, "body_ratio",
                "run %s: candle body ratio %.2f below min %.2f, skipping",
                strategy_run.id, body_ratio, self.min_body_ratio,
            )
            return None

        ema9_values = get_recent_indicator_values(
            db, self.instrument_id, "EMA9", self.timeframe, limit=self.ema_expansion_lookback
        )
        ema20_values = get_recent_indicator_values(
            db, self.instrument_id, "EMA20", self.timeframe, limit=self.ema_expansion_lookback
        )
        if (
            len(ema9_values) < self.ema_expansion_lookback
            or len(ema20_values) < self.ema_expansion_lookback
        ):
            return None  # not warmed up yet

        spreads = [e9 - e20 for e9, e20 in zip(ema9_values, ema20_values, strict=True)]
        bullish_expansion = all(s > 0 for s in spreads) and all(
            spreads[i] < spreads[i + 1] for i in range(len(spreads) - 1)
        )
        bearish_expansion = all(s < 0 for s in spreads) and all(
            spreads[i] > spreads[i + 1] for i in range(len(spreads) - 1)
        )

        direction: Literal["bullish", "bearish"]
        if bullish_expansion:
            direction = "bullish"
        elif bearish_expansion:
            direction = "bearish"
        else:
            self._log_once(
                logger, "expansion",
                "run %s: EMA expansion filter blocked (spreads=%s)",
                strategy_run.id, [round(s, 2) for s in spreads],
            )
            return None

        ema9, ema20 = ema9_values[-1], ema20_values[-1]
        setup_bar, entry_bar = bars[-2], bars[-1]

        if not _bone_zone_pullback(
            setup_bar, entry_bar, ema9, ema20, bullish=(direction == "bullish")
        ):
            self._log_once(
                logger, "bone_zone",
                "run %s: %s bone-zone pullback failed", strategy_run.id, direction,
            )
            return None

        if direction == "bullish":
            option_type, structure_level = OptionType.CE, float(setup_bar.low)
        else:
            option_type, structure_level = OptionType.PE, float(setup_bar.high)

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

        self.trades_fired_count += 1
        logger.info(
            "run %s: EMA %s fired -- entry=%.2f stop=%.2f target=%.2f structure=%.2f",
            strategy_run.id, direction, entry_price, stop_price, target_price, structure_level,
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
            structure_break_buffer=resolve_structure_break_buffer(
                db, self.instrument_id, self.structure_break_atr_multiplier, self.timeframe
            ),
            structure_break_persistence_seconds=self.structure_break_persistence_seconds,
            payload={
                "strategy": "ema_micro_pullback",
                "ema9": ema9,
                "ema20": ema20,
                "strike_score": top.score,
                "breakdown": top.breakdown,
                "env": get_latest_env_metrics(db, self.instrument_id, self.expiry_date),
            },
        )
