"""Token-bucket rate limiter wrapping outbound broker calls. This system is
explicitly not high-frequency, so this exists purely as a safety net against
bugs (e.g. a runaway reconciliation retry loop) hammering the broker API, not
as a throughput optimization — bursty-but-bounded is the goal, not maximum
throughput.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class TokenBucket:
    capacity: float
    refill_rate_per_second: float
    _tokens: float = 0.0
    _last_refill: float = 0.0
    _lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._tokens = self.capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate_per_second)
        self._last_refill = now

    def try_acquire(self, cost: float = 1.0) -> bool:
        """Non-blocking. Returns False immediately if there isn't budget —
        callers decide whether to queue, drop, or raise."""
        with self._lock:
            self._refill()
            if self._tokens >= cost:
                self._tokens -= cost
                return True
            return False

    def acquire_blocking(self, cost: float = 1.0, timeout: float | None = None) -> bool:
        """Blocks (sleeping, not busy-waiting) until budget is available or
        timeout elapses. Returns False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.try_acquire(cost):
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


class RateLimitExceeded(Exception):
    pass


def make_broker_call_limiter() -> TokenBucket:
    """Conservative default until Phase 5's research spike confirms
    Shoonya's actual documented rate limits — 5/second with a small burst
    allowance is comfortably under what any broker API tends to allow for a
    low-frequency retail system, and safety-net limiters should start
    conservative and widen only once the real limits are confirmed, not the
    other way around.
    """
    return TokenBucket(capacity=10, refill_rate_per_second=5.0)
