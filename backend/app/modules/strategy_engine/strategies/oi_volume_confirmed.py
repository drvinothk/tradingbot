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

**2026-08-13: four more gates, plus a pre-emptive false-breakout gate that
sits ahead of all of them:**

1. **False breakout (3-bar re-entry)** — a raw breakout candidate (`close`
   beyond the rolling window) is tracked (`_pending_breakout`: direction ->
   frozen `(window_high, window_low, bar_count_at_detection)`) the moment
   it's detected, *before* any of the gates below get a chance to block or
   allow it. If `close` re-enters that *frozen* range within the next 3
   bars, the direction is blocked for the rest of the run
   (`_false_breakout_blocked`) — a real, pre-emptive gate, not just a log:
   a candidate that gets blocked this bar by, say, the time-window filter
   would otherwise still be sitting there eligible to fire later once the
   window opens, even though price already proved the move was fake in the
   meantime. A candidate that *does* fire for real is dropped from
   `_pending_breakout` (already permanently covered by `_fired_directions`
   from then on, so there's nothing left to track).
2. **Instrument-aware range-width filter** — `window_high - window_low`
   must fall inside a sane band, picked by the instrument's own symbol
   (NIFTY vs BANKNIFTY), same "one reusable StrategyConfig, either
   underlying" reasoning `ORBStrategy._range_thresholds` already
   established.
3. **Candle body ratio** — `common_rules.compute_body_ratio` over the last
   10 bars, same chop filter EMA Micro-pullback uses.
4. **Time windows & session trade cap** — identical dual-window shape to
   EMA Micro-pullback (`oi_morning_window`/`oi_afternoon_window`,
   `oi_max_trades_per_session`), same reasoning: breakouts fail during
   midday low-volume chop regardless of which strategy is looking for one.
5. **Participation stubs** (`oi_use_futures_volume_confirmation`,
   `oi_futures_volume_multiplier`, `oi_use_atm_oi_buildup`) — not enforced
   yet (no real futures-volume/ATM-OI-buildup pipeline exists), but real
   constructor kwargs (unlike ORB's inert expiry-day hooks) since a real
   breakout's log line references their current values, ready for the
   pipeline that will eventually read them.

All new per-run state (`trades_fired_count`, `bar_count`,
`_pending_breakout`, `_false_breakout_blocked`, and `_fired_directions`)
resets together if `strategy_run.id` ever changes on this instance —
defensive, same as EMA Micro-pullback's identical reset (a fresh instance
is always constructed per `StrategyRun` today, so this can't actually
trigger, but costs nothing to have in place).
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
FALSE_BREAKOUT_GRACE_BARS = 3

logger = logging.getLogger("app.strategy_engine.oi_volume_confirmed")

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
        oi_use_futures_volume_confirmation: bool = False,
        oi_futures_volume_multiplier: float = 1.5,
        oi_use_atm_oi_buildup: bool = False,
        min_range_nifty_points: float = 15.0,
        max_range_nifty_points: float = 60.0,
        min_range_banknifty_points: float = 50.0,
        max_range_banknifty_points: float = 180.0,
        min_body_ratio: float = 0.40,
        oi_morning_window_start: str = "09:31",
        oi_morning_window_end: str = "11:00",
        oi_afternoon_window_start: str = "13:00",
        oi_afternoon_window_end: str = "15:00",
        oi_max_trades_per_session: int = 3,
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
        self.oi_use_futures_volume_confirmation = oi_use_futures_volume_confirmation
        self.oi_futures_volume_multiplier = oi_futures_volume_multiplier
        self.oi_use_atm_oi_buildup = oi_use_atm_oi_buildup
        self.min_range_nifty_points = min_range_nifty_points
        self.max_range_nifty_points = max_range_nifty_points
        self.min_range_banknifty_points = min_range_banknifty_points
        self.max_range_banknifty_points = max_range_banknifty_points
        self.min_body_ratio = min_body_ratio
        self.oi_morning_window_start = _parse_hhmm(oi_morning_window_start)
        self.oi_morning_window_end = _parse_hhmm(oi_morning_window_end)
        self.oi_afternoon_window_start = _parse_hhmm(oi_afternoon_window_start)
        self.oi_afternoon_window_end = _parse_hhmm(oi_afternoon_window_end)
        self.oi_max_trades_per_session = oi_max_trades_per_session
        self.structure_break_atr_multiplier = structure_break_atr_multiplier
        self.structure_break_persistence_seconds = structure_break_persistence_seconds
        self.trades_fired_count = 0
        self.bar_count = 0
        self._fired_directions: set[OptionType] = set()
        self._pending_breakout: dict[OptionType, tuple[float, float, int]] = {}
        self._false_breakout_blocked: set[OptionType] = set()
        self._current_run_id: uuid.UUID | None = None

    def _within_trade_windows(self, t: time) -> bool:
        return (
            self.oi_morning_window_start <= t <= self.oi_morning_window_end
            or self.oi_afternoon_window_start <= t <= self.oi_afternoon_window_end
        )

    def _range_thresholds(self, symbol: str) -> tuple[float, float]:
        return pick_by_underlying(
            symbol,
            nifty=(self.min_range_nifty_points, self.max_range_nifty_points),
            banknifty=(self.min_range_banknifty_points, self.max_range_banknifty_points),
        )

    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        if self._current_run_id != strategy_run.id:
            self._current_run_id = strategy_run.id
            self.trades_fired_count = 0
            self.bar_count = 0
            self._fired_directions = set()
            self._pending_breakout = {}
            self._false_breakout_blocked = set()

        self.bar_count += 1
        close = float(latest_bar.close)

        # False-breakout re-entry check against any already-pending
        # candidate -- runs first, needs no DB query, and applies
        # regardless of whether a *new* candidate is detected this bar.
        for direction in list(self._pending_breakout):
            snap_high, snap_low, detected_at = self._pending_breakout[direction]
            if self.bar_count - detected_at > FALSE_BREAKOUT_GRACE_BARS:
                del self._pending_breakout[direction]
                continue
            if snap_low <= close <= snap_high:
                self._false_breakout_blocked.add(direction)
                del self._pending_breakout[direction]
                self._log_once(
                    logger, f"false_breakout_{direction.value}",
                    "run %s: %s breakout (bar %d, window[%.2f-%.2f]) re-entered the "
                    "static range within %d bars -- blocking direction",
                    strategy_run.id, direction.value, detected_at, snap_low, snap_high,
                    FALSE_BREAKOUT_GRACE_BARS,
                )

        needed = max(self.lookback_bars + 1, BODY_RATIO_LOOKBACK_BARS)
        bars = get_recent_completed_bars(db, self.instrument_id, self.timeframe, limit=needed)
        if len(bars) < needed:
            return None
        window_bars = bars[-(self.lookback_bars + 1):-1]  # bars[-1] is latest_bar itself
        window_high, window_low = compute_range_high_low(window_bars)

        if close > window_high:
            candidate, structure_level = OptionType.CE, window_low
        elif close < window_low:
            candidate, structure_level = OptionType.PE, window_high
        else:
            return None

        if candidate in self._fired_directions or candidate in self._false_breakout_blocked:
            return None

        if candidate not in self._pending_breakout:
            self._pending_breakout[candidate] = (window_high, window_low, self.bar_count)

        bar_time = to_ist(latest_bar.bucket_start).time()
        if not self._within_trade_windows(bar_time):
            self._log_once(
                logger, "time_window",
                "run %s: bar time %s outside OI/Volume morning/afternoon windows",
                strategy_run.id, bar_time.strftime("%H:%M"),
            )
            return None

        if self.trades_fired_count >= self.oi_max_trades_per_session:
            self._log_once(
                logger, "max_trades",
                "run %s: max trades per session reached (%d)",
                strategy_run.id, self.oi_max_trades_per_session,
            )
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

        body_ratio = compute_body_ratio(bars[-BODY_RATIO_LOOKBACK_BARS:])
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
        top = pick_top_by_type(ranked, candidate)
        if top is None:
            return None

        self._fired_directions.add(candidate)
        self._pending_breakout.pop(candidate, None)
        self.trades_fired_count += 1

        entry_price = top.ltp
        tick_size = float(instrument.tick_size) if instrument is not None else 0.0
        stop_price, target_price = compute_stop_target(
            entry_price, self.stop_pct, self.target_pct, tick_size
        )

        logger.info(
            "run %s: %s breakout fired -- window[%.2f-%.2f] entry=%.2f stop=%.2f target=%.2f "
            "(participation stubs: futures_volume_confirmation=%s multiplier=%.2f "
            "atm_oi_buildup=%s, not yet enforced)",
            strategy_run.id, candidate.value, window_low, window_high,
            entry_price, stop_price, target_price,
            self.oi_use_futures_volume_confirmation, self.oi_futures_volume_multiplier,
            self.oi_use_atm_oi_buildup,
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
                "strategy": "oi_volume_confirmed",
                "window_high": window_high,
                "window_low": window_low,
                "strike_score": top.score,
                "breakdown": top.breakdown,
                "env": get_latest_env_metrics(db, self.instrument_id, self.expiry_date),
            },
        )
