"""The abstract interface every live-tick source implements — the market-data
counterpart to `broker_adapter/base/broker_port.py`, deliberately separate
from it (see `market_data/provider_composition.py`'s own docstring for why).
`MarketDataIngestionService` and `PositionManager` depend only on this
interface, never on a concrete provider, so a future feed swap (Angel One
today; something else if it doesn't work out) touches one adapter, not the
strategy/execution/risk layers.

Synchronous by design, matching `BrokerPort`'s own documented rationale: this
codebase's core is deliberately synchronous (sync SQLAlchemy sessions,
`threading`-based background workers throughout — see `ShoonyaWSClient`,
`PositionManager`, `HealthCheckScheduler`). Streaming ticks use a callback
registered via `subscribe_ticks`, run on the implementation's own background
thread, rather than an async generator — the same shape works whether the
implementation is a real WebSocket client or a replay loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime

from app.modules.broker_adapter.base.contracts import DepthSnapshot, PriceCandle, Tick

TickCallback = Callable[[Tick], None]
DepthCallback = Callable[[DepthSnapshot], None]


class BaseMarketDataProvider(ABC):
    """Every method here must be implemented by every provider (Angel One,
    the Shoonya/mock shims). Method names match the shape originally
    requested — `connect`/`disconnect`/`subscribe_ticks`/`unsubscribe_ticks`
    plus a tick callback — with two additions justified by real call sites
    this system already has (see `get_latest_tick`/`get_price_history` below).
    """

    @abstractmethod
    def connect(self) -> None:
        """Authenticate and prepare the provider to stream — for Angel One,
        the REST login (`loginByPassword`) that yields the feed/JWT tokens
        the WebSocket needs. Idempotent: calling it again while already
        connected must not re-authenticate or open a second connection.
        """

    @abstractmethod
    def disconnect(self) -> None:
        """Tears down the connection. Same "stopped must mean no more
        callbacks fire, not just asked to stop" discipline this codebase
        already requires of `BrokerPort.unsubscribe_quotes` — a caller that
        tears down its own state right after this returns must not race an
        in-flight tick callback.
        """

    @abstractmethod
    def subscribe_ticks(
        self,
        symbols: list[str],
        on_tick: TickCallback,
        on_depth: DepthCallback | None = None,
    ) -> None:
        """Starts streaming `symbols` (this system's own DB `Instrument`/
        `OptionContract.symbol` values, never a provider-native symbol —
        each concrete provider is responsible for its own symbol/token
        resolution before subscribing on the wire). Idempotent per symbol:
        re-subscribing an already-subscribed symbol is a no-op change.

        `on_depth` is optional (not part of the originally requested method
        list) — added so `MarketDataIngestionService`'s existing
        `DepthSnapshot` persistence (which `strike_ranking.engine`'s
        liquidity scoring already reads) doesn't silently regress for a
        provider that swaps in here, matching `BrokerPort.subscribe_quotes`'s
        identical shape.
        """

    @abstractmethod
    def unsubscribe_ticks(self, symbols: list[str]) -> None: ...

    @abstractmethod
    def get_latest_tick(self, symbol: str) -> Tick | None:
        """Synchronous "last known price" read, backed by an in-memory cache
        the provider updates on every tick. `None` if nothing has arrived
        for this symbol yet. This is what lets `PositionManager`'s stop/
        target/trail checks price a position every poll cycle without
        blocking on the streaming callback.
        """

    @abstractmethod
    def get_price_history(
        self, underlying: str, start: datetime, end: datetime, timeframe_seconds: int = 60
    ) -> list[PriceCandle]:
        """Real, already-completed OHLCV bars — preserves
        `MarketDataIngestionService`'s existing WS-health-watchdog ->
        REST-polling-fallback resilience feature for whichever provider is
        primary. Same "a broker's own 'latest' candle can be the one still
        forming" caveat as `BrokerPort.get_price_history` — callers must not
        assume every returned row's bucket has actually closed.
        """

    def is_ready(self) -> bool:
        """Cheap, synchronous, no-network-call readiness probe — added
        2026-08-25 so `FailoverMarketDataProvider` can check *before*
        attempting to subscribe a backup leg whether it even has live
        credentials, rather than only discovering that via a failed
        `subscribe_ticks()` call (see that class's own `_ensure_backup_
        subscribed` docstring). Deliberately a concrete method with a
        `True` default, not abstract -- every provider that can
        self-authenticate (Angel One, TrueData) or shares an
        already-managed connection (Shoonya/mock via
        `BrokerPortMarketDataAdapter`) is "always ready" by construction,
        so adding this as abstract would force a no-op override onto every
        existing provider for zero behavioral gain. Only a provider whose
        auth is a one-time human browser action with no backend-triggerable
        retry (Alice Blue) needs to override this with a real check.
        """
        return True
