"""Shared rules the three Phase 4 confirmation-filter strategies (ORB, VWAP
Pullback, EMA Micro-pullback) all need, implemented once here rather than
duplicated per strategy:

- **full-candle completion**: a strategy must only ever evaluate a signal
  once per newly-*completed* bar, never mid-bar and never twice for the same
  bar. `ConfirmationFilterStrategy.evaluate` enforces this generically by
  tracking the last bar `bucket_start` it acted on.
- **no signal while already in a position**: `get_open_position_for_run`
  backs both this guard and the generalized runner's (Phase 4 Step 5)
  `StrategyRunStatus.IN_POSITION` refresh — one query, two uses.

Deliberately does *not* try to hand every strategy a single shared lookback
window of bars: ORB needs a fixed window anchored to the real 9:15 IST
session open (see `strategies/orb.py`'s own docstring — derived from the
bar's own timestamp, not `strategy_run.started_at`, specifically so a late
or restarted run still computes the same range), while VWAP Pullback/EMA
Micro-pullback only need a handful of recent bars. `get_recent_completed_bars`
supports both via `since`/`until` (a fixed window) or `limit` (a trailing
window) — each strategy's `check_setup` calls it with whatever shape it
actually needs. `get_recent_indicator_values` is the identical shape for
`IndicatorSnapshot` rows instead of `PriceBar` rows — EMA Micro-pullback's
expansion filter needs the last few EMA9/EMA20 values, not just the single
latest scalar `get_latest_indicator_value` returns.

`_log_once` (on `ConfirmationFilterStrategy`) and `_parse_hhmm` are also
shared here rather than per-strategy: ORB was first to need a "log this
skip reason once per run, not once per bar" pattern and a configured
"HH:MM" cutoff string, but EMA Micro-pullback's own time-window/max-trades/
body-ratio/expansion/bone-zone gates need the identical two things — a
second copy-pasted set of boolean flags per strategy was the wrong shape
once a second (now third) consumer showed up. `common_rules.py` is this
codebase's existing "shared among confirmation-filter strategies, not
promoted to `app.core.clock`/global utilities" home, same reasoning
`touch_and_confirm`/`compute_stop_target` already live here for.
"""

from __future__ import annotations

import logging
import uuid
from abc import abstractmethod
from datetime import datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, TypeVar

from sqlalchemy.orm import Session

from app.domain.execution.models import Position, PositionStatus
from app.domain.market.models import IndicatorSnapshot, PriceBar
from app.domain.strategy.models import StrategyRun, TradeIntent
from app.modules.strategy_engine.interface import Strategy, TradeProposal

# Matches the timeframe string market_data.ingestion writes
# (f"{IndicatorEngine.timeframe_seconds}s") for the system-wide 60s bar —
# the only timeframe anything in this codebase persists.
BAR_TIMEFRAME = "60s"

_T = TypeVar("_T")


def pick_by_underlying(symbol: str, *, nifty: _T, banknifty: _T) -> _T:
    """Picks `nifty` or `banknifty` by the instrument's own symbol —
    `"BANKNIFTY" in symbol.upper()` (a substring match, not exact equality,
    matching every real production/test `instrument.symbol` seen so far),
    else `nifty`. Generic over whatever shape the caller needs — a
    `(min, max)` threshold pair for a range-width filter, a single float
    floor for a distance filter, ... — ORB, OI/Volume Confirmed, and
    Liquidity Sweep/Reversal each keep their own independently configured
    threshold *values* (different config key names, different defaults);
    only this symbol-lookup shape is shared.
    """
    if "BANKNIFTY" in symbol.upper():
        return banknifty
    return nifty


def get_open_position_for_run(db: Session, strategy_run: StrategyRun) -> Position | None:
    return (
        db.query(Position)
        .join(TradeIntent, Position.trade_intent_id == TradeIntent.id)
        .filter(
            TradeIntent.strategy_run_id == strategy_run.id,
            Position.status == PositionStatus.OPEN,
        )
        .one_or_none()
    )


def get_recent_completed_bars(
    db: Session,
    instrument_id: uuid.UUID,
    timeframe: str = BAR_TIMEFRAME,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
) -> list[PriceBar]:
    """Ascending by `bucket_start` (oldest first) regardless of which
    filters are supplied — every `price_bars` row is already a *completed*
    bar (see market_data.ingestion), so there's no separate "is this bar
    done yet" check needed here.
    """
    query = db.query(PriceBar).filter(
        PriceBar.instrument_id == instrument_id, PriceBar.timeframe == timeframe
    )
    if since is not None:
        query = query.filter(PriceBar.bucket_start >= since)
    if until is not None:
        query = query.filter(PriceBar.bucket_start < until)
    query = query.order_by(PriceBar.bucket_start.desc())
    if limit is not None:
        query = query.limit(limit)
    return list(reversed(query.all()))


def get_latest_indicator_value(
    db: Session,
    instrument_id: uuid.UUID,
    indicator_name: str,
    timeframe: str = BAR_TIMEFRAME,
) -> float | None:
    """VWAP Pullback (VWAP) and EMA Micro-pullback (EMA9/EMA20) both need
    the latest persisted scalar for the underlying — `None` means "not
    warmed up yet", same convention `IndicatorEngine`/`EMACalculator`
    already use, not an error.
    """
    row = (
        db.query(IndicatorSnapshot)
        .filter(
            IndicatorSnapshot.instrument_id == instrument_id,
            IndicatorSnapshot.indicator_name == indicator_name,
            IndicatorSnapshot.timeframe == timeframe,
        )
        .order_by(IndicatorSnapshot.ts.desc())
        .first()
    )
    return float(row.value) if row is not None else None


def get_recent_indicator_values(
    db: Session,
    instrument_id: uuid.UUID,
    indicator_name: str,
    timeframe: str = BAR_TIMEFRAME,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
) -> list[float]:
    """`get_recent_completed_bars`'s identical shape for `IndicatorSnapshot`
    rows — ascending by `ts` (oldest first) regardless of which filters are
    supplied. EMA9 and EMA20 are always persisted together, from the same
    bar-completion event (`IndicatorEngine.on_tick`), so calling this once
    per indicator name with the same `limit` and zipping the two results
    positionally gives correctly-paired same-bar values — the same
    assumption `EMAMicroPullbackStrategy` already made comparing single
    latest EMA9/EMA20 scalars before this function existed.
    """
    query = db.query(IndicatorSnapshot.value).filter(
        IndicatorSnapshot.instrument_id == instrument_id,
        IndicatorSnapshot.indicator_name == indicator_name,
        IndicatorSnapshot.timeframe == timeframe,
    )
    if since is not None:
        query = query.filter(IndicatorSnapshot.ts >= since)
    if until is not None:
        query = query.filter(IndicatorSnapshot.ts < until)
    query = query.order_by(IndicatorSnapshot.ts.desc())
    if limit is not None:
        query = query.limit(limit)
    return [float(v) for (v,) in reversed(query.all())]


def _parse_hhmm(value: str) -> time:
    """"HH:MM" -> `time`. Raises `ValueError` on malformed input, same as
    every other strategy param — nothing in `_build_strategy` validates
    config values before passing them to a strategy's constructor, so
    failing loudly here (at strategy construction, not silently at some
    later bar) is consistent with that existing behavior.
    """
    hour_str, minute_str = value.split(":")
    return time(int(hour_str), int(minute_str))


def compute_range_high_low(bars: list[PriceBar]) -> tuple[float, float]:
    """(high, low) across a window of completed bars — the range-boundary
    computation ORB does over its fixed, session-anchored opening-range
    window, and the OI/Volume Confirmed and Liquidity Sweep/Reversal
    strategies (Phase 7) both do over a rolling last-N-bars window instead.
    Assumes `bars` is non-empty; callers already guard on window length
    before calling this.
    """
    return max(float(b.high) for b in bars), min(float(b.low) for b in bars)


def compute_body_ratio(bars: list[PriceBar]) -> float:
    """Mean(`|close-open|`) / mean(`high-low`) across `bars` — a chop/
    indecision filter (small bodies relative to range = wicky, indecisive
    candles) EMA Micro-pullback and OI/Volume Confirmed both use, over
    their own respective bar windows. Deliberately a ratio of means, not a
    mean of per-bar ratios — those aren't the same computation. Returns
    `0.0` (never raises) when the mean range is `0.0` (a run of perfectly
    flat bars), so that value then naturally fails any real
    `min_body_ratio` threshold at the call site rather than needing a
    zero-guard duplicated at every caller. Assumes `bars` is non-empty,
    same convention `compute_range_high_low` already uses.
    """
    bodies = [abs(float(b.close) - float(b.open)) for b in bars]
    ranges = [float(b.high) - float(b.low) for b in bars]
    avg_body = sum(bodies) / len(bodies)
    avg_range = sum(ranges) / len(ranges)
    return 0.0 if avg_range == 0.0 else avg_body / avg_range


def _round_to_tick(price: float, tick_size: float) -> float:
    """2026-08-12: real bug fixed here — plain `round(price, 2)` produces
    prices like `132.84` that aren't a multiple of a real instrument's tick
    size (e.g. `0.05`), which `risk_engine.evaluate_trade_intent`'s
    `_is_tick_aligned` check then correctly rejects as
    `tick_size_violation`. Never surfaced against the mock adapter's clean
    whole-number synthetic premiums (any 0.9/1.15-style multiplier of a
    whole number stays 0.05-aligned); live-found the first time real,
    genuinely fractional Shoonya-sourced premiums flowed into a real
    strategy's signal generation, rejecting the *entire* signal on this
    alone in the vast majority of cases. Decimal-based, not float division,
    so the result is an exact tick multiple rather than one that merely
    looks aligned when printed.
    """
    if tick_size <= 0:
        return round(price, 2)
    price_dec = Decimal(str(price))
    tick_dec = Decimal(str(tick_size))
    ticks = (price_dec / tick_dec).to_integral_value(rounding=ROUND_HALF_UP)
    return float(ticks * tick_dec)


def compute_stop_target(
    entry_price: float, stop_pct: float, target_pct: float, tick_size: float = 0.0
) -> tuple[float, float]:
    """The identical stop/target formula every strategy (synthetic, ORB,
    VWAP Pullback, EMA Micro-pullback, OI/Volume Confirmed, Liquidity
    Sweep/Reversal — all six, not just the original four) computed inline —
    same percentage-off-entry shape, only the pct values differ per
    strategy. `tick_size` rounds both prices to a real, tradable value for
    the instrument (see `_round_to_tick`'s own docstring) — defaults to
    `0.0` (plain 2-decimal rounding, the old behavior) only so a caller that
    genuinely has no tick size to hand doesn't crash; every real strategy
    call site passes the instrument's actual tick size.
    """
    stop_price = _round_to_tick(entry_price * (1 - stop_pct), tick_size)
    target_price = _round_to_tick(entry_price * (1 + target_pct), tick_size)
    return stop_price, target_price


def touch_and_confirm(
    prev_bar: PriceBar,
    latest_bar: PriceBar,
    reference_level: float,
    tolerance_frac: float,
) -> Literal["bullish", "bearish"] | None:
    """"Pullback bar touches `reference_level`, confirmation bar closes back
    through it" — the touch/confirm mechanics VWAP Pullback (touching VWAP)
    and EMA Micro-pullback (touching EMA9) both need, byte-for-byte
    identical once the reference scalar is factored out. Deliberately
    returns *only* the touch/confirm direction, nothing else: each
    strategy's own trend filter (EMA's ema9-vs-ema20 gate, which VWAP has no
    equivalent of) and `structure_level` assignment (VWAP uses the touched
    bar's own extreme; EMA uses the reference level itself) stay
    strategy-owned — those differ in ways that would silently change
    behavior if folded in here too.
    """
    band = reference_level * tolerance_frac
    close = float(latest_bar.close)

    # "Touched" means the pullback bar's extreme landed within `band` of
    # reference_level — not merely "somewhere below/above it", which would
    # also match a bar that blew straight through the level.
    touched_from_above = abs(float(prev_bar.low) - reference_level) <= band
    bullish_confirmation = close > float(prev_bar.high) and close > reference_level
    touched_from_below = abs(float(prev_bar.high) - reference_level) <= band
    bearish_confirmation = close < float(prev_bar.low) and close < reference_level

    if touched_from_above and bullish_confirmation:
        return "bullish"
    if touched_from_below and bearish_confirmation:
        return "bearish"
    return None


class ConfirmationFilterStrategy(Strategy):
    """Template method: `evaluate()` applies the two generic guards above,
    then delegates the actual setup logic to `check_setup`, which receives
    the single latest completed bar (already the one `evaluate` gated on) —
    any additional history a strategy needs, it fetches itself via
    `get_recent_completed_bars`.
    """

    def __init__(self, instrument_id: uuid.UUID, timeframe: str = BAR_TIMEFRAME) -> None:
        self.instrument_id = instrument_id
        self.timeframe = timeframe
        self._last_seen_bucket_start: datetime | None = None
        self._logged_keys: set[str] = set()

    def _log_once(self, logger: logging.Logger, key: str, msg: str, *args: object) -> None:
        """Logs `msg % args` at most once per `key` for this strategy
        instance's lifetime — a fresh instance is constructed per
        `StrategyRun` (see `api.v1.strategies._build_strategy`), so this is
        effectively "once per run," not just once per process. Needed
        because `check_setup` runs on every newly-completed bar for the
        rest of the session once triggered — an un-gated skip-reason log
        would repeat for hours on a single filtered-out day. `key`
        distinguishes independent skip reasons (e.g. "cutoff" vs
        "range_filter") so each logs its own first occurrence once,
        independent of the others.
        """
        if key in self._logged_keys:
            return
        self._logged_keys.add(key)
        logger.info(msg, *args)

    def evaluate(self, db: Session, strategy_run: StrategyRun) -> TradeProposal | None:
        if get_open_position_for_run(db, strategy_run) is not None:
            return None

        latest = get_recent_completed_bars(db, self.instrument_id, self.timeframe, limit=1)
        if not latest:
            return None
        bar = latest[0]

        already_seen = (
            self._last_seen_bucket_start is not None
            and bar.bucket_start <= self._last_seen_bucket_start
        )
        if already_seen:
            return None
        self._last_seen_bucket_start = bar.bucket_start

        return self.check_setup(db, strategy_run, bar)

    @abstractmethod
    def check_setup(
        self, db: Session, strategy_run: StrategyRun, latest_bar: PriceBar
    ) -> TradeProposal | None:
        """Called at most once per newly-completed bar, only when this run
        has no open position — implement the strategy-specific entry logic
        here, using `get_recent_completed_bars` for any extra history."""
