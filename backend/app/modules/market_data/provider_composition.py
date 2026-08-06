"""The composition root for `BaseMarketDataProvider` — the market-data
counterpart to `broker_adapter.composition` (`BrokerPort`), and deliberately
a *separate* module-level singleton from it. The whole point of adding Angel
One is that market data and execution must be able to point at two entirely
different brokers at once (Angel One for ticks, Shoonya for orders) — a
single shared resolver would defeat that by construction, the same reasoning
`broker_adapter.composition`'s own docstring gives for why `get_broker()`
and `get_execution_broker()` are already two separate slots on that side.

`get_market_data_provider()` reads `Settings.market_data.provider`
(`"angel_one"` | `"shoonya"` | `"mock"`, default `"mock"` so every existing
test and local dev run behaves exactly as before this existed, unless
explicitly opted in) and lazily constructs the matching provider.
`"shoonya"` and `"mock"` both resolve to `BrokerPortMarketDataAdapter`
wrapping `broker_adapter.composition.get_broker()`'s own current value —
that function already dynamically resolves to the mock by default or a
connected `ShoonyaBrokerAdapter` once OAuth completes, so duplicating that
same dynamic choice into a second, independent static slot here would just
be a second place for the two to disagree. Only `"angel_one"` gets a
genuinely distinct instance.
"""

from __future__ import annotations

from app.config.settings import get_settings
from app.modules.broker_adapter.composition import get_broker
from app.modules.market_data.providers.angel_one import AngelOneMarketDataProvider
from app.modules.market_data.providers.base import BaseMarketDataProvider
from app.modules.market_data.providers.broker_port_shim import BrokerPortMarketDataAdapter
from app.modules.market_data.providers.market_hours_gate import MarketHoursGatedProvider
from app.modules.market_data.scrip_master import ScripMasterService

_provider: BaseMarketDataProvider | None = None
_scrip_master: ScripMasterService | None = None


def get_scrip_master() -> ScripMasterService:
    """Shared instance so `app.main`'s startup sync and
    `AngelOneMarketDataProvider`'s subscribe/history calls read the same
    in-memory symbol/token index rather than each rebuilding their own.
    """
    global _scrip_master
    if _scrip_master is None:
        settings = get_settings().angel_one
        _scrip_master = ScripMasterService(
            primary_url=settings.scrip_master_url,
            fallback_url=settings.scrip_master_url_fallback,
        )
    return _scrip_master


def get_market_data_provider() -> BaseMarketDataProvider:
    """`"mock"` is deliberately never wrapped by `MarketHoursGatedProvider`
    (`market_data/market_hours.py`) — it's the safe default every test and
    local dev run resolves to unless a real provider is explicitly
    configured, and a mock adapter has no real API/rate-limit/session
    concept for a market-hours schedule to protect in the first place.
    `"angel_one"` and `"shoonya"` both get the gate, uniformly, so the
    08:30-16:00 IST policy stays in force regardless of which real
    provider is selected — the whole point of wrapping at this
    composition-root level instead of inside one concrete provider.
    """
    global _provider
    if _provider is not None:
        return _provider

    settings = get_settings()
    provider_name = settings.market_data.provider
    if provider_name == "angel_one":
        inner: BaseMarketDataProvider = AngelOneMarketDataProvider(
            settings.angel_one, get_scrip_master()
        )
    else:
        inner = BrokerPortMarketDataAdapter(get_broker())

    if provider_name == "mock":
        _provider = inner
    else:
        _provider = MarketHoursGatedProvider(
            inner, allow_offhours=settings.market_data.allow_offhours_testing
        )
    return _provider


def set_market_data_provider(provider: BaseMarketDataProvider | None) -> None:
    """Test/composition-root hook, same reasoning as
    `broker_adapter.composition.set_broker` — lets tests inject a fake
    provider, and lets a future reconnect flow replace the live one.
    """
    global _provider
    previous = _provider
    if previous is not None:
        close = getattr(previous, "close", None)
        if callable(close):
            close()
    _provider = provider


def reset_for_tests() -> None:
    global _provider, _scrip_master
    if _provider is not None:
        close = getattr(_provider, "close", None)
        if callable(close):
            close()
    _provider = None
    _scrip_master = None
