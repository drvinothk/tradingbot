"""Session-cumulative VWAP. Resets at session start (a new trading day means
a new VWAP from zero) — callers own calling `reset()` at the right time; this
class has no notion of "session" or wall-clock time on its own.
"""

from __future__ import annotations


class VWAPCalculator:
    def __init__(self) -> None:
        self._cum_price_volume = 0.0
        self._cum_volume = 0

    def update(self, price: float, volume: int) -> float | None:
        if volume < 0:
            raise ValueError("volume must be >= 0")
        self._cum_price_volume += price * volume
        self._cum_volume += volume
        return self.value

    def reset(self) -> None:
        self._cum_price_volume = 0.0
        self._cum_volume = 0

    @property
    def value(self) -> float | None:
        return None if self._cum_volume == 0 else self._cum_price_volume / self._cum_volume
