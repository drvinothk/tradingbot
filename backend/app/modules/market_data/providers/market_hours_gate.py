"""Wraps any real `BaseMarketDataProvider` with the market-hours schedule
(`market_data.market_hours`) — deliberately at this level, not inside any
one concrete provider, so switching `MARKET_DATA_PROVIDER` between real
providers (`angel_one` today, `shoonya` if ever selected again, any future
one) keeps the same off-hours policy automatically. See
`provider_composition.get_market_data_provider`'s own docstring for why
`"mock"` is never wrapped here.

Outside market hours, `connect`/`subscribe_ticks`/`get_price_history` are
no-ops (or return empty) rather than reaching the real provider at all —
this is the actual "strict zero-activity" policy: it prevents the call
from ever being attempted, not just discouraging it. `disconnect`/
`unsubscribe_ticks`/`get_latest_tick` are never gated — tearing down or
reading already-cached state must always be allowed regardless of the
clock, same "cleanup is never blocked" reasoning `BrokerPort` adapters
already follow elsewhere in this codebase.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from app.modules.broker_adapter.base.contracts import DepthSnapshot, PriceCandle, Tick
from app.modules.market_data.market_hours import is_within_market_hours
from app.modules.market_data.providers.base import BaseMarketDataProvider

logger = logging.getLogger("app.market_data.market_hours_gate")

TickCallback = Callable[[Tick], None]
DepthCallback = Callable[[DepthSnapshot], None]


class MarketHoursGatedProvider(BaseMarketDataProvider):
    def __init__(self, inner: BaseMarketDataProvider, *, allow_offhours: bool) -> None:
        self._inner = inner
        self._allow_offhours = allow_offhours

    def _blocked(self) -> bool:
        return not self._allow_offhours and not is_within_market_hours()

    def connect(self) -> None:
        if self._blocked():
            logger.warning("Market-hours gate: connect() blocked outside 08:30-16:00 IST")
            return
        self._inner.connect()

    def disconnect(self) -> None:
        self._inner.disconnect()

    def subscribe_ticks(
        self,
        symbols: list[str],
        on_tick: TickCallback,
        on_depth: DepthCallback | None = None,
    ) -> None:
        if self._blocked():
            logger.warning(
                "Market-hours gate: subscribe_ticks(%r) blocked outside 08:30-16:00 IST",
                symbols,
            )
            return
        self._inner.subscribe_ticks(symbols, on_tick, on_depth)

    def unsubscribe_ticks(self, symbols: list[str]) -> None:
        self._inner.unsubscribe_ticks(symbols)

    def get_latest_tick(self, symbol: str) -> Tick | None:
        return self._inner.get_latest_tick(symbol)

    def get_price_history(
        self, underlying: str, start: datetime, end: datetime, timeframe_seconds: int = 60
    ) -> list[PriceCandle]:
        if self._blocked():
            logger.warning(
                "Market-hours gate: get_price_history(%r) blocked outside 08:30-16:00 IST",
                underlying,
            )
            return []
        return self._inner.get_price_history(underlying, start, end, timeframe_seconds)

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()
