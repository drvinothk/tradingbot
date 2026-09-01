"""`market_data.market_hours` — pure time-boundary logic, no threading or
I/O. Boundary tests specifically (each `<=`/`<` edge), since an off-by-one
here would either block real market activity or fail to enforce the
off-hours policy at all.
"""

from __future__ import annotations

from datetime import time

from app.modules.market_data.market_hours import (
    DATA_FLOW_EXPECTED_END,
    MARKET_CLOSE,
    MARKET_OPEN,
    NORMAL_MARKET_OPEN,
    PRE_MARKET_END,
    REPLAY_MODE_MARKET_CLOSE,
    MarketPhase,
    current_phase,
    is_data_flow_expected,
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


# -- replay mode: extended 23:30 IST cutoff for TrueData's aftermarket
# replay server (2026-08-10) — explicit replay_mode=True/False overrides
# test the boundary logic itself, deterministically, without touching
# Settings; the last two tests confirm the *default* (no override passed,
# every real call site's own behavior) actually reads Settings.market_data
# .is_replay_mode as documented.


def test_standard_mode_still_closes_at_16_00_even_with_override_explicitly_false():
    assert current_phase(MARKET_CLOSE, replay_mode=False) is MarketPhase.CLOSED
    assert current_phase(time(15, 59, 59), replay_mode=False) is MarketPhase.ACTIVE_MARKET


def test_replay_mode_stays_active_past_16_00():
    assert current_phase(time(20, 0), replay_mode=True) is MarketPhase.ACTIVE_MARKET
    assert current_phase(MARKET_CLOSE, replay_mode=True) is MarketPhase.ACTIVE_MARKET


def test_replay_mode_just_before_23_30_is_still_active_market():
    assert current_phase(time(23, 29, 59), replay_mode=True) is MarketPhase.ACTIVE_MARKET


def test_replay_mode_exactly_at_23_30_is_closed():
    assert current_phase(REPLAY_MODE_MARKET_CLOSE, replay_mode=True) is MarketPhase.CLOSED


def test_replay_mode_after_23_30_is_closed():
    assert current_phase(time(23, 45), replay_mode=True) is MarketPhase.CLOSED


def test_replay_mode_does_not_change_the_pre_market_or_open_boundaries():
    assert current_phase(time(8, 29, 59), replay_mode=True) is MarketPhase.CLOSED
    assert current_phase(MARKET_OPEN, replay_mode=True) is MarketPhase.PRE_MARKET


def test_is_within_market_hours_true_late_evening_only_in_replay_mode():
    assert is_within_market_hours(time(20, 0), replay_mode=True) is True
    assert is_within_market_hours(time(20, 0), replay_mode=False) is False


def test_default_replay_mode_reads_from_settings_when_true(monkeypatch):
    # _resolve_market_close imports get_settings locally each call (see its
    # own docstring) -- patching app.config.settings' own get_settings, not
    # anything on the market_hours module itself, is what actually reaches it.
    import app.config.settings as settings_module

    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: _FakeSettingsForReplay(is_replay_mode=True),
    )

    assert current_phase(time(20, 0)) is MarketPhase.ACTIVE_MARKET


def test_default_replay_mode_reads_from_settings_when_false(monkeypatch):
    import app.config.settings as settings_module

    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: _FakeSettingsForReplay(is_replay_mode=False),
    )

    assert current_phase(time(20, 0)) is MarketPhase.CLOSED


# -- is_data_flow_expected (2026-09-01, 15:15 upper bound added 2026-09-02):
# NSE's real 09:15 session open through a 15:15 wind-down cutoff, a strict
# subset of is_within_market_hours's wider 08:30-16:00 connectivity window.
# Boundary tests specifically -- an off-by-one here either lets a false
# failover trip / spurious staleness alert through in the 09:00-09:15 or
# 15:15-16:00 gaps, or wrongly suppresses a genuine one inside 09:15-15:15.


def test_data_flow_not_expected_during_pre_market():
    assert is_data_flow_expected(time(8, 45)) is False


def test_data_flow_not_expected_just_after_pre_market_end():
    assert is_data_flow_expected(time(9, 0)) is False
    assert is_data_flow_expected(time(9, 14, 59)) is False


def test_data_flow_expected_exactly_at_normal_market_open():
    assert is_data_flow_expected(NORMAL_MARKET_OPEN) is True


def test_data_flow_expected_through_the_middle_of_active_market():
    assert is_data_flow_expected(time(12, 0)) is True


def test_data_flow_expected_just_before_the_15_15_cutoff():
    assert is_data_flow_expected(time(15, 14, 59)) is True


def test_data_flow_not_expected_at_or_after_the_15_15_cutoff():
    assert is_data_flow_expected(DATA_FLOW_EXPECTED_END) is False
    assert is_data_flow_expected(time(15, 59, 59)) is False


def test_data_flow_not_expected_once_closed():
    assert is_data_flow_expected(MARKET_CLOSE) is False
    assert is_data_flow_expected(time(20, 0)) is False


def test_data_flow_expected_respects_replay_mode_close_extension():
    # Replay mode only ever streams in the evening (see module docstring's
    # 2026-09-01 section) -- the 09:15 floor is irrelevant there in practice,
    # but the boundary logic itself must still compose correctly: evening
    # replay data is "expected" only while ACTIVE_MARKET (extended to 23:30)
    # actually holds.
    assert is_data_flow_expected(time(20, 0), replay_mode=True) is True
    assert is_data_flow_expected(time(20, 0), replay_mode=False) is False
    assert is_data_flow_expected(REPLAY_MODE_MARKET_CLOSE, replay_mode=True) is False


def test_data_flow_expected_15_15_cutoff_does_not_apply_in_replay_mode():
    # The 15:15 upper bound is live-mode only (see module docstring's
    # 2026-09-02 section) -- replay data legitimately streams well past it,
    # right up until REPLAY_MODE_MARKET_CLOSE (23:30).
    assert is_data_flow_expected(time(15, 30), replay_mode=True) is True
    assert is_data_flow_expected(DATA_FLOW_EXPECTED_END, replay_mode=True) is True


class _FakeMarketDataSettingsForReplay:
    def __init__(self, is_replay_mode: bool) -> None:
        self.is_replay_mode = is_replay_mode


class _FakeSettingsForReplay:
    def __init__(self, is_replay_mode: bool) -> None:
        self.market_data = _FakeMarketDataSettingsForReplay(is_replay_mode)
