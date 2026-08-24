"""Incremental ATR (Average True Range), Wilder-smoothed — the standard ATR
convention (`alpha = 1/period`, distinct from `EMACalculator`'s `2/(period+1)`).
Seeded with a simple moving average of the first `period` true-range samples,
then smoothed from there, mirroring `EMACalculator`'s own seed-then-smooth
shape. Pure, stateful, no DB/broker dependency — fed one completed bar's
high/low/close at a time by `IndicatorEngine`, same as EMA9/EMA20.
"""

from __future__ import annotations


class ATRCalculator:
    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._value: float | None = None
        self._warmup: list[float] = []
        self._prev_close: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        """Returns the new ATR value once warmed up (after `period` true-range
        samples have been seen — the first bar ever fed has no prior close,
        so its true range is just `high - low`), else None. Callers should
        treat None as "not enough history yet", not as an error.
        """
        true_range = (
            high - low
            if self._prev_close is None
            else max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        )
        self._prev_close = close

        if self._value is not None:
            self._value = (self._value * (self.period - 1) + true_range) / self.period
            return self._value

        self._warmup.append(true_range)
        if len(self._warmup) < self.period:
            return None

        self._value = sum(self._warmup) / self.period
        return self._value

    @property
    def value(self) -> float | None:
        return self._value

    @property
    def is_warmed_up(self) -> bool:
        return self._value is not None
