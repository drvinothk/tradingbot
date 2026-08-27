from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.modules.broker_adapter.base.contracts import PriceCandle, Tick
from app.modules.market_data.indicators import (
    ATRCalculator,
    Bar,
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


class TestATRCalculator:
    def test_returns_none_until_warmed_up(self):
        atr = ATRCalculator(period=3)
        assert atr.update(high=10, low=8, close=9) is None  # TR=2 (no prior close)
        assert atr.update(high=12, low=9, close=11) is None  # TR=max(3,3,0)=3
        seed = atr.update(high=11, low=10, close=10.5)  # TR=max(1,0,1)=1
        assert seed == pytest.approx((2 + 3 + 1) / 3)
        assert atr.is_warmed_up

    def test_matches_hand_computed_sequence(self):
        atr = ATRCalculator(period=3)
        atr.update(high=10, low=8, close=9)
        atr.update(high=12, low=9, close=11)
        seed = atr.update(high=11, low=10, close=10.5)
        assert seed == pytest.approx(2.0)

        # Bar 4: prev_close=10.5 -> TR=max(13-10, |13-10.5|, |10-10.5|)=3
        value = atr.update(high=13, low=10, close=12)
        expected = (seed * (3 - 1) + 3) / 3
        assert value == pytest.approx(expected)
        assert value == pytest.approx(7 / 3)

    def test_first_bar_true_range_is_just_high_minus_low(self):
        atr = ATRCalculator(period=1)
        assert atr.update(high=105, low=95, close=100) == pytest.approx(10)

    def test_constant_bars_converge_to_that_true_range(self):
        atr = ATRCalculator(period=14)
        value = None
        for _ in range(30):
            value = atr.update(high=101.0, low=99.0, close=100.0)
        assert value == pytest.approx(2.0)

    def test_rejects_invalid_period(self):
        with pytest.raises(ValueError):
            ATRCalculator(period=0)


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

    def test_on_completed_bar_warms_up_ema_from_candle_closes(self):
        """The REST-polling fallback path feeds already-complete
        `PriceCandle`s (real broker OHLC), not raw ticks — EMA9/EMA20
        must warm up from those closes exactly like they do from
        `on_tick`'s own bar-completion branch.
        """
        engine = IndicatorEngine(timeframe_seconds=60)
        instrument_id = uuid.uuid4()

        results: dict[str, float] = {}
        for i in range(1, 10):
            candle = PriceCandle(
                bucket_start=_ts(i * 60),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=0,
            )
            results = engine.on_completed_bar(instrument_id, candle)

        assert results["EMA9"] == pytest.approx(100.0)
        assert "EMA20" not in results  # not warmed up yet at 9 candles
        assert "ATR14" not in results  # not warmed up yet at 9 candles either

    def test_on_completed_bar_warms_up_atr14_from_candle_high_low_close(self):
        engine = IndicatorEngine(timeframe_seconds=60)
        instrument_id = uuid.uuid4()

        results: dict[str, float] = {}
        for i in range(1, 15):
            candle = PriceCandle(
                bucket_start=_ts(i * 60),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=0,
            )
            results = engine.on_completed_bar(instrument_id, candle)

        assert results["ATR14"] == pytest.approx(2.0)

    def test_on_tick_warms_up_atr14_alongside_ema_on_bar_completion(self):
        engine = IndicatorEngine(timeframe_seconds=60)
        instrument_id = uuid.uuid4()

        values: dict[str, float] = {}
        for i in range(15):
            values, _ = engine.on_tick(
                instrument_id,
                Tick(
                    "NIFTY", ltp=100.0, bid=0, ask=0, volume=10, oi=None, ts=_ts(i * 60)
                ),
            )
        assert "ATR14" in values

    def test_on_completed_bar_never_produces_or_touches_vwap(self):
        """A completed candle has no per-trade volume to weight — VWAP is a
        tick-level concept `on_completed_bar` deliberately never feeds (see
        its own docstring). This is what correctly leaves VWAP Pullback
        unable to fire on this path, matching the real, live-confirmed gap
        that Shoonya's index feed carries no volume at all.
        """
        engine = IndicatorEngine(timeframe_seconds=60)
        instrument_id = uuid.uuid4()
        candle = PriceCandle(
            bucket_start=_ts(0), open=100.0, high=101.0, low=99.0, close=100.0, volume=5000
        )

        results = engine.on_completed_bar(instrument_id, candle)

        assert "VWAP" not in results
        assert instrument_id not in engine._vwap

    def test_reset_session_clears_vwap_but_not_ema(self):
        """VWAP is session-cumulative and must restart from zero each
        trading day; EMA deliberately does not (trend continuity across
        sessions is the whole point of an exponential average) -- see
        `reset_session`'s own docstring.
        """
        engine = IndicatorEngine(timeframe_seconds=60)
        instrument_id = uuid.uuid4()
        for i in range(10):
            engine.on_tick(
                instrument_id,
                Tick("NIFTY", ltp=100.0 + i, bid=0, ask=0, volume=10, oi=None, ts=_ts(i * 60)),
            )
        assert engine._vwap[instrument_id].value is not None
        ema9_before_reset = engine._ema9[instrument_id].value

        engine.reset_session()

        assert engine._vwap[instrument_id].value is None
        assert engine._ema9[instrument_id].value == ema9_before_reset

    def test_reset_session_for_one_instrument_leaves_others_untouched(self):
        engine = IndicatorEngine(timeframe_seconds=60)
        instrument_a, instrument_b = uuid.uuid4(), uuid.uuid4()
        engine.on_tick(
            instrument_a,
            Tick("NIFTY", ltp=100.0, bid=0, ask=0, volume=10, oi=None, ts=_ts(0)),
        )
        engine.on_tick(
            instrument_b,
            Tick("BANKNIFTY", ltp=200.0, bid=0, ask=0, volume=10, oi=None, ts=_ts(0)),
        )

        engine.reset_session(instrument_a)

        assert engine._vwap[instrument_a].value is None
        assert engine._vwap[instrument_b].value is not None

    def test_on_completed_bar_never_touches_bar_aggregator_state(self):
        """This path bypasses `BarAggregator` entirely — the candle is
        already a finished bar with real broker-side high/low,
        `BarAggregator` has no way to reconstruct that from a single point,
        and feeding it one synthetic tick per candle would silently corrupt
        the aggregated bar (open=high=low=close=candle.close). Confirms no
        aggregator state is created for an instrument that only ever goes
        through `on_completed_bar`.
        """
        engine = IndicatorEngine(timeframe_seconds=60)
        instrument_id = uuid.uuid4()
        candle = PriceCandle(
            bucket_start=_ts(0), open=100.0, high=105.0, low=95.0, close=102.0, volume=0
        )

        engine.on_completed_bar(instrument_id, candle)

        assert instrument_id not in engine._bar_aggregators


class TestWarmStart:
    """`warm_start` exists to close the exact live incident recorded in its
    own docstring: after a cold restart, EMA9 (9-bar warmup) and EMA20
    (20-bar warmup) used to re-arm ~11 minutes apart, and
    `EMAMicroPullbackStrategy` zipped a fresh EMA9 against a stale EMA20
    during that gap. These tests verify the fix's actual invariant — EMA9
    and EMA20 (and ATR14) become warmed *together*, in one synchronous call,
    not on their own independent real-time schedules.
    """

    def test_warms_ema9_ema20_atr14_simultaneously(self):
        """The whole point: unlike feeding these bars one at a time through
        real ticks (where EMA9 would be warm at bar 9, ATR14 at bar 14, and
        EMA20 not until bar 20 -- a real gap), a single `warm_start` call
        with enough history leaves all three warmed at once.
        """
        engine = IndicatorEngine(timeframe_seconds=60)
        instrument_id = uuid.uuid4()
        bars = [
            Bar(bucket_start=_ts(i * 60), open=100.0, high=101.0, low=99.0, close=100.0, volume=0)
            for i in range(25)
        ]

        engine.warm_start(instrument_id, bars)

        assert engine._ema9[instrument_id].is_warmed_up
        assert engine._ema20[instrument_id].is_warmed_up
        assert engine._atr[instrument_id].is_warmed_up

    def test_matches_replaying_the_same_bars_through_on_completed_bar(self):
        """`warm_start` must be mathematically identical to normal live
        warmup, not an approximation — same inputs, same order, same
        result, whether they arrive via `warm_start` all at once or via
        `on_completed_bar` one at a time.
        """
        bars = [
            Bar(
                bucket_start=_ts(i * 60),
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.0 + i * 0.7,
                volume=0,
            )
            for i in range(30)
        ]

        warm_started = IndicatorEngine(timeframe_seconds=60)
        warm_id = uuid.uuid4()
        warm_started.warm_start(warm_id, bars)

        live = IndicatorEngine(timeframe_seconds=60)
        live_id = uuid.uuid4()
        results: dict[str, float] = {}
        for bar in bars:
            candle = PriceCandle(
                bucket_start=bar.bucket_start,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            results = live.on_completed_bar(live_id, candle)

        assert warm_started._ema9[warm_id].value == pytest.approx(results["EMA9"])
        assert warm_started._ema20[warm_id].value == pytest.approx(results["EMA20"])
        assert warm_started._atr[warm_id].value == pytest.approx(results["ATR14"])

    def test_is_a_noop_once_a_live_tick_already_arrived(self):
        """The critical safety property: replaying stale historical bars
        over already-live state would silently regress a correct running
        value back to an approximation. If a live tick got there first
        (whatever ordering reason), `warm_start` must do nothing.
        """
        engine = IndicatorEngine(timeframe_seconds=60)
        instrument_id = uuid.uuid4()
        engine.on_tick(
            instrument_id,
            Tick("NIFTY", ltp=100.0, bid=0, ask=0, volume=10, oi=None, ts=_ts(0)),
        )
        # A second tick, crossing the bucket boundary, is what actually
        # completes the first bar and creates the EMA9/EMA20 calculator
        # entries this test needs to exist (but still cold — 1 bar in, not
        # the 9 EMA9 needs).
        engine.on_tick(
            instrument_id,
            Tick("NIFTY", ltp=100.0, bid=0, ask=0, volume=10, oi=None, ts=_ts(61)),
        )
        assert not engine._ema9[instrument_id].is_warmed_up  # only 1 bar in so far

        bars = [
            Bar(bucket_start=_ts(i * 60), open=50.0, high=51.0, low=49.0, close=50.0, volume=0)
            for i in range(1, 25)
        ]
        engine.warm_start(instrument_id, bars)

        # Still not warmed -- warm_start must not have touched this
        # instrument's calculators once a live tick already created them.
        assert not engine._ema9[instrument_id].is_warmed_up

    def test_is_idempotent_second_call_is_ignored(self):
        engine = IndicatorEngine(timeframe_seconds=60)
        instrument_id = uuid.uuid4()
        first_bars = [
            Bar(bucket_start=_ts(i * 60), open=100.0, high=101.0, low=99.0, close=100.0, volume=0)
            for i in range(25)
        ]
        engine.warm_start(instrument_id, first_bars)
        value_after_first = engine._ema9[instrument_id].value

        second_bars = [
            Bar(bucket_start=_ts(i * 60), open=200.0, high=201.0, low=199.0, close=200.0, volume=0)
            for i in range(25)
        ]
        engine.warm_start(instrument_id, second_bars)

        assert engine._ema9[instrument_id].value == pytest.approx(value_after_first)

    def test_empty_bars_list_leaves_no_state_and_stays_a_noop(self):
        """A genuinely new instrument with no persisted `price_bars` yet
        must warm up from live ticks exactly as before this method existed
        -- no half-created calculator entries left behind.
        """
        engine = IndicatorEngine(timeframe_seconds=60)
        instrument_id = uuid.uuid4()

        engine.warm_start(instrument_id, [])

        assert instrument_id not in engine._ema9
        assert instrument_id not in engine._ema20
        assert instrument_id not in engine._atr

    def test_replaying_fewer_than_the_warmup_period_leaves_indicators_unwarmed(self):
        """Same 'not enough history yet' semantics as live warmup -- a
        restart soon after market open, with fewer than 20 persisted bars
        available, must not fake a warmed EMA20 out of insufficient data.
        """
        engine = IndicatorEngine(timeframe_seconds=60)
        instrument_id = uuid.uuid4()
        bars = [
            Bar(bucket_start=_ts(i * 60), open=100.0, high=101.0, low=99.0, close=100.0, volume=0)
            for i in range(12)
        ]

        engine.warm_start(instrument_id, bars)

        assert engine._ema9[instrument_id].is_warmed_up
        assert not engine._ema20[instrument_id].is_warmed_up

    def test_other_instruments_are_unaffected(self):
        engine = IndicatorEngine(timeframe_seconds=60)
        warmed_id, untouched_id = uuid.uuid4(), uuid.uuid4()
        bars = [
            Bar(bucket_start=_ts(i * 60), open=100.0, high=101.0, low=99.0, close=100.0, volume=0)
            for i in range(25)
        ]

        engine.warm_start(warmed_id, bars)

        assert warmed_id in engine._ema9
        assert untouched_id not in engine._ema9
