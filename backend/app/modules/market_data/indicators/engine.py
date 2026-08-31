"""Per-instrument indicator state: VWAP updates every tick, EMA9/EMA20/
ATR14/RSI14 update only when a bar completes. Pure logic — no DB/broker
dependency, so it's fully testable against synthetic ticks; the ingestion
service is what wires this to persistence.
"""

from __future__ import annotations

import uuid

from app.modules.broker_adapter.base.contracts import PriceCandle, Tick
from app.modules.market_data.indicators.atr import ATRCalculator
from app.modules.market_data.indicators.bar_aggregator import Bar, BarAggregator
from app.modules.market_data.indicators.ema import EMACalculator
from app.modules.market_data.indicators.rsi import RSICalculator
from app.modules.market_data.indicators.vwap import VWAPCalculator

EMA_SHORT_PERIOD = 9
EMA_LONG_PERIOD = 20
ATR_PERIOD = 14
RSI_PERIOD = 14


class IndicatorEngine:
    def __init__(self, timeframe_seconds: int = 60) -> None:
        self.timeframe_seconds = timeframe_seconds
        self._bar_aggregators: dict[uuid.UUID, BarAggregator] = {}
        self._ema9: dict[uuid.UUID, EMACalculator] = {}
        self._ema20: dict[uuid.UUID, EMACalculator] = {}
        self._vwap: dict[uuid.UUID, VWAPCalculator] = {}
        self._atr: dict[uuid.UUID, ATRCalculator] = {}
        self._rsi: dict[uuid.UUID, RSICalculator] = {}

    def on_tick(self, instrument_id: uuid.UUID, tick: Tick) -> tuple[dict[str, float], Bar | None]:
        """Feeds one tick for `instrument_id`. Returns the indicator values
        that actually changed on this call (e.g. `{"VWAP": ...}` most ticks,
        plus `EMA9`/`EMA20` only on the tick that completes a bar — an empty
        dict means nothing new to persist yet, which only happens on
        zero-volume ticks before VWAP has warmed up), plus the just-completed
        `Bar` on that same tick, else `None` — Phase 4's strategies persist
        this via `market_data.ingestion` for real opening-range/pullback/
        confirmation-candle structure, not just the EMA/VWAP scalars above.
        """
        results: dict[str, float] = {}

        vwap_calc = self._vwap.setdefault(instrument_id, VWAPCalculator())
        vwap_value = vwap_calc.update(tick.ltp, tick.volume)
        if vwap_value is not None:
            results["VWAP"] = vwap_value

        bar_agg = self._bar_aggregators.setdefault(
            instrument_id, BarAggregator(self.timeframe_seconds)
        )
        completed_bar = bar_agg.on_tick(tick.ltp, tick.volume, tick.ts)

        if completed_bar is not None:
            ema9_calc = self._ema9.setdefault(instrument_id, EMACalculator(EMA_SHORT_PERIOD))
            ema20_calc = self._ema20.setdefault(instrument_id, EMACalculator(EMA_LONG_PERIOD))
            ema9_value = ema9_calc.update(completed_bar.close)
            ema20_value = ema20_calc.update(completed_bar.close)
            if ema9_value is not None:
                results["EMA9"] = ema9_value
            if ema20_value is not None:
                results["EMA20"] = ema20_value

            atr_calc = self._atr.setdefault(instrument_id, ATRCalculator(ATR_PERIOD))
            atr_value = atr_calc.update(completed_bar.high, completed_bar.low, completed_bar.close)
            if atr_value is not None:
                results["ATR14"] = atr_value

            rsi_calc = self._rsi.setdefault(instrument_id, RSICalculator(RSI_PERIOD))
            rsi_value = rsi_calc.update(completed_bar.close)
            if rsi_value is not None:
                results["RSI14"] = rsi_value

        return results, completed_bar

    def on_completed_bar(self, instrument_id: uuid.UUID, candle: PriceCandle) -> dict[str, float]:
        """For a REST-polled `PriceCandle` (already a complete, broker-
        supplied bar) rather than a raw tick — the WS-fallback path in
        `market_data.ingestion` uses this instead of `on_tick`. Updates
        EMA9/EMA20/ATR14 from the candle's own close (and, for ATR, its real
        high/low too), same as `on_tick` does the moment `BarAggregator`
        completes a bar — but skips `BarAggregator`
        entirely (there's nothing left to aggregate; the candle already is
        the finished bar, with real broker-side high/low `BarAggregator`
        has no way to reconstruct from a single point) and skips VWAP
        (a tick/volume-cumulative concept a discrete completed candle can't
        correctly feed one sample at a time — see `PriceCandle`'s own
        docstring on why this path's `volume` can legitimately be `0` for
        the NSE index tokens it's built for; VWAP-dependent strategies
        simply see no VWAP value on this path, same as they already do
        before volume-based VWAP ever warms up).
        """
        ema9_calc = self._ema9.setdefault(instrument_id, EMACalculator(EMA_SHORT_PERIOD))
        ema20_calc = self._ema20.setdefault(instrument_id, EMACalculator(EMA_LONG_PERIOD))
        ema9_value = ema9_calc.update(candle.close)
        ema20_value = ema20_calc.update(candle.close)

        atr_calc = self._atr.setdefault(instrument_id, ATRCalculator(ATR_PERIOD))
        atr_value = atr_calc.update(candle.high, candle.low, candle.close)

        rsi_calc = self._rsi.setdefault(instrument_id, RSICalculator(RSI_PERIOD))
        rsi_value = rsi_calc.update(candle.close)

        results: dict[str, float] = {}
        if ema9_value is not None:
            results["EMA9"] = ema9_value
        if ema20_value is not None:
            results["EMA20"] = ema20_value
        if atr_value is not None:
            results["ATR14"] = atr_value
        if rsi_value is not None:
            results["RSI14"] = rsi_value
        return results

    def warm_start(self, instrument_id: uuid.UUID, bars: list[Bar]) -> None:
        """Replays already-persisted completed bars (oldest first) through
        EMA9/EMA20/ATR14 so a freshly-constructed engine — built cold on
        every process restart, since none of this state persists on its own
        (see this class's own module docstring) — doesn't warm up from zero
        in real time.

        **Why this matters**: EMA9 (9-bar warmup) and EMA20 (20-bar warmup)
        only ever advance on live `on_tick`/`on_completed_bar` calls, so
        after a cold restart they'd otherwise re-arm ~11 minutes apart.
        `EMAMicroPullbackStrategy`'s own expansion filter reads the latest N
        EMA9 and EMA20 rows from `indicator_snapshots` as two independent
        queries and zips them positionally, assuming same-bar pairing
        (`common_rules.get_recent_indicator_values`'s own docstring states
        this assumption explicitly) — during that gap it silently paired a
        fresh post-restart EMA9 against a stale pre-restart EMA20,
        manufacturing a false "accelerating expansion" spread neither
        indicator actually showed on its own. Live-confirmed 2026-08-26: a
        restart at ~10:33 IST produced exactly this gap (EMA9 resumed at
        10:42, EMA20 not until 10:53) and fired a real, losing counter-trend
        trade off the resulting bogus signal.

        Idempotent and one-shot per `instrument_id` for this engine's
        lifetime — a no-op if this instrument already has EMA9/EMA20
        calculator state, whether from an earlier `warm_start` call or
        because a live tick already arrived first. Replaying historical bars
        over already-live state would silently regress a correct running
        value back to a stale approximation, not fix anything — so callers
        must call this once, synchronously, *before* the live provider
        subscription for `instrument_id` begins (see
        `MarketDataIngestionService.start`'s own call site).

        Deliberately does **not** touch the database — unlike
        `on_completed_bar`, nothing here is persisted to
        `indicator_snapshots`, and `bars` themselves must already exist in
        `price_bars` (re-inserting them would hit `uq_price_bar_bucket`).
        Only this engine's in-memory calculator state is fast-forwarded; the
        next live completed bar persists EMA9+EMA20 together as normal,
        which is what actually closes the gap going forward (both are
        warmed together by this point, so neither is ever missing from that
        write).
        """
        if instrument_id in self._ema9 or instrument_id in self._ema20:
            return
        if not bars:
            return  # nothing to replay -- leave state untouched, not "touched but cold"

        ema9_calc = self._ema9.setdefault(instrument_id, EMACalculator(EMA_SHORT_PERIOD))
        ema20_calc = self._ema20.setdefault(instrument_id, EMACalculator(EMA_LONG_PERIOD))
        atr_calc = self._atr.setdefault(instrument_id, ATRCalculator(ATR_PERIOD))
        rsi_calc = self._rsi.setdefault(instrument_id, RSICalculator(RSI_PERIOD))
        for bar in bars:
            ema9_calc.update(bar.close)
            ema20_calc.update(bar.close)
            atr_calc.update(bar.high, bar.low, bar.close)
            rsi_calc.update(bar.close)

    def reset_session(self, instrument_id: uuid.UUID | None = None) -> None:
        """VWAP resets at the start of each trading day; EMA deliberately
        does not (trend continuity across sessions is the whole point of an
        exponential average) — this only ever touches VWAP state.
        """
        if instrument_id is None:
            for calc in self._vwap.values():
                calc.reset()
        else:
            self._vwap.setdefault(instrument_id, VWAPCalculator()).reset()
