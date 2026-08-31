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
    """Phase 5's research spike confirmed Shoonya's documented limits
    (shoonya.com FAQ): GetQuotes 10/sec & 200/min, order placement 20/sec &
    200/min per service instance. 5/second with a burst of 10 stays under
    the tightest of those (GetQuotes) even though every call type shares
    this one limiter, which is intentional — this system is explicitly not
    high-frequency (see module docstring), so there's no reason to run
    closer to the real ceiling than "comfortably safe."

    **2026-08-20 — capacity raised 10 -> 50, refill 5.0 -> 6.0/sec, live
    incident.** `ShoonyaBrokerAdapter.get_option_chain` calls `GetQuotes`
    once per strike in the chain plus once for the underlying's own LTP —
    a single option-chain refresh (itself gated to roughly once per ~2
    minutes per instrument+expiry by `market_data.freshness
    .OPTION_CHAIN_THRESHOLDS`, not per-cycle) is a burst of ~41 calls
    against this one shared bucket, not a trickle. With the old capacity=10,
    a single refresh alone needed ~6s to drain (10 immediate + 31 more at
    5/sec) even in total isolation — confirmed live: 20
    `RateLimitExceeded: broker call limiter timed out waiting to call
    GetQuotes` errors across 2 concurrently-scanning strategies in 1.5
    hours, each one skipping that entire evaluation cycle
    (`ensure_fresh_option_chain` catches `BrokerError` and reports `DEAD`,
    per its own docstring — contained, not a crash, but a real missed
    cycle). Traced to root cause via the actual traceback before changing
    anything, not guessed. New capacity=50 lets one full refresh burst
    drain without queuing at all; refill kept well under Shoonya's real
    10/sec ceiling (leaves headroom for PositionManager's own concurrent
    polling, which shares this same bucket). Sustained-average risk against
    Shoonya's 200/min figure is a known, deliberate tradeoff, not
    overlooked — this system's own real traffic is bursty-then-quiet, not
    continuously saturated (see module docstring), so a burst-sized
    capacity increase is the correctly-targeted fix for the actual observed
    failure mode. **Check back**: if `RateLimitExceeded` errors keep
    recurring after this change deploys, the bottleneck isn't capacity —
    it's the one-`GetQuotes`-call-per-strike design of `get_option_chain`
    itself, which would need addressing next (e.g. Shoonya's own possible
    multi-quote/batched endpoint, not yet checked).

    **2026-08-31 — that prediction came true, live incident.** 2 concurrent
    strategies at the time of the note above became 11 (real strategy-config
    growth since 2026-08-20), all sharing one NIFTY expiry, and
    `RateLimitExceeded` errors recurred — but the "contained, not a crash"
    claim two paragraphs up was itself wrong: `RateLimitExceeded` is a plain
    `Exception`, not a `BrokerError`, so `ensure_fresh_option_chain` was
    never actually catching it — it propagated uncaught out of
    `StrategyRunner.run_cycle`, crashing the entire cycle (not just the
    option-chain check), a real gap present since this very capacity fix
    landed on 2026-08-20 and never previously verified. Root-caused and
    fixed the same day: `ensure_fresh_option_chain` now also catches
    `RateLimitExceeded` (matching what this docstring always claimed), and
    a new in-process, non-blocking coalescing lock keyed by
    `(instrument_id, expiry_date)` (`market_data.freshness
    ._refresh_lock_for`) means only one of the N strategies sharing a chain
    actually calls the broker per refresh window — the other N-1 just read
    the row it writes. `OPTION_CHAIN_THRESHOLDS.degraded_after_seconds`
    also lowered 120s -> 60s the same day once the coalescing landed (safe
    now that refresh cost no longer multiplies by strategy count) — see
    that constant's own comment in `market_data.freshness` for the
    escalation plan if this recurs again.
    """
    return TokenBucket(capacity=50, refill_rate_per_second=6.0)
