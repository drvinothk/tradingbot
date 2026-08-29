"""Classic floor pivot points, computed off the prior trading day's
underlying OHLC — the "far resistance/support" level source for
`run_backtest.py`'s pivot-anchored exit-mode split-leg backtesting (see
`docs/architecture/build-plan.md`/project memory for the design writeup).

Deliberately standalone and dependency-free (no DB, no app imports) so it's
directly reusable by a future live-path port of this feature without
dragging in any backtest-only machinery.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import NamedTuple, Protocol


class _HasDateAndClose(Protocol):
    # Read-only properties, not plain attributes -- a Protocol's plain
    # attribute annotations default to read-write, which a `NamedTuple`
    # field (get-only) can't satisfy structurally; `@property` matches its
    # actual read-only shape (`run_backtest.Bar` is a `NamedTuple`).
    @property
    def ts(self) -> datetime: ...
    @property
    def high(self) -> float: ...
    @property
    def low(self) -> float: ...
    @property
    def close(self) -> float: ...


class PivotLevels(NamedTuple):
    pp: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float


def compute_floor_pivots(high: float, low: float, close: float) -> PivotLevels:
    """Classic floor-trader pivot formulas. `high`/`low`/`close` are the
    *prior* trading day's underlying OHLC — the pivots computed here apply
    to the *current* trading day.
    """
    pp = (high + low + close) / 3.0
    r1 = 2 * pp - low
    s1 = 2 * pp - high
    r2 = pp + (high - low)
    s2 = pp - (high - low)
    r3 = high + 2 * (pp - low)
    s3 = low - 2 * (high - pp)
    return PivotLevels(pp=pp, r1=r1, r2=r2, r3=r3, s1=s1, s2=s2, s3=s3)


def prior_day_ohlc(
    bars: Sequence[_HasDateAndClose], trade_date: date
) -> tuple[float, float, float] | None:
    """Aggregates the most recent calendar day strictly before `trade_date`
    that has bars in `bars` into (high, low, close). `bars` must already be
    sorted ascending by timestamp (true of every underlying-bar list this
    script loads — see `run_backtest.py`'s own loaders). Returns `None` if
    no prior day exists at all (e.g. `trade_date` is the first day of the
    dataset) — callers should treat that as "no pivot level available" and
    fall back accordingly, not crash.
    """
    prior_bars = [b for b in bars if b.ts.date() < trade_date]
    if not prior_bars:
        return None
    last_day = prior_bars[-1].ts.date()
    day_bars = [b for b in prior_bars if b.ts.date() == last_day]
    high = max(b.high for b in day_bars)
    low = min(b.low for b in day_bars)
    close = day_bars[-1].close
    return high, low, close
