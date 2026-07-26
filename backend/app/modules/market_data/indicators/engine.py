"""Per-instrument indicator state: VWAP updates every tick, EMA9/EMA20
update only when a bar completes. Pure logic — no DB/broker dependency, so
it's fully testable against synthetic ticks; the ingestion service is what
wires this to persistence.
"""

from __future__ import annotations

import uuid

from app.modules.broker_adapter.base.contracts import Tick
from app.modules.market_data.indicators.bar_aggregator import Bar, BarAggregator
from app.modules.market_data.indicators.ema import EMACalculator
from app.modules.market_data.indicators.vwap import VWAPCalculator

EMA_SHORT_PERIOD = 9
EMA_LONG_PERIOD = 20


class IndicatorEngine:
    def __init__(self, timeframe_seconds: int = 60) -> None:
        self.timeframe_seconds = timeframe_seconds
        self._bar_aggregators: dict[uuid.UUID, BarAggregator] = {}
        self._ema9: dict[uuid.UUID, EMACalculator] = {}
        self._ema20: dict[uuid.UUID, EMACalculator] = {}
        self._vwap: dict[uuid.UUID, VWAPCalculator] = {}

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

        return results, completed_bar

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
