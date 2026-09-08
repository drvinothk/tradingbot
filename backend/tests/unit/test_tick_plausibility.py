"""Pure unit tests for the shared option-premium plausibility check — see
tick_plausibility.py's own module docstring for the 2026-09-02 live incident
this exists to catch. Values below are the exact real data pulled from that
incident's raw quote_ticks, not synthetic placeholders.
"""
from __future__ import annotations

from app.modules.market_data.tick_plausibility import (
    MAX_PLAUSIBLE_OPTION_PREMIUM,
    is_plausible_option_entry,
    is_plausible_option_tick,
    option_premium_upper_bound,
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


# --- Rail 2: no-arbitrage bounds (is_plausible_option_entry) --------------


def test_upper_bound_is_intrinsic_plus_extrinsic_slice():
    # NIFTY spot 23,670; C23500 is 170 ITM.
    assert option_premium_upper_bound(strike=23_500, is_call=True, spot=23_670) == (
        170.0 + 0.05 * 23_670
    )
    # P23900 is 230 ITM.
    assert option_premium_upper_bound(strike=23_900, is_call=False, spot=23_670) == (
        230.0 + 0.05 * 23_670
    )


def test_entry_rejects_a_spot_valued_leak_on_an_atm_strike():
    # The exact 2026-09-08 pattern: ltp ~= spot, empty book.
    assert (
        is_plausible_option_entry(
            ltp=23_671.15,
            bid=0.0,
            ask=0.0,
            volume=0,
            strike=23_650,
            is_call=True,
            underlying="NIFTY",
            spot=23_670.0,
        )
        is False
    )


def test_entry_rejects_a_spot_valued_leak_even_with_a_nonzero_book():
    # A corrupted quote that carries a fake book still fails the no-arb bound.
    assert (
        is_plausible_option_entry(
            ltp=23_670.0,
            bid=1.0,
            ask=2.0,
            volume=5,
            strike=23_650,
            is_call=True,
            underlying="NIFTY",
            spot=23_670.0,
        )
        is False
    )


def test_entry_accepts_a_real_atm_premium_on_a_violent_day():
    # A genuinely elevated NIFTY 0-DTE ATM premium is well under the cap.
    assert (
        is_plausible_option_entry(
            ltp=520.0,
            bid=515.0,
            ask=525.0,
            volume=1200,
            strike=23_650,
            is_call=True,
            underlying="NIFTY",
            spot=23_670.0,
        )
        is True
    )


def test_entry_accepts_a_deep_itm_premium_near_intrinsic():
    # C22000, spot 23,670 -> intrinsic 1,670; a real premium ~1,720 passes.
    assert (
        is_plausible_option_entry(
            ltp=1_720.0,
            bid=1_710.0,
            ask=1_730.0,
            volume=300,
            strike=22_000,
            is_call=True,
            underlying="NIFTY",
            spot=23_670.0,
        )
        is True
    )


def test_entry_falls_back_to_flat_ceiling_when_spot_is_unknown():
    # spot=0 -> behaves exactly like is_plausible_option_tick.
    assert (
        is_plausible_option_entry(
            ltp=99.75, bid=99.5, ask=100.0, volume=250,
            strike=23_650, is_call=True, underlying="NIFTY", spot=0.0,
        )
        is True
    )
    assert (
        is_plausible_option_entry(
            ltp=MAX_PLAUSIBLE_OPTION_PREMIUM + 1, bid=100.0, ask=101.0, volume=50,
            strike=23_650, is_call=True, underlying="NIFTY", spot=0.0,
        )
        is False
    )


def test_entry_falls_back_to_flat_ceiling_when_spot_is_out_of_sane_band():
    # A corrupted *underlying* quote (e.g. 180, option-premium-scale) must not
    # feed a garbage no-arb bound — treat spot as unknown.
    assert (
        is_plausible_option_entry(
            ltp=120.0, bid=118.0, ask=122.0, volume=90,
            strike=23_650, is_call=True, underlying="NIFTY", spot=180.0,
        )
        is True
    )


def test_entry_zero_book_check_still_wins_over_a_plausible_premium():
    assert (
        is_plausible_option_entry(
            ltp=100.0, bid=0.0, ask=0.0, volume=0,
            strike=23_650, is_call=True, underlying="NIFTY", spot=23_670.0,
        )
        is False
    )
