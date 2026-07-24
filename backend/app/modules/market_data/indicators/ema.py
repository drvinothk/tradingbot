"""Incremental EMA. Seeded with a simple moving average of the first
`period` samples (the standard approach), then updated exponentially from
there — pure, stateful, no DB/broker dependency, so it's trivially testable
against a plain list of prices.
"""

from __future__ import annotations


class EMACalculator:
    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._alpha = 2 / (period + 1)
        self._value: float | None = None
        self._warmup: list[float] = []

    def update(self, price: float) -> float | None:
        """Returns the new EMA value once warmed up (after `period` samples
        have been seen), else None — callers should treat None as "not
        enough history yet", not as an error.
        """
        if self._value is not None:
            self._value = price * self._alpha + self._value * (1 - self._alpha)
            return self._value

        self._warmup.append(price)
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
