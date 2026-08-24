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

**2026-08-13: a confirmation-candle gate, plus four more filters:**

1. **Confirmation candle (Option A)** — a sweep candle (the existing
   wick-beyond/close-back-inside detection above, unchanged) no longer
   fires immediately. It's recorded as `_pending_sweep` (direction, the
   sweep candle's own *opposite* extreme as the confirmation level,
   `structure_level`, `bar_count`), and only the *immediate* next bar is
   checked against it: for a bullish reversal (swept low, buying CE), that
   next bar must close strictly *above* the sweep candle's high; for a
   bearish reversal (swept high, buying PE), strictly *below* the sweep
   candle's low. If the next bar doesn't confirm, the pending sweep is
   dropped — no multi-bar grace window (unlike OI/Volume Confirmed's 3-bar
   false-breakout check) — a genuinely new sweep would need to occur again.
   A bar spent resolving a pending confirmation doesn't also get evaluated
   as a fresh sweep candidate in the same call.
2. **Sweep distance filter** — how far the wick pokes beyond the level
   (`high - window_high` / `window_low - low`) must clear
   `min_sweep_distance_{nifty,banknifty}_points` — a noise floor, checked
   at sweep-detection time (a property of the sweep event itself). No
   ceiling, unlike the range-width filter below — a bigger sweep is never
   a bad sign here.
3. **Instrument-aware range-width filter** — `window_high - window_low`
   must fall inside `sweep_min/max_range_width_{nifty,banknifty}_points`,
   using `common_rules.pick_by_underlying` — the same symbol-lookup shape
   ORB/OI-Volume Confirmed use, with this strategy's own independent
   config keys and defaults (a 10-bar rolling window here vs OI/Volume's
   5-bar one, so naturally wider on average).
4. **Candle body ratio & time windows** — `common_rules.compute_body_ratio`
   over the last 10 bars and the identical dual-window shape EMA/OI-Volume
   use (`sweep_morning_window`/`sweep_afternoon_window`,
   `sweep_max_trades_per_session`) — checked at the *confirmation* bar
   (where the trade would actually enter), not the sweep bar.

`target_pct` default moves 0.16 -> 0.20 (a stated 1:2 R:R against the
unchanged 0.10 stop). Expiry-day config hooks are deliberately NOT added to
`LIQUIDITY_SWEEP_REVERSAL_PARAM_KEYS` in `api.v1.strategies` — same "inert
JSON, no matching constructor kwarg" reasoning as ORB's own expiry hooks.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, time

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
    compute_range_high_low,
    compute_stop_target,
    get_recent_completed_bars,
    pick_by_underlying,
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

logger = logging.getLogger("app.strategy_engine.liquidity_sweep_reversal")


class LiquiditySweepReversalStrategy(ConfirmationFilterStrategy):
    def __init__(
        self,
        instrument_id: uuid.UUID,
        expiry_date: date,
        ranking_config: StrikeRankingConfig = StrikeRankingConfig(),
        lookback_bars: int = 10,
        stop_pct: float = 0.10,
        target_pct: float = 0.20,
        trail_activation_fraction: float = 0.5,
        trail_lock_fraction: float = 0.5,
        timeframe: str = BAR_TIMEFRAME,
        min_sweep_distance_nifty_points: float = 5.0,
        min_sweep_distance_banknifty_points: float = 15.0,
        sweep_min_range_width_nifty_points: float = 30.0,
        sweep_max_range_width_nifty_points: float = 120.0,
        sweep_min_range_width_banknifty_points: float = 100.0,
        sweep_max_range_width_banknifty_points: float = 360.0,
        min_body_ratio: float = 0.40,
        sweep_morning_window_start: str = "09:31",
        sweep_morning_window_end: str = "11:00",
        sweep_afternoon_window_start: str = "13:00",
        sweep_afternoon_window_end: str = "15:00",
        sweep_max_trades_per_session: int = 3,
        structure_break_atr_multiplier: float = DEFAULT_STRUCTURE_BREAK_ATR_MULTIPLIER,
        structure_break_persistence_seconds: float = DEFAULT_STRUCTURE_BREAK_PERSISTENCE_SECONDS,
    ) -> None:
        super().__init__(instrument_id, timeframe)
        self.expiry_date = expiry_date
        self.ranking_config = ranking_config
        self.lookback_bars = lookback_bars
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.trail_activation_fraction = trail_activation_fraction
        self.trail_lock_fraction = trail_lock_fraction
        self.min_sweep_distance_nifty_points = min_sweep_distance_nifty_points
        self.min_sweep_distance_banknifty_points = min_sweep_distance_banknifty_points
        self.sweep_min_range_width_nifty_points = sweep_min_range_width_nifty_points
        self.sweep_max_range_width_nifty_points = sweep_max_range_width_nifty_points
        self.sweep_min_range_width_banknifty_points = sweep_min_range_width_banknifty_points
        self.sweep_max_range_width_banknifty_points = sweep_max_range_width_banknifty_points
        self.min_body_ratio = min_body_ratio
        self.sweep_morning_window_start = _parse_hhmm(sweep_morning_window_start)
        self.sweep_morning_window_end = _parse_hhmm(sweep_morning_window_end)
        self.sweep_afternoon_window_start = _parse_hhmm(sweep_afternoon_window_start)
        self.sweep_afternoon_window_end = _parse_hhmm(sweep_afternoon_window_end)
        self.sweep_max_trades_per_session = sweep_max_trades_per_session
        self.structure_break_atr_multiplier = structure_break_atr_multiplier
        self.structure_break_persistence_seconds = structure_break_persistence_seconds
        self.trades_fired_count = 0
        self.bar_count = 0
        self._pending_sweep: tuple[OptionType, float, float, float, float, int] | None = None
        self._current_run_id: uuid.UUID | None = None

    def _within_trade_windows(self, t: time) -> bool:
        return (
            self.sweep_morning_window_start <= t <= self.sweep_morning_window_end
            or self.sweep_afternoon_window_start <= t <= self.sweep_afternoon_window_end
        )

    def _range_thresholds(self, symbol: str) -> tuple[float, float]:
        return pick_by_underlying(
            symbol,
            nifty=(
                self.sweep_min_range_width_nifty_points,
                self.sweep_max_range_width_nifty_points,
            ),
            banknifty=(
                self.sweep_min_range_width_banknifty_points,
                self.sweep_max_range_width_banknifty_points,
            ),
        )

    def _distance_threshold(self, symbol: str) -> float:
        return pick_by_underlying(
            symbol,
            nifty=self.min_sweep_distance_nifty_points,
            banknifty=self.min_sweep_distance_banknifty_points,
        )

    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        if self._current_run_id != strategy_run.id:
            self._current_run_id = strategy_run.id
            self.trades_fired_count = 0
            self.bar_count = 0
            self._pending_sweep = None

        self.bar_count += 1
        close = float(latest_bar.close)

        if self._pending_sweep is not None:
            direction, confirm_level, structure_level, window_high, window_low, detected_at = (
                self._pending_sweep
            )
            self._pending_sweep = None
            if detected_at == self.bar_count - 1:
                confirmed = (
                    close > confirm_level if direction is OptionType.CE else close < confirm_level
                )
                if confirmed:
                    return self._fire(
                        db, strategy_run, latest_bar, direction,
                        structure_level, window_high, window_low,
                    )
                self._log_once(
                    logger, "confirmation_failed",
                    "run %s: %s sweep confirmation failed at bar %d",
                    strategy_run.id, direction.value, self.bar_count,
                )
            return None

        bars = get_recent_completed_bars(
            db, self.instrument_id, self.timeframe, limit=self.lookback_bars + 1
        )
        if len(bars) < self.lookback_bars + 1:
            return None
        window_bars = bars[:-1]  # bars[-1] is latest_bar itself

        window_high, window_low = compute_range_high_low(window_bars)
        high = float(latest_bar.high)
        low = float(latest_bar.low)

        swept_high = high > window_high and close <= window_high
        swept_low = low < window_low and close >= window_low

        if swept_high:
            direction, structure_level, confirm_level, distance = (
                OptionType.PE, window_high, low, high - window_high,
            )
        elif swept_low:
            direction, structure_level, confirm_level, distance = (
                OptionType.CE, window_low, high, window_low - low,
            )
        else:
            return None

        instrument = db.get(Instrument, self.instrument_id)
        symbol = instrument.symbol if instrument is not None else ""

        min_range, max_range = self._range_thresholds(symbol)
        range_width = window_high - window_low
        if range_width < min_range or range_width > max_range:
            self._log_once(
                logger, "range_filter",
                "run %s: window width %.2f outside [%.2f, %.2f], skipping",
                strategy_run.id, range_width, min_range, max_range,
            )
            return None

        min_distance = self._distance_threshold(symbol)
        if distance < min_distance:
            self._log_once(
                logger, "sweep_distance",
                "run %s: sweep distance %.2f below min %.2f, skipping",
                strategy_run.id, distance, min_distance,
            )
            return None

        self._pending_sweep = (
            direction, confirm_level, structure_level, window_high, window_low, self.bar_count,
        )
        return None

    def _fire(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar, direction: OptionType,
        structure_level: float, window_high: float, window_low: float,
    ) -> TradeProposal | None:
        bar_time = to_ist(latest_bar.bucket_start).time()
        if not self._within_trade_windows(bar_time):
            self._log_once(
                logger, "time_window",
                "run %s: bar time %s outside sweep morning/afternoon windows",
                strategy_run.id, bar_time.strftime("%H:%M"),
            )
            return None

        if self.trades_fired_count >= self.sweep_max_trades_per_session:
            self._log_once(
                logger, "max_trades",
                "run %s: max trades per session reached (%d)",
                strategy_run.id, self.sweep_max_trades_per_session,
            )
            return None

        bars = get_recent_completed_bars(
            db, self.instrument_id, self.timeframe, limit=BODY_RATIO_LOOKBACK_BARS
        )
        if len(bars) < BODY_RATIO_LOOKBACK_BARS:
            return None
        body_ratio = compute_body_ratio(bars)
        if body_ratio < self.min_body_ratio:
            self._log_once(
                logger, "body_ratio",
                "run %s: candle body ratio %.2f below min %.2f, skipping",
                strategy_run.id, body_ratio, self.min_body_ratio,
            )
            return None

        ranked = rank_from_latest_snapshot(
            db, self.instrument_id, self.expiry_date, self.ranking_config
        )
        top = pick_top_by_type(ranked, direction)
        if top is None:
            return None

        self.trades_fired_count += 1

        entry_price = top.ltp
        instrument = db.get(Instrument, self.instrument_id)
        tick_size = float(instrument.tick_size) if instrument is not None else 0.0
        stop_price, target_price = compute_stop_target(
            entry_price, self.stop_pct, self.target_pct, tick_size
        )

        logger.info(
            "run %s: %s sweep-reversal fired -- structure=%.2f entry=%.2f stop=%.2f target=%.2f",
            strategy_run.id, direction.value, structure_level,
            entry_price, stop_price, target_price,
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
                "strategy": "liquidity_sweep_reversal",
                "window_high": window_high,
                "window_low": window_low,
                "strike_score": top.score,
                "breakdown": top.breakdown,
                "env": get_latest_env_metrics(db, self.instrument_id, self.expiry_date),
            },
        )
