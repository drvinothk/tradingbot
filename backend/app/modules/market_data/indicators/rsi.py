"""Incremental RSI (Relative Strength Index), Wilder-smoothed — same
`alpha = 1/period` convention as `ATRCalculator`. Seeded with a simple
average of the first `period` gain/loss samples, then smoothed from there,
mirroring `ATRCalculator`'s own seed-then-smooth shape exactly. Pure,
stateful, no DB/broker dependency — fed one completed bar's close at a time
by `IndicatorEngine`, same as EMA9/EMA20/ATR14.
"""

from __future__ import annotations


class RSICalculator:
    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._gain_warmup: list[float] = []
        self._loss_warmup: list[float] = []
        self._prev_close: float | None = None

    def update(self, close: float) -> float | None:
        """Returns the new RSI value (0-100) once warmed up (after `period`
        bar-over-bar changes have been seen — the first bar ever fed has no
        prior close, so it contributes nothing), else None. Callers should
        treat None as "not enough history yet", not as an error.
        """
        if self._prev_close is None:
            self._prev_close = close
            return None

        delta = close - self._prev_close
        self._prev_close = close
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0

        if self._avg_gain is not None and self._avg_loss is not None:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period
        else:
            self._gain_warmup.append(gain)
            self._loss_warmup.append(loss)
            if len(self._gain_warmup) < self.period:
                return None
            self._avg_gain = sum(self._gain_warmup) / self.period
            self._avg_loss = sum(self._loss_warmup) / self.period

        if self._avg_loss == 0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @property
    def value(self) -> float | None:
        if self._avg_gain is None or self._avg_loss is None:
            return None
        if self._avg_loss == 0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @property
    def is_warmed_up(self) -> bool:
        return self._avg_gain is not None
