from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.modules.broker_adapter.base.contracts import Tick
from app.modules.market_data.indicators import (
    BarAggregator,
    EMACalculator,
    IndicatorEngine,
    VWAPCalculator,
)


def _ts(seconds_offset: float) -> datetime:
    base = datetime(2026, 7, 24, 9, 15, 0, tzinfo=UTC)
    return base + timedelta(seconds=seconds_offset)


class TestEMACalculator:
    def test_returns_none_until_warmed_up(self):
        ema = EMACalculator(period=3)
        assert ema.update(10) is None
        assert ema.update(20) is None
        assert ema.update(30) == pytest.approx(20.0)  # SMA seed
        assert ema.is_warmed_up

    def test_matches_hand_computed_sequence(self):
        ema = EMACalculator(period=3)
        ema.update(10)
        ema.update(20)
        seed = ema.update(30)
        assert seed == pytest.approx(20.0)

        alpha = 2 / 4
        expected = 40 * alpha + seed * (1 - alpha)
        assert ema.update(40) == pytest.approx(expected)

    def test_constant_price_stream_converges_to_that_price(self):
        ema = EMACalculator(period=9)
        for _ in range(9):
            value = ema.update(100.0)
        assert value == pytest.approx(100.0)
        for _ in range(50):
            value = ema.update(100.0)
        assert value == pytest.approx(100.0)

    def test_rejects_invalid_period(self):
        with pytest.raises(ValueError):
            EMACalculator(period=0)


class TestVWAPCalculator:
    def test_none_before_any_volume(self):
        vwap = VWAPCalculator()
        assert vwap.value is None

    def test_single_update_equals_price(self):
        vwap = VWAPCalculator()
        assert vwap.update(100.0, 50) == pytest.approx(100.0)

    def test_volume_weighted_average_is_correct(self):
        vwap = VWAPCalculator()
        vwap.update(100.0, 100)  # 10,000
        result = vwap.update(200.0, 300)  # +60,000 -> 70,000 / 400
        assert result == pytest.approx(70000 / 400)

    def test_reset_clears_state(self):
        vwap = VWAPCalculator()
        vwap.update(100.0, 100)
        vwap.reset()
        assert vwap.value is None
        assert vwap.update(50.0, 10) == pytest.approx(50.0)

    def test_rejects_negative_volume(self):
        vwap = VWAPCalculator()
        with pytest.raises(ValueError):
            vwap.update(100.0, -1)


class TestBarAggregator:
    def test_first_tick_never_emits_a_bar(self):
        agg = BarAggregator(timeframe_seconds=60)
        assert agg.on_tick(100.0, 10, _ts(0)) is None

    def test_ticks_within_same_bucket_do_not_emit(self):
        agg = BarAggregator(timeframe_seconds=60)
        agg.on_tick(100.0, 10, _ts(0))
        assert agg.on_tick(101.0, 5, _ts(30)) is None
        assert agg.on_tick(99.0, 5, _ts(59)) is None

    def test_crossing_bucket_boundary_emits_the_completed_bar(self):
        agg = BarAggregator(timeframe_seconds=60)
        agg.on_tick(100.0, 10, _ts(0))
        agg.on_tick(105.0, 5, _ts(30))
        agg.on_tick(95.0, 5, _ts(45))
        bar = agg.on_tick(102.0, 20, _ts(61))

        assert bar is not None
        assert bar.open == 100.0
        assert bar.high == 105.0
        assert bar.low == 95.0
        assert bar.close == 95.0  # last tick of the completed bucket
        assert bar.volume == 20  # 10 + 5 + 5, not including the tick that started the new bar

    def test_new_bar_starts_after_completion(self):
        agg = BarAggregator(timeframe_seconds=60)
        agg.on_tick(100.0, 10, _ts(0))
        agg.on_tick(102.0, 20, _ts(61))  # completes bucket 0, starts bucket 60
        assert agg.current_bar is not None
        assert agg.current_bar.open == 102.0
        assert agg.current_bar.volume == 20

    def test_rejects_invalid_timeframe(self):
        with pytest.raises(ValueError):
            BarAggregator(timeframe_seconds=0)


class TestIndicatorEngine:
    def test_returns_no_bar_while_still_within_the_current_bucket(self):
        engine = IndicatorEngine(timeframe_seconds=60)
        instrument_id = uuid.uuid4()

        _, bar = engine.on_tick(
            instrument_id,
            Tick("NIFTY", ltp=100.0, bid=99.9, ask=100.1, volume=10, oi=None, ts=_ts(0)),
        )
        assert bar is None

    def test_returns_the_completed_bar_alongside_indicator_values(self):
        engine = IndicatorEngine(timeframe_seconds=60)
        instrument_id = uuid.uuid4()

        engine.on_tick(
            instrument_id,
            Tick("NIFTY", ltp=100.0, bid=0, ask=0, volume=10, oi=None, ts=_ts(0)),
        )
        engine.on_tick(
            instrument_id,
            Tick("NIFTY", ltp=105.0, bid=0, ask=0, volume=5, oi=None, ts=_ts(30)),
        )
        values, bar = engine.on_tick(
            instrument_id,
            Tick("NIFTY", ltp=102.0, bid=0, ask=0, volume=20, oi=None, ts=_ts(61)),
        )

        assert "VWAP" in values
        assert bar is not None
        assert bar.open == 100.0
        assert bar.high == 105.0
        assert bar.close == 105.0  # last tick of the completed bucket
        assert bar.volume == 15
