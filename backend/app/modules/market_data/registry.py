"""Process-wide registry for the single running `MarketDataIngestionService`
stream — not one per underlying instrument. `BrokerPort.subscribe_quotes`'s
own contract (see its docstring) is a *single shared connection* ("Shoonya
only supports one connection per session"); `MockBrokerAdapter` reflects that
literally — one `on_tick`/`on_depth` callback slot, clobbered by whichever
caller subscribed last. A `MarketDataIngestionService` instance per
instrument (this module's first cut, before this was caught in Phase 4's own
manual QC) would each fight over that one slot, silently dropping every
underlying's ticks except whichever service subscribed most recently.

The fix: one lazily-constructed service, shared the same way `get_broker()`
shares one broker instance — `ensure_ingestion_running` just extends its
subscription (`MarketDataIngestionService.start` already accumulates
`_symbol_map` and re-subscribing the same callback is a no-op change to the
mock's single slot) when a new underlying needs streaming, idempotent per
symbol so several concurrent strategy runs on the same underlying, or on
different underlyings, all share the one real connection.
"""

from __future__ import annotations

from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.composition import get_broker
from app.modules.market_data.indicators import IndicatorEngine
from app.modules.market_data.ingestion import MarketDataIngestionService

_service: MarketDataIngestionService | None = None
_subscribed_symbols: set[str] = set()


def ensure_ingestion_running(
    underlying_symbol: str,
    broker: BrokerPort | None = None,
) -> MarketDataIngestionService:
    global _service

    if _service is None:
        _service = MarketDataIngestionService(
            broker or get_broker(), indicator_engine=IndicatorEngine()
        )

    if underlying_symbol not in _subscribed_symbols:
        _service.start([underlying_symbol])
        _subscribed_symbols.add(underlying_symbol)

    return _service


def reset() -> None:
    """Test/composition-root hook, same reasoning as
    `broker_adapter.composition.set_broker` — lets tests (and a future
    `app.main` shutdown hook) reset this module-level singleton instead of
    leaking a service bound to a previous test's broker/engine across runs.
    """
    global _service
    _service = None
    _subscribed_symbols.clear()
