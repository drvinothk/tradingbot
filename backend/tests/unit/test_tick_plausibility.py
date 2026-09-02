"""Pure unit tests for the shared option-premium plausibility check — see
tick_plausibility.py's own module docstring for the 2026-09-02 live incident
this exists to catch. Values below are the exact real data pulled from that
incident's raw quote_ticks, not synthetic placeholders.
"""
from __future__ import annotations

from app.modules.market_data.tick_plausibility import (
    MAX_PLAUSIBLE_OPTION_PREMIUM,
    is_plausible_option_tick,
)


def test_rejects_a_leaked_underlying_tick_with_all_zero_bid_ask_volume():
    # NIFTY08SEP26C23850, 2026-09-02 13:49:15 IST — the real corrupted tick.
    assert is_plausible_option_tick(ltp=23871.25, bid=0.0, ask=0.0, volume=0) is False


def test_rejects_a_leaked_underlying_tick_below_the_options_own_strike():
    # NIFTY08SEP26C24000, 2026-09-02 13:57:23 IST — spot (23,869.70) sits
    # *below* the 24,000 strike, the exact case the old `ltp >= strike`
    # guard missed.
    assert is_plausible_option_tick(ltp=23869.70, bid=0.0, ask=0.0, volume=0) is False


def test_rejects_an_implausible_ltp_even_with_nonzero_bid_ask_volume():
    assert (
        is_plausible_option_tick(
            ltp=MAX_PLAUSIBLE_OPTION_PREMIUM + 1, bid=100.0, ask=101.0, volume=50
        )
        is False
    )


def test_accepts_a_realistic_option_premium():
    assert is_plausible_option_tick(ltp=99.75, bid=99.5, ask=100.0, volume=250) is True


def test_accepts_a_premium_right_at_the_ceiling():
    assert (
        is_plausible_option_tick(
            ltp=MAX_PLAUSIBLE_OPTION_PREMIUM, bid=1.0, ask=1.0, volume=1
        )
        is True
    )


def test_all_zero_bid_ask_volume_rejected_regardless_of_a_plausible_ltp():
    # A real, actively-quoted contract never has bid=ask=volume=0 during
    # market hours, even if ltp itself happens to look reasonable.
    assert is_plausible_option_tick(ltp=100.0, bid=0.0, ask=0.0, volume=0) is False
