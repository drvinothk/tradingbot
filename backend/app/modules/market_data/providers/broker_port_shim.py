"""Wraps any `BrokerPort` (the mock adapter, or a connected `ShoonyaBrokerAdapter`)
behind `BaseMarketDataProvider` — kept for fallback/testing and for every
existing test/local-dev run that hasn't opted into Angel One, not the
production default. `connect`/`disconnect` are no-ops: a `BrokerPort`
instance authenticates outside this class's control entirely (the mock
needs no auth; Shoonya's own OAuth flow lives in `api.v1.shoonya`).
"""

from __future__ import annotations

import threading
from datetime import datetime

from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.base.contracts import PriceCandle, Tick
from app.modules.market_data.providers.base import (
    BaseMarketDataProvider,
    DepthCallback,
    TickCallback,
)


class BrokerPortMarketDataAdapter(BaseMarketDataProvider):
    def __init__(self, broker: BrokerPort) -> None:
        self._broker = broker
        self._lock = threading.Lock()
        self._latest_ticks: dict[str, Tick] = {}
        self._on_tick_external: TickCallback | None = None

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def subscribe_ticks(
        self,
        symbols: list[str],
        on_tick: TickCallback,
        on_depth: DepthCallback | None = None,
    ) -> None:
        self._on_tick_external = on_tick
        self._broker.subscribe_quotes(symbols, on_tick=self._handle_tick, on_depth=on_depth)

    def unsubscribe_ticks(self, symbols: list[str]) -> None:
        self._broker.unsubscribe_quotes(symbols)

    def _handle_tick(self, tick: Tick) -> None:
        with self._lock:
            self._latest_ticks[tick.contract_symbol] = tick
        if self._on_tick_external is not None:
            self._on_tick_external(tick)

    def get_latest_tick(self, symbol: str) -> Tick | None:
        with self._lock:
            return self._latest_ticks.get(symbol)

    def get_price_history(
        self, underlying: str, start: datetime, end: datetime, timeframe_seconds: int = 60
    ) -> list[PriceCandle]:
        return self._broker.get_price_history(underlying, start, end, timeframe_seconds)
