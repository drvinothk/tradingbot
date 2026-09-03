"""Pure-function tests for `momentum_plateau_signals`/`PlateauParams`
(`app.modules.strategy_engine.common_rules`) — the shared threshold logic
behind the new exit-side momentum-plateau check, used identically by
production (`execution_engine.paper.service._momentum_plateau_detected`)
and the backtest engine's own in-memory-series equivalent. No DB session
needed here; the DB-backed wrapper is covered at the integration level in
`tests/integration/test_execution_paper_service.py`.
"""

from __future__ import annotations

from app.modules.strategy_engine.common_rules import PlateauParams, momentum_plateau_signals

FLAT_CLOSES = [24000.0, 24001.0, 24000.5, 24001.5, 24000.0, 24001.0]  # 6 bars, N=5 window
TRENDING_CLOSES = [24000.0, 24010.0, 24025.0, 24040.0, 24060.0, 24085.0]  # steep, non-flat
FLAT_RSI = [55.0, 56.0, 54.5, 55.5, 55.0, 56.0]
TRENDING_RSI = [50.0, 58.0, 65.0, 71.0, 76.0, 80.0]


def _params(**overrides: object) -> PlateauParams:
    overrides.setdefault("lookback_bars", 5)
    return PlateauParams(enabled=True, **overrides)  # type: ignore[arg-type]


def test_both_signals_disabled_never_plateaus() -> None:
    p = _params(use_slope=False, use_rsi=False)
    assert momentum_plateau_signals(FLAT_CLOSES, FLAT_RSI, atr=10.0, params=p) is False


def test_slope_only_flags_flat_price() -> None:
    p = _params(use_slope=True, use_rsi=False, slope_atr_fraction=0.3)
    # |24001.0 - 24000.0| = 1.0, well under 0.3 * ATR(10) = 3.0 -> flat.
    assert momentum_plateau_signals(FLAT_CLOSES, TRENDING_RSI, atr=10.0, params=p) is True


def test_slope_only_does_not_flag_a_steep_trend() -> None:
    p = _params(use_slope=True, use_rsi=False, slope_atr_fraction=0.3)
    # |24085 - 24000| = 85, far beyond 0.3 * ATR(10) = 3.0 -> not flat.
    assert momentum_plateau_signals(TRENDING_CLOSES, FLAT_RSI, atr=10.0, params=p) is False


def test_rsi_only_flags_flat_rsi() -> None:
    p = _params(use_slope=False, use_rsi=True, rsi_flatten_delta=5.0)
    # |56.0 - 55.0| = 1.0, under the 5.0 delta -> flat.
    assert momentum_plateau_signals(TRENDING_CLOSES, FLAT_RSI, atr=10.0, params=p) is True


def test_rsi_only_does_not_flag_moving_rsi() -> None:
    p = _params(use_slope=False, use_rsi=True, rsi_flatten_delta=5.0)
    # |80.0 - 50.0| = 30, far beyond the 5.0 delta -> not flat.
    assert momentum_plateau_signals(FLAT_CLOSES, TRENDING_RSI, atr=10.0, params=p) is False


def test_combine_any_fires_when_only_one_signal_is_flat() -> None:
    p = _params(use_slope=True, use_rsi=True, combine_mode="any")
    # Slope flat (FLAT_CLOSES), RSI trending (TRENDING_RSI) -- OR still fires.
    assert momentum_plateau_signals(FLAT_CLOSES, TRENDING_RSI, atr=10.0, params=p) is True


def test_combine_all_does_not_fire_when_only_one_signal_is_flat() -> None:
    p = _params(use_slope=True, use_rsi=True, combine_mode="all")
    assert momentum_plateau_signals(FLAT_CLOSES, TRENDING_RSI, atr=10.0, params=p) is False


def test_combine_all_fires_when_both_signals_are_flat() -> None:
    p = _params(use_slope=True, use_rsi=True, combine_mode="all")
    assert momentum_plateau_signals(FLAT_CLOSES, FLAT_RSI, atr=10.0, params=p) is True


def test_insufficient_bar_history_degrades_to_not_plateaued() -> None:
    p = _params(use_slope=True, use_rsi=False, lookback_bars=5)
    # Only 3 closes -- fewer than lookback_bars + 1 = 6 -- must not raise,
    # must not falsely flag a plateau on incomplete data.
    assert momentum_plateau_signals([24000.0, 24001.0, 24000.5], [], atr=10.0, params=p) is False


def test_missing_atr_degrades_slope_check_to_not_plateaued() -> None:
    p = _params(use_slope=True, use_rsi=False)
    assert momentum_plateau_signals(FLAT_CLOSES, [], atr=None, params=p) is False


def test_zero_atr_degrades_slope_check_to_not_plateaued() -> None:
    p = _params(use_slope=True, use_rsi=False)
    assert momentum_plateau_signals(FLAT_CLOSES, [], atr=0.0, params=p) is False


def test_extra_leading_history_is_trimmed_to_the_window() -> None:
    """A caller passing more than lookback_bars + 1 entries (the production
    wrapper's DB query already limits exactly, but the backtest's in-memory
    series may hand over a longer slice) must only compare the trailing
    window, not the whole list."""
    long_trending_then_flat = [20000.0, 21000.0, 22000.0] + FLAT_CLOSES
    p = _params(use_slope=True, use_rsi=False, lookback_bars=5)
    assert momentum_plateau_signals(long_trending_then_flat, [], atr=10.0, params=p) is True


def test_from_params_defaults_to_disabled() -> None:
    params = PlateauParams.from_params({})
    assert params.enabled is False
    assert params.use_slope is False
    assert params.use_rsi is False
    assert params.combine_mode == "any"
    assert params.lookback_bars == 5
    assert params.slope_atr_fraction == 0.3
    assert params.rsi_flatten_delta == 5.0


def test_from_params_parses_every_key() -> None:
    params = PlateauParams.from_params(
        {
            "require_momentum_plateau_exit": True,
            "plateau_use_slope": True,
            "plateau_use_rsi": True,
            "plateau_combine_mode": "all",
            "plateau_lookback_bars": 8,
            "plateau_slope_atr_fraction": 0.5,
            "plateau_rsi_flatten_delta": 3.0,
        }
    )
    assert params == PlateauParams(
        enabled=True,
        use_slope=True,
        use_rsi=True,
        combine_mode="all",
        lookback_bars=8,
        slope_atr_fraction=0.5,
        rsi_flatten_delta=3.0,
    )


def test_from_params_rejects_an_unrecognized_combine_mode() -> None:
    """An invalid value (typo, stale config) must fail safe to the default
    'any', never raise and never silently behave as 'all'."""
    params = PlateauParams.from_params({"plateau_combine_mode": "xor"})
    assert params.combine_mode == "any"
