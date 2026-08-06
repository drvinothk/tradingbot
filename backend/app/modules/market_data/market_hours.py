"""The live-market-data session schedule — pure time-boundary logic, no
threading or I/O, shared by `MarketDataScheduler` (which acts on phase
transitions) and `MarketHoursGatedProvider` (which gates outbound calls
against the current phase) so both have one source of truth instead of
each hardcoding the same three timestamps.

Deliberately broker-agnostic — this is a property of when a real market-
data session should be open at all, not anything specific to Angel One.
Applies to whichever real `BaseMarketDataProvider` is configured
(`MARKET_DATA_PROVIDER=angel_one` today, `shoonya` if ever selected as the
live-tick source again, any future real provider) uniformly, since the
gate wraps at the `BaseMarketDataProvider` composition level, not inside
any one concrete provider. See `provider_composition.get_market_data_provider`'s
own docstring for why `"mock"` is deliberately excluded from this gate.

Boundaries per 2026-08-06's explicit spec: 08:30 IST session start,
09:00 IST pre-market-to-active-market switch, 16:00 IST hard stop. NSE's
own 09:15-15:30 trading window sits inside the 09:00-16:00 "active_market"
phase deliberately — the 15 minutes on each side are for a provider's own
session warm-up/wind-down, not an attempt to model the exchange's own
hours precisely.
"""

from __future__ import annotations

import enum
from datetime import time

from app.core.clock import now_ist

MARKET_OPEN = time(8, 30)
PRE_MARKET_END = time(9, 0)
MARKET_CLOSE = time(16, 0)


class MarketPhase(enum.Enum):
    PRE_MARKET = "pre_market"
    ACTIVE_MARKET = "active_market"
    CLOSED = "closed"


def current_phase(now: time | None = None) -> MarketPhase:
    t = now if now is not None else now_ist().time()
    if MARKET_OPEN <= t < PRE_MARKET_END:
        return MarketPhase.PRE_MARKET
    if PRE_MARKET_END <= t < MARKET_CLOSE:
        return MarketPhase.ACTIVE_MARKET
    return MarketPhase.CLOSED


def is_within_market_hours(now: time | None = None) -> bool:
    return current_phase(now) is not MarketPhase.CLOSED
