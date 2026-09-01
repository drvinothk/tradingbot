"""ATR volatility breakout (Turtle-style, adapted intraday).

The framework's Strategy C: price breaks the highest-high / lowest-low of
the last N completed bars *and* volatility is expanding (today's ATR14 is
above its own recent average). Expanding volatility is the conviction
filter — a breakout into a contracting-range, low-ATR tape is exactly the
false-breakout this strategy is meant to skip.

Continuation, not reversal: a bar that **closes** beyond the rolling
window fires in the break direction (close > window high -> buy CE, close <
window low -> buy PE). Each direction fires at most once per run
(`_fired_directions`, same one-shot latch and same in-memory durability
class as `ORBStrategy`); the opposite direction can still fire later
(a stop-and-reverse is a real pattern).

Risk framing follows the rest of this codebase: `stop_pct`/`target_pct`
are on the **option premium**; `structure_level` is the opposite window
band on the **underlying** (the breakout is invalidated if price falls
back inside the range it broke out of), checked by the shared
structure-break machinery in `evaluate_open_position`. `target_r_multiple`,
when set, overrides `target_price` to a fixed reward:risk multiple of the
premium risk (the framework wants R:R enforced mechanically, 2:1-3:1).

**Backtest-relevant limitations** (see `run_backtest.py`'s own module
docstring for the full list): ATR14 here is computed off the system-wide
60s bar, so `breakout_lookback_bars=20` is a 20-*minute* Donchian channel,
not the framework's literal "N=20 on a 5-min chart" (that would be
`breakout_lookback_bars=100`); the sweep is left as a tunable. The
chandelier trail the framework describes is approximated by the existing
premium-based `trail_activation_fraction`/`trail_lock_fraction`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time

from sqlalchemy.orm import Session

from app.core.clock import IST, to_ist
from app.domain.market.models import Instrument, OptionType, PriceBar
from app.domain.strategy.models import SignalSide, StrategyRun
from app.modules.strategy_engine.common_rules import (
    BAR_TIMEFRAME,
    DEFAULT_STRUCTURE_BREAK_ATR_MULTIPLIER,
    DEFAULT_STRUCTURE_BREAK_PERSISTENCE_SECONDS,
    ConfirmationFilterStrategy,
    _parse_hhmm,
    compute_range_high_low,
    compute_stop_target,
    get_recent_completed_bars,
    get_recent_indicator_values,
    pick_by_underlying,
    resolve_structure_break_buffer,
)
from app.modules.strategy_engine.env_metrics import get_latest_env_metrics, get_vix_as_of
from app.modules.strategy_engine.interface import TradePayload, TradeProposal
from app.modules.strategy_engine.strike_ranking.engine import (
    StrikeRankingConfig,
    pick_top_by_type,
    rank_from_latest_snapshot,
)

logger = logging.getLogger("app.strategy_engine.atr_breakout")

ATR_BREAKOUT_PARAM_KEYS = {
    "qty_lots",
    "breakout_lookback_bars",
    "atr_expansion_lookback",
    "atr_expansion_min_ratio",
    "stop_pct",
    "target_pct",
    "target_r_multiple",
    "trail_activation_fraction",
    "trail_lock_fraction",
    "entry_start_time",
    "entry_cutoff_time",
    "min_breakout_range_nifty_points",
    "min_breakout_range_banknifty_points",
    "vix_min",
    "vix_max",
    "structure_break_atr_multiplier",
    "structure_break_persistence_seconds",
}


class ATRBreakoutStrategy(ConfirmationFilterStrategy):
    def __init__(
        self,
        instrument_id: uuid.UUID,
        expiry_date: date,
        ranking_config: StrikeRankingConfig = StrikeRankingConfig(),
        qty_lots: int = 1,
        breakout_lookback_bars: int = 20,
        atr_expansion_lookback: int = 20,
        atr_expansion_min_ratio: float = 1.1,
        stop_pct: float = 0.12,
        target_pct: float = 0.24,
        target_r_multiple: float | None = None,
        trail_activation_fraction: float = 0.6,
        trail_lock_fraction: float = 0.4,
        timeframe: str = BAR_TIMEFRAME,
        entry_start_time: str = "09:30",
        entry_cutoff_time: str = "14:00",
        min_breakout_range_nifty_points: float = 15.0,
        min_breakout_range_banknifty_points: float = 45.0,
        vix_min: float | None = None,
        vix_max: float | None = None,
        structure_break_atr_multiplier: float = DEFAULT_STRUCTURE_BREAK_ATR_MULTIPLIER,
        structure_break_persistence_seconds: float = DEFAULT_STRUCTURE_BREAK_PERSISTENCE_SECONDS,
    ) -> None:
        super().__init__(instrument_id, timeframe)
        self.expiry_date = expiry_date
        self.ranking_config = ranking_config
        self.qty_lots = qty_lots
        self.breakout_lookback_bars = breakout_lookback_bars
        self.atr_expansion_lookback = atr_expansion_lookback
        self.atr_expansion_min_ratio = atr_expansion_min_ratio
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.target_r_multiple = target_r_multiple
        self.trail_activation_fraction = trail_activation_fraction
        self.trail_lock_fraction = trail_lock_fraction
        self.entry_start_time = _parse_hhmm(entry_start_time)
        self.entry_cutoff_time = _parse_hhmm(entry_cutoff_time)
        self.min_breakout_range_nifty_points = min_breakout_range_nifty_points
        self.min_breakout_range_banknifty_points = min_breakout_range_banknifty_points
        self.vix_min = vix_min
        self.vix_max = vix_max
        self.structure_break_atr_multiplier = structure_break_atr_multiplier
        self.structure_break_persistence_seconds = structure_break_persistence_seconds
        self._fired_directions: set[OptionType] = set()
        self._current_run_id: uuid.UUID | None = None

    def _min_range(self, symbol: str) -> float:
        return pick_by_underlying(
            symbol,
            nifty=self.min_breakout_range_nifty_points,
            banknifty=self.min_breakout_range_banknifty_points,
        )

    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        if self._current_run_id != strategy_run.id:
            self._current_run_id = strategy_run.id
            self._fired_directions = set()

        bar_ist = to_ist(latest_bar.bucket_start)
        if not (self.entry_start_time <= bar_ist.time() <= self.entry_cutoff_time):
            return None

        day = bar_ist.date()
        need = self.breakout_lookback_bars + 1
        # `since=day_start` -- see `oi_volume_confirmed.py`'s identical fix
        # for the live-confirmed 2026-09-01 cross-session contamination.
        day_start = datetime.combine(day, time.min, tzinfo=IST)
        bars = get_recent_completed_bars(
            db, self.instrument_id, self.timeframe, since=day_start, limit=need
        )
        if len(bars) < need:
            return None
        window_high, window_low = compute_range_high_low(bars[:-1])
        range_width = window_high - window_low
        close = float(latest_bar.close)

        if close > window_high:
            option_type, structure_level = OptionType.CE, window_low
        elif close < window_low:
            option_type, structure_level = OptionType.PE, window_high
        else:
            return None

        if option_type in self._fired_directions:
            return None

        instrument = db.get(Instrument, self.instrument_id)
        symbol = instrument.symbol if instrument is not None else ""
        if range_width < self._min_range(symbol):
            self._log_once(
                logger, "range_floor",
                "run %s: breakout window width %.2f below floor %.2f, skipping",
                strategy_run.id, range_width, self._min_range(symbol),
            )
            return None

        atr_reason = self._atr_expansion_reject(db)
        if atr_reason is not None:
            self._log_once(
                logger, atr_reason,
                "run %s: %s breakout rejected — %s",
                strategy_run.id, option_type.value, atr_reason,
            )
            return None

        if self._vix_reject(db, latest_bar) is not None:
            self._log_once(
                logger, "vix_band",
                "run %s: %s breakout rejected — VIX outside band",
                strategy_run.id, option_type.value,
            )
            return None

        ranked = rank_from_latest_snapshot(
            db, self.instrument_id, self.expiry_date, self.ranking_config
        )
        top = pick_top_by_type(ranked, option_type)
        if top is None:
            return None

        self._fired_directions.add(option_type)

        entry_price = top.ltp
        tick_size = float(instrument.tick_size) if instrument is not None else 0.0
        stop_price, target_price = compute_stop_target(
            entry_price, self.stop_pct, self.target_pct, tick_size
        )
        if self.target_r_multiple is not None:
            risk = entry_price - stop_price
            if risk > 0:
                tick = tick_size or 0.05
                target_price = round((entry_price + self.target_r_multiple * risk) / tick) * tick

        logger.info(
            "run %s: %s ATR breakout fired — window[%.2f-%.2f] close=%.2f entry=%.2f "
            "stop=%.2f target=%.2f",
            strategy_run.id, option_type.value, window_low, window_high, close,
            entry_price, stop_price, target_price,
        )

        payload: TradePayload = {
            "strategy": "atr_breakout",
            "window_high": window_high,
            "window_low": window_low,
            "strike_score": top.score,
            "breakdown": top.breakdown,
            "env": get_latest_env_metrics(db, self.instrument_id, self.expiry_date),
        }
        proposal = TradeProposal(
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
            payload=payload,
        )
        return proposal

    def _atr_expansion_reject(self, db: Session) -> str | None:
        need = self.atr_expansion_lookback + 1
        atrs = get_recent_indicator_values(
            db, self.instrument_id, "ATR14", self.timeframe, limit=need
        )
        if len(atrs) < need:
            return "atr_not_ready"
        prior_avg = sum(atrs[:-1]) / len(atrs[:-1])
        if prior_avg <= 0 or atrs[-1] <= prior_avg * self.atr_expansion_min_ratio:
            return "atr_not_expanding"
        return None

    def _vix_reject(self, db: Session, latest_bar: PriceBar) -> str | None:
        if self.vix_min is None and self.vix_max is None:
            return None
        vix = get_vix_as_of(db, latest_bar.bucket_start)
        if vix is None:
            return None
        if self.vix_min is not None and vix < self.vix_min:
            return "vix_below_band"
        if self.vix_max is not None and vix > self.vix_max:
            return "vix_above_band"
        return None
