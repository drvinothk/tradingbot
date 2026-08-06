"""`market_data.market_hours` — pure time-boundary logic, no threading or
I/O. Boundary tests specifically (each `<=`/`<` edge), since an off-by-one
here would either block real market activity or fail to enforce the
off-hours policy at all.
"""

from __future__ import annotations

from datetime import time

from app.modules.market_data.market_hours import (
    MARKET_CLOSE,
    MARKET_OPEN,
    PRE_MARKET_END,
    MarketPhase,
    current_phase,
    is_within_market_hours,
)


def test_just_before_open_is_closed():
    assert current_phase(time(8, 29, 59)) is MarketPhase.CLOSED


def test_exactly_at_open_is_pre_market():
    assert current_phase(MARKET_OPEN) is MarketPhase.PRE_MARKET


def test_just_before_pre_market_end_is_still_pre_market():
    assert current_phase(time(8, 59, 59)) is MarketPhase.PRE_MARKET


def test_exactly_at_pre_market_end_is_active_market():
    assert current_phase(PRE_MARKET_END) is MarketPhase.ACTIVE_MARKET


def test_just_before_close_is_still_active_market():
    assert current_phase(time(15, 59, 59)) is MarketPhase.ACTIVE_MARKET


def test_exactly_at_close_is_closed():
    assert current_phase(MARKET_CLOSE) is MarketPhase.CLOSED


def test_midnight_is_closed():
    assert current_phase(time(0, 0)) is MarketPhase.CLOSED


def test_is_within_market_hours_true_for_pre_market_and_active():
    assert is_within_market_hours(time(8, 45)) is True
    assert is_within_market_hours(time(12, 0)) is True


def test_is_within_market_hours_false_when_closed():
    assert is_within_market_hours(time(18, 0)) is False
    assert is_within_market_hours(time(2, 0)) is False
