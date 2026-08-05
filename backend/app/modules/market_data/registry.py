"""Process-wide registry for the single running `MarketDataIngestionService`
stream — not one per underlying instrument. `BaseMarketDataProvider.
subscribe_ticks`'s own contract (see its docstring, and
`BrokerPort.subscribe_quotes`'s identical one) is a *single shared
connection* ("Shoonya only supports one connection per session", and Angel
One's SmartStream is the same shape); `MockBrokerAdapter` reflects that
literally — one `on_tick`/`on_depth` callback slot, clobbered by whichever
caller subscribed last. A `MarketDataIngestionService` instance per
instrument (this module's first cut, before this was caught in Phase 4's own
manual QC) would each fight over that one slot, silently dropping every
underlying's ticks except whichever service subscribed most recently.

The fix: one lazily-constructed service, shared the same way
`market_data.provider_composition.get_market_data_provider()` shares one
provider instance — `ensure_ingestion_running` just extends its subscription
(`MarketDataIngestionService.start` already accumulates `_symbol_map` and
re-subscribing the same callback is a no-op change to a single-slot
provider) when a new underlying needs streaming, idempotent per symbol so
several concurrent strategy runs on the same underlying, or on different
underlyings, all share the one real connection.
"""

from __future__ import annotations

from app.modules.market_data.indicators import IndicatorEngine
from app.modules.market_data.ingestion import MarketDataIngestionService
from app.modules.market_data.provider_composition import get_market_data_provider
from app.modules.market_data.providers.base import BaseMarketDataProvider

_service: MarketDataIngestionService | None = None
_subscribed_symbols: set[str] = set()


def ensure_ingestion_running(
    underlying_symbol: str,
    provider: BaseMarketDataProvider | None = None,
) -> MarketDataIngestionService:
    global _service

    if _service is None:
        _service = MarketDataIngestionService(
            provider or get_market_data_provider(), indicator_engine=IndicatorEngine()
        )

    if underlying_symbol not in _subscribed_symbols:
        _service.start([underlying_symbol])
        _subscribed_symbols.add(underlying_symbol)

    return _service


def unsubscribe_symbol(symbol: str) -> None:
    """The close-position half of `PositionManager`'s per-position
    subscription lifecycle (see its own docstring) — a symbol only ever
    gets subscribed here when a position opens on it (option contracts
    aren't part of a strategy's own underlying subscription), so it's
    unsubscribed here when that position closes. Safe to call for a symbol
    that was never subscribed (no-op) or while `_service` doesn't exist yet
    (nothing to unsubscribe from).
    """
    if _service is None or symbol not in _subscribed_symbols:
        return
    _service.stop([symbol])
    _subscribed_symbols.discard(symbol)


def reset() -> None:
    """Test/composition-root hook, same reasoning as
    `broker_adapter.composition.set_broker` — lets tests (and a future
    `app.main` shutdown hook) reset this module-level singleton instead of
    leaking a service bound to a previous test's broker/engine across runs.
    """
    global _service
    _service = None
    _subscribed_symbols.clear()
