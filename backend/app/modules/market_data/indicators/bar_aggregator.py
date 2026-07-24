"""Tick-to-OHLCV bar aggregation. EMA is conventionally computed on bar
closes (e.g. 1-minute candles), not raw ticks — feeding it every tick would
make "EMA9" mean something different every time tick frequency changes.
VWAP, by contrast, is genuinely tick/volume cumulative and doesn't go
through this aggregator (see vwap.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Bar:
    bucket_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class BarAggregator:
    def __init__(self, timeframe_seconds: int = 60) -> None:
        if timeframe_seconds < 1:
            raise ValueError("timeframe_seconds must be >= 1")
        self.timeframe_seconds = timeframe_seconds
        self._current_bucket: datetime | None = None
        self._current_bar: Bar | None = None

    def _bucket_for(self, ts: datetime) -> datetime:
        epoch = ts.timestamp()
        bucket_epoch = epoch - (epoch % self.timeframe_seconds)
        return datetime.fromtimestamp(bucket_epoch, tz=ts.tzinfo)

    def on_tick(self, price: float, volume: int, ts: datetime) -> Bar | None:
        """Returns the just-completed Bar the moment a new bucket starts,
        else None while still accumulating the current one. The in-progress
        bar is never returned mid-formation — callers only ever see finished
        bars, so an EMA fed from this can't see a partial candle.
        """
        bucket = self._bucket_for(ts)

        if self._current_bucket is None:
            self._current_bucket = bucket
            self._current_bar = Bar(
                bucket_start=bucket, open=price, high=price, low=price, close=price, volume=volume
            )
            return None

        if bucket == self._current_bucket:
            bar = self._current_bar
            assert bar is not None
            bar.high = max(bar.high, price)
            bar.low = min(bar.low, price)
            bar.close = price
            bar.volume += volume
            return None

        completed = self._current_bar
        self._current_bucket = bucket
        self._current_bar = Bar(
            bucket_start=bucket, open=price, high=price, low=price, close=price, volume=volume
        )
        return completed

    @property
    def current_bar(self) -> Bar | None:
        """The in-progress (not yet completed) bar, if any."""
        return self._current_bar
