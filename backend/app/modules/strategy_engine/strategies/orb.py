"""Opening Range Breakout. The opening range is anchored to a fixed 9:15 IST
session open — the real NSE open, not "whenever this strategy run/process
happened to start" — derived from the *bar's own* timestamp
(`latest_bar.bucket_start`), never from `strategy_run.started_at` or
wall-clock `now()`. This is what makes it restart-safe: a runner that starts
or restarts at 10am on a day that already has 9:15-9:30 bars persisted
still computes the *same* range those bars define, rather than treating
10:00-10:15 as "the opening range" the way an anchor tied to
`strategy_run.started_at` would. Requires the WS/data feed to actually
deliver underlying bars from 9:15 onwards (see `market_data_scheduler`'s
pre-market auto-subscribe) — `check_setup` guards against a partial/gapped
window below rather than compute a range from incomplete data.

Once `or_minutes` of completed bars have passed, the first subsequent bar
that *closes* beyond the range (not just wicks through it) fires a
breakout in that direction; each direction only fires once per run (tracked
in-memory, same durability class as everything else a `Strategy` instance
holds — a process restart loses this the same way it loses the runner
thread itself), but the opposite direction can still fire later, since a
stop-out-then-reverse is a real pattern this shouldn't suppress.

**Phase 2 (2026-08-13): two more gates before a breakout is even
considered** -- both are day/time-level filters, not per-strategy tuning:
`orb_entry_cutoff_time` blocks new entries after a configured IST
time-of-day (default 10:15 -- ORB is an opening-move strategy, a breakout
that fires hours later isn't the same setup), and the
`min/max_or_range_{nifty,banknifty}_points` pair blocks the *entire day*
once the 9:15-9:30 range width falls outside a sane band (too narrow ==
chop, too wide == already-trending/gap day neither of which this strategy's
premise handles well) -- picked by underlying (`Instrument.symbol`), not
hardcoded, since one reusable `StrategyConfig` can be started against
either NIFTY or BANKNIFTY (`instrument_id` is a per-`start_strategy`-request
param, never stored on the config itself). Each skip reason logs once per
run, not once per bar -- `check_setup` runs on every new completed bar for
the rest of the session once triggered, so an un-gated log would repeat for
hours on a filtered-out day.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.clock import IST, to_ist
from app.domain.market.models import Instrument, OptionType, PriceBar
from app.domain.strategy.models import SignalSide, StrategyRun
from app.modules.market_data.market_hours import NORMAL_MARKET_OPEN
from app.modules.strategy_engine.common_rules import (
    BAR_TIMEFRAME,
    DEFAULT_STRUCTURE_BREAK_ATR_MULTIPLIER,
    DEFAULT_STRUCTURE_BREAK_PERSISTENCE_SECONDS,
    ConfirmationFilterStrategy,
    _parse_hhmm,
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

# The opening-range window deliberately starts one minute after the real
# NSE cash/index open (`NORMAL_MARKET_OPEN`, single source of truth shared
# with `market_hours.py` -- was previously its own separately-hardcoded
# `time(9, 15)` here, a real duplication), not at open itself: the very
# first minute's candle is disproportionately likely to carry opening-
# auction noise or an initial-WS-reconnect artifact, and including it in a
# 15-candle range skews the range for the rest of the day. Fixed regardless
# of when the StrategyRunner process starts or restarts -- see module
# docstring.
_OPEN_CANDLE_SKIP_MINUTES = 1
ORB_RANGE_START_IST = (
    datetime.combine(date.min, NORMAL_MARKET_OPEN) + timedelta(minutes=_OPEN_CANDLE_SKIP_MINUTES)
).time()

logger = logging.getLogger("app.strategy_engine.orb")


class ORBStrategy(ConfirmationFilterStrategy):
    def __init__(
        self,
        instrument_id: uuid.UUID,
        expiry_date: date,
        ranking_config: StrikeRankingConfig = StrikeRankingConfig(),
        qty_lots: int = 1,
        or_minutes: int = 15,
        or_search_minutes: int = 30,
        stop_pct: float = 0.12,
        target_pct: float = 0.20,
        trail_activation_fraction: float = 0.6,
        trail_lock_fraction: float = 0.4,
        timeframe: str = BAR_TIMEFRAME,
        orb_entry_cutoff_time: str = "10:15",
        min_or_range_nifty_points: float = 20.0,
        max_or_range_nifty_points: float = 80.0,
        min_or_range_banknifty_points: float = 75.0,
        max_or_range_banknifty_points: float = 250.0,
        structure_break_atr_multiplier: float = DEFAULT_STRUCTURE_BREAK_ATR_MULTIPLIER,
        structure_break_persistence_seconds: float = DEFAULT_STRUCTURE_BREAK_PERSISTENCE_SECONDS,
    ) -> None:
        super().__init__(instrument_id, timeframe)
        self.expiry_date = expiry_date
        self.ranking_config = ranking_config
        self.qty_lots = qty_lots
        self.or_minutes = or_minutes
        self.or_search_minutes = or_search_minutes
        self.stop_pct = stop_pct
        self.target_pct = target_pct
        self.trail_activation_fraction = trail_activation_fraction
        self.trail_lock_fraction = trail_lock_fraction
        self.orb_entry_cutoff_time = _parse_hhmm(orb_entry_cutoff_time)
        self.min_or_range_nifty_points = min_or_range_nifty_points
        self.max_or_range_nifty_points = max_or_range_nifty_points
        self.min_or_range_banknifty_points = min_or_range_banknifty_points
        self.max_or_range_banknifty_points = max_or_range_banknifty_points
        self.structure_break_atr_multiplier = structure_break_atr_multiplier
        self.structure_break_persistence_seconds = structure_break_persistence_seconds
        self._fired_directions: set[OptionType] = set()

    def _range_thresholds(self, symbol: str) -> tuple[float, float]:
        return pick_by_underlying(
            symbol,
            nifty=(self.min_or_range_nifty_points, self.max_or_range_nifty_points),
            banknifty=(self.min_or_range_banknifty_points, self.max_or_range_banknifty_points),
        )

    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        bar_ist = to_ist(latest_bar.bucket_start)
        or_start = datetime.combine(bar_ist.date(), ORB_RANGE_START_IST, tzinfo=IST)
        or_min_end = or_start + timedelta(minutes=self.or_minutes)
        if latest_bar.bucket_start < or_min_end:
            return None  # still inside the minimum opening-range window

        # Search window widened to `or_search_minutes` (default 30, i.e.
        # 9:16-9:46) but the *count* required stays `or_minutes` (15) --
        # this tolerates a short gap in same-day bars (a dropped minute
        # anywhere in the window, not just at the very start -- a real
        # `price_bars` row missing from any single minute already meant
        # "not enough bars, sit out for the day" under the old 9:15-9:30-
        # only window) without changing normal-day behavior at all: on any
        # day where 15 bars already exist by `or_min_end` (9:31), this
        # fires at exactly the same bar it always has, byte-identical
        # range. Only a day with a real gap reaches past `or_min_end`
        # still short, and keeps checking bar-by-bar until either 15
        # valid same-day bars accumulate (wherever they land
        # chronologically -- a missing bar is skipped, never gap-filled)
        # or `or_search_minutes` passes with still too few, in which case
        # this sits out for the day exactly as before, just with a longer
        # grace period. Live-confirmed 2026-09-01: a Shoonya reconnect at
        # 10:03 left zero bars for 9:15-9:30, so ORB_Conviction fired zero
        # signals all day -- this fix tolerates a short (few-minute) gap;
        # an outage that long is still a sit-out day by design, not
        # something this window relaxation is meant to cover.
        or_max_end = or_start + timedelta(minutes=self.or_search_minutes)
        or_bars_candidates = get_recent_completed_bars(
            db, self.instrument_id, self.timeframe, since=or_start, until=or_max_end
        )

        # Bad-bar exclusion: a single 1-minute bar whose own (high - low)
        # already exceeds the *aggregate* 15-bar range ceiling
        # (`max_or_range_{nifty,banknifty}_points`) is treated as a data
        # artifact (a wide/garbage bar from an initial WS-reconnect
        # handshake), not real price action -- real NIFTY/BANKNIFTY index
        # moves don't cover an entire opening-range's worth of width in one
        # minute. Reuses the existing, already-calibrated ceiling rather
        # than a new uncalibrated threshold -- no behavior change on a
        # normal day (no bar ever approaches this width), and strictly
        # better than the pre-existing fallback on a corrupted-bar day
        # (previously that one bar would poison the aggregate range calc
        # and likely fail the width filter below, killing the whole day;
        # now it's simply excluded and the search continues for the next
        # valid bar, same "skip it, use the next one" tolerance as a
        # missing bar). Instrument resolved here (moved up from below) so
        # both this filter and the width filter share one lookup.
        instrument = db.get(Instrument, self.instrument_id)
        symbol = instrument.symbol if instrument is not None else ""
        min_range, max_range = self._range_thresholds(symbol)
        or_bars_all = [
            bar for bar in or_bars_candidates if (float(bar.high) - float(bar.low)) <= max_range
        ]
        if len(or_bars_all) < self.or_minutes:
            return None  # not enough valid same-day bars yet, or a real gap -- either way, wait

        or_bars = or_bars_all[: self.or_minutes]  # first 15 valid bars chronologically
        or_high, or_low = compute_range_high_low(or_bars)
        range_width = or_high - or_low

        self._log_once(
            logger, "or_range",
            "run %s: opening range OR[%.2f-%.2f] width=%.2f",
            strategy_run.id, or_low, or_high, range_width,
        )

        if range_width < min_range or range_width > max_range:
            self._log_once(
                logger, "range_filter",
                "run %s: opening range width %.2f outside [%.2f, %.2f], skipping",
                strategy_run.id, range_width, min_range, max_range,
            )
            return None

        if bar_ist.time() > self.orb_entry_cutoff_time:
            self._log_once(
                logger, "cutoff",
                "run %s: skipped breakout after cutoff time %s",
                strategy_run.id, self.orb_entry_cutoff_time.strftime("%H:%M"),
            )
            return None

        close = float(latest_bar.close)

        if close > or_high:
            option_type, structure_level = OptionType.CE, or_low
        elif close < or_low:
            option_type, structure_level = OptionType.PE, or_high
        else:
            return None

        if option_type in self._fired_directions:
            logger.info(
                "run %s: %s breakout suppressed (already fired this run) -- "
                "OR[%.2f-%.2f], bar %s close=%.2f",
                strategy_run.id, option_type.value, or_low, or_high,
                latest_bar.bucket_start.isoformat(), close,
            )
            return None

        ranked = rank_from_latest_snapshot(
            db, self.instrument_id, self.expiry_date, self.ranking_config
        )
        top = pick_top_by_type(ranked, option_type)
        if top is None:
            return None

        self._fired_directions.add(option_type)
        logger.info(
            "run %s: %s breakout fired -- OR[%.2f-%.2f], bar %s close=%.2f",
            strategy_run.id, option_type.value, or_low, or_high,
            latest_bar.bucket_start.isoformat(), close,
        )

        entry_price = top.ltp
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
                "strategy": "orb",
                "or_high": or_high,
                "or_low": or_low,
                "strike_score": top.score,
                "breakdown": top.breakdown,
                "env": get_latest_env_metrics(db, self.instrument_id, self.expiry_date),
            },
        )
