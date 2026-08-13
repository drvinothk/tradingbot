"""Wraps any `BrokerPort` (the mock adapter, or a connected `ShoonyaBrokerAdapter`)
behind `BaseMarketDataProvider` — kept for fallback/testing and for every
existing test/local-dev run that hasn't opted into Angel One, not the
production default. `connect`/`disconnect` are no-ops: a `BrokerPort`
instance authenticates outside this class's control entirely (the mock
needs no auth; Shoonya's own OAuth flow lives in `api.v1.shoonya`).

**Per-symbol callback dispatch, not one shared slot.** `subscribe_ticks`
used to store `on_tick` in a single `self._on_tick_external` attribute,
overwritten on every call — safe as long as every caller resubscribes with
its own same callback (true for `MarketDataIngestionService`), but
`PositionManager._ensure_symbol_subscribed` subscribes option-contract
symbols on this same shared instance with a different (no-op) callback the
moment any position opens, silently overwriting ingestion's real one —
live-confirmed 2026-08-13: NIFTY/BANKNIFTY `quote_ticks` went completely
silent, system-wide, from the instant of the first position of the
session, with no error anywhere (`get_latest_tick` kept working since the
in-memory cache updates unconditionally in `_handle_tick`, independent of
which external callback is registered — this is exactly why nothing looked
broken until directly diagnosed). Fixed by keying the callback by symbol
instead: the two real callers subscribe disjoint symbol sets (underlyings
vs option contracts), so this was never actually a multiple-subscribers-
per-symbol problem, just the wrong sharing granularity.
"""

from __future__ import annotations

import threading
from datetime import datetime

from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.base.contracts import DepthSnapshot, PriceCandle, Tick
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
        self._on_tick_by_symbol: dict[str, TickCallback] = {}
        self._on_depth_by_symbol: dict[str, DepthCallback] = {}

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
        with self._lock:
            for symbol in symbols:
                self._on_tick_by_symbol[symbol] = on_tick
                if on_depth is not None:
                    self._on_depth_by_symbol[symbol] = on_depth
        # Only pass a real depth dispatcher through when at least this call
        # actually wants depth -- passing self._handle_depth unconditionally
        # would signal "subscribe depth" to the broker even for a caller
        # that never asked for it, an unwanted behavior change from before
        # (which passed the caller's own on_depth, None included, straight
        # through).
        self._broker.subscribe_quotes(
            symbols,
            on_tick=self._handle_tick,
            on_depth=self._handle_depth if on_depth is not None else None,
        )

    def unsubscribe_ticks(self, symbols: list[str]) -> None:
        with self._lock:
            for symbol in symbols:
                self._on_tick_by_symbol.pop(symbol, None)
                self._on_depth_by_symbol.pop(symbol, None)
        self._broker.unsubscribe_quotes(symbols)

    def _handle_tick(self, tick: Tick) -> None:
        with self._lock:
            self._latest_ticks[tick.contract_symbol] = tick
            callback = self._on_tick_by_symbol.get(tick.contract_symbol)
        if callback is not None:
            callback(tick)

    def _handle_depth(self, depth: DepthSnapshot) -> None:
        with self._lock:
            callback = self._on_depth_by_symbol.get(depth.contract_symbol)
        if callback is not None:
            callback(depth)

    def get_latest_tick(self, symbol: str) -> Tick | None:
        with self._lock:
            return self._latest_ticks.get(symbol)

    def get_price_history(
        self, underlying: str, start: datetime, end: datetime, timeframe_seconds: int = 60
    ) -> list[PriceCandle]:
        return self._broker.get_price_history(underlying, start, end, timeframe_seconds)
