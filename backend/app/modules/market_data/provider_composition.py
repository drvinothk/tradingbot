"""The composition root for `BaseMarketDataProvider` — the market-data
counterpart to `broker_adapter.composition` (`BrokerPort`), and deliberately
a *separate* module-level singleton from it. The whole point of adding Angel
One is that market data and execution must be able to point at two entirely
different brokers at once (Angel One for ticks, Shoonya for orders) — a
single shared resolver would defeat that by construction, the same reasoning
`broker_adapter.composition`'s own docstring gives for why `get_broker()`
and `get_execution_broker()` are already two separate slots on that side.

`get_market_data_provider()` reads `Settings.market_data.provider`
(`"angel_one"` | `"shoonya"` | `"truedata"` | `"mock"`, default `"mock"` so
every existing test and local dev run behaves exactly as before this
existed, unless explicitly opted in) and lazily constructs the matching
provider. `"shoonya"` and `"mock"` both resolve to
`BrokerPortMarketDataAdapter` wrapping `broker_adapter.composition
.get_broker()`'s own current value — that function already dynamically
resolves to the mock by default or a connected `ShoonyaBrokerAdapter` once
OAuth completes, so duplicating that same dynamic choice into a second,
independent static slot here would just be a second place for the two to
disagree. `"angel_one"` and `"truedata"` each get a genuinely distinct
instance. `"truedata"` is untested against a live account as of
2026-08-10 — see `TrueDataProvider`'s own docstring for exactly what's
confirmed vs. still inferred.

An unrecognized `provider` value raises `ValueError` rather than falling
through to the `"shoonya"`/`"mock"` branch — added 2026-08-10, before that a
typo or a not-yet-implemented name (e.g. setting `MARKET_DATA_PROVIDER=
truedata` before a real `TrueDataProvider` class exists) would silently
serve Shoonya- or mock-sourced data with no indication anything was
misconfigured. A market-data source is exactly the kind of dependency that
should fail loud at first use, not quietly substitute a different broker's
data for the one actually requested.
"""

from __future__ import annotations

import logging

from app.config.settings import get_settings
from app.core.db.session import session_scope
from app.domain.ops.models import MarketDataProviderPreference
from app.modules.broker_adapter.composition import get_broker, is_shoonya_configured
from app.modules.market_data.providers.angel_one import AngelOneMarketDataProvider
from app.modules.market_data.providers.base import BaseMarketDataProvider
from app.modules.market_data.providers.broker_port_shim import BrokerPortMarketDataAdapter
from app.modules.market_data.providers.failover import FailoverMarketDataProvider
from app.modules.market_data.providers.market_hours_gate import MarketHoursGatedProvider
from app.modules.market_data.providers.truedata_provider import TrueDataProvider
from app.modules.market_data.scrip_master import ScripMasterService

logger = logging.getLogger("app.market_data.provider_composition")

_RECOGNIZED_PROVIDERS = ("angel_one", "shoonya", "truedata", "mock")
# Backup legs supported for MARKET_DATA_FAILOVER_BACKUP_PROVIDER today --
# narrower than _RECOGNIZED_PROVIDERS on purpose: TrueData is a deliberately
# deferred scope call (not yet live-tested as a failover backup at all), and
# "mock"/self-as-backup are never valid regardless of provider.
_RECOGNIZED_FAILOVER_BACKUPS = ("angel_one",)

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


def _build_provider(name: str, settings: object) -> BaseMarketDataProvider:
    """Constructs a single raw provider by name -- shared by primary
    selection and, when failover is enabled, backup selection below, so
    the two don't duplicate this if/elif chain.
    """
    if name == "angel_one":
        return AngelOneMarketDataProvider(settings.angel_one, get_scrip_master())  # type: ignore[attr-defined]
    if name == "truedata":
        return TrueDataProvider(settings.truedata)  # type: ignore[attr-defined]
    return BrokerPortMarketDataAdapter(get_broker())


def _seed_manual_override(failover: FailoverMarketDataProvider) -> None:
    """Ops-Hardening Phase 4: applies any persisted `MarketDataProviderPreference`
    as the initial manual override at construction time, so a preference set
    before a restart/reconnect is honored immediately rather than only ever
    taking effect via a live PATCH call on an already-running singleton.

    This composition root is a process-wide lazy singleton with no
    per-request workspace context -- takes the first preference row found,
    which is correct for this system's actual single-workspace usage (same
    simplification `market_data.market_hours.TRADABLE_UNDERLYINGS`'s own
    docstring already makes explicitly for the identical reason).

    Deliberately never raises: a DB hiccup, or a stale preference pointing
    at a symbol set that isn't subscribed yet, must never prevent
    market-data ingestion from starting at all -- falls back to no override
    (normal automatic failover) and logs a warning instead.

    `session_scope` is a module-level import (not local), specifically so
    tests can monkeypatch `provider_composition.session_scope` to a fake
    that never touches the real engine -- the module-level `_reset_broker_
    singleton` fixture in conftest.py does exactly this for the whole test
    suite, the same "never let a composition-root helper default to the
    production DB inside a test" discipline this project's own CLAUDE.md
    already documents hitting as a real incident once before.
    """
    try:
        with session_scope() as db:
            pref = db.query(MarketDataProviderPreference).first()
            override = pref.active_provider if pref is not None else None
        if override is not None:
            failover.set_manual_override(override)
    except Exception:  # noqa: BLE001 - best-effort seeding, never blocks startup
        logger.warning(
            "Could not apply a persisted market-data provider preference at startup "
            "-- continuing with normal automatic failover.",
            exc_info=True,
        )


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

    When `market_data.failover_enabled` is set (and provider isn't
    `"mock"`), the primary is wrapped in `FailoverMarketDataProvider`
    against a second, independently-constructed backup provider *before*
    the market-hours gate is applied — so the gate sits outermost and
    covers both legs uniformly, matching the reasoning above. See
    `failover.py`'s own docstring for the 5s-trip/90s-anti-flap state
    machine this wraps in.
    """
    global _provider
    if _provider is not None:
        return _provider

    settings = get_settings()
    provider_name = settings.market_data.provider
    if provider_name not in _RECOGNIZED_PROVIDERS:
        raise ValueError(
            f"Unrecognized MARKET_DATA_PROVIDER={provider_name!r} — must be one of "
            f"{_RECOGNIZED_PROVIDERS}. Refusing to silently fall back to Shoonya/mock "
            "market data for an unimplemented or misspelled provider name."
        )

    inner = _build_provider(provider_name, settings)

    if provider_name != "mock" and settings.market_data.failover_enabled:
        backup_name = settings.market_data.failover_backup_provider
        if backup_name not in _RECOGNIZED_FAILOVER_BACKUPS:
            raise ValueError(
                f"Unrecognized MARKET_DATA_FAILOVER_BACKUP_PROVIDER={backup_name!r} — "
                f"must be one of {_RECOGNIZED_FAILOVER_BACKUPS}. Refusing to silently run "
                "with failover configured-but-inert for an unimplemented or misspelled "
                "backup provider name."
            )
        if backup_name == provider_name:
            raise ValueError(
                f"MARKET_DATA_FAILOVER_BACKUP_PROVIDER={backup_name!r} must differ from "
                f"MARKET_DATA_PROVIDER={provider_name!r} — a provider can't be its own "
                "failover backup."
            )
        inner = FailoverMarketDataProvider(
            primary=inner,
            backup=_build_provider(backup_name, settings),
            primary_name=provider_name,
            backup_name=backup_name,
            failover_threshold_seconds=settings.market_data.failover_threshold_seconds,
            recovery_stabilization_seconds=(
                settings.market_data.failover_recovery_stabilization_seconds
            ),
            backup_retry_seconds=settings.market_data.failover_backup_retry_seconds,
        )
        _seed_manual_override(inner)

    if provider_name == "mock":
        _provider = inner
    else:
        _provider = MarketHoursGatedProvider(
            inner, allow_offhours=settings.market_data.allow_offhours_testing
        )
    return _provider


def is_shoonya_market_data_ready() -> bool:
    """False only when `MARKET_DATA_PROVIDER=shoonya` and no real broker is
    connected yet — `get_market_data_provider()` would still wrap whatever
    `get_broker()` currently resolves to, which is the mock until a human
    completes OAuth (`broker_adapter.composition.get_broker`'s own docstring).
    `"angel_one"`/`"truedata"` construct independently of `get_broker()`, and
    `"mock"` returning the mock is correct by definition, so both are always
    ready regardless of this flag.

    2026-08-14: added after a live incident where `_resume_strategy_runners`
    (`app.main`) called `ensure_ingestion_running` before any reconnect had
    happened, permanently caching this module's `_provider` singleton
    wrapping the mock — see `market_data.registry.reset_for_reconnect`'s own
    docstring for the full mechanism. Callers use this to defer market-data work
    entirely rather than let it silently run against fabricated prices.
    """
    return get_settings().market_data.provider != "shoonya" or is_shoonya_configured()


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
