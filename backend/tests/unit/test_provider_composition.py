"""`get_market_data_provider` must fail loud on an unrecognized
`MARKET_DATA_PROVIDER` value rather than silently falling back to
Shoonya/mock data — see that function's own docstring for the live
misconfiguration risk this guards against (2026-08-10).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.modules.market_data import provider_composition


@dataclass
class _FakeMarketDataSettings:
    provider: str
    allow_offhours_testing: bool = False
    failover_enabled: bool = False
    failover_backup_provider: str = "angel_one"
    failover_threshold_seconds: float = 5.0
    failover_recovery_stabilization_seconds: float = 90.0
    failover_backup_retry_seconds: float = 30.0


@dataclass
class _FakeSettings:
    market_data: _FakeMarketDataSettings
    # Unused by TrueDataProvider.__init__ itself (only read lazily inside
    # connect(), never called here) -- any placeholder satisfies the
    # attribute access in get_market_data_provider's "truedata" branch.
    truedata: object = None
    angel_one: object = None


def _settings_with_provider(name: str) -> _FakeSettings:
    return _FakeSettings(market_data=_FakeMarketDataSettings(provider=name))


def _settings_with_failover(
    provider: str, backup: str = "angel_one", *, enabled: bool = True
) -> _FakeSettings:
    # A real AngelOneSettings() (all-defaults) rather than another hand-rolled
    # fake -- matches test_angel_one_provider.py's own convention of using
    # the real settings class with literal/default values, not monkeypatched
    # env, and stays correct automatically as that class's fields evolve.
    from app.config.settings import AngelOneSettings

    return _FakeSettings(
        market_data=_FakeMarketDataSettings(
            provider=provider, failover_enabled=enabled, failover_backup_provider=backup
        ),
        angel_one=AngelOneSettings(),
    )


@pytest.fixture(autouse=True)
def _reset_provider_singleton():
    provider_composition.reset_for_tests()
    yield
    provider_composition.reset_for_tests()


def test_unrecognized_provider_name_raises(monkeypatch):
    # Not "truedata" -- that became a real, recognized provider on
    # 2026-08-10 (TrueDataProvider), so it can no longer stand in for "a
    # name nothing implements" here. Any name that will never be a real
    # provider works for this test's actual purpose.
    monkeypatch.setattr(
        provider_composition, "get_settings", lambda: _settings_with_provider("bloomberg")
    )

    with pytest.raises(ValueError, match="bloomberg"):
        provider_composition.get_market_data_provider()


def test_mock_provider_name_still_resolves(monkeypatch):
    monkeypatch.setattr(
        provider_composition, "get_settings", lambda: _settings_with_provider("mock")
    )

    provider = provider_composition.get_market_data_provider()

    assert provider is not None


def test_truedata_provider_name_resolves_without_needing_the_real_library(monkeypatch):
    """TrueDataProvider itself is import-safe with zero `truedata-ws`
    dependency (lazy import inside connect(), never called here) -- this
    proves the whole composition path actually constructs it, not just
    that the class exists in isolation.
    """
    monkeypatch.setattr(
        provider_composition, "get_settings", lambda: _settings_with_provider("truedata")
    )

    provider = provider_composition.get_market_data_provider()

    assert provider is not None


def test_failover_enabled_wraps_primary_and_backup(monkeypatch):
    """provider="truedata" (not "shoonya") deliberately, to avoid pulling in
    broker_adapter.composition's own singleton -- both TrueDataProvider and
    AngelOneMarketDataProvider are import-safe with no real network call at
    construction time, matching the existing truedata test's own reasoning.
    """
    from app.modules.market_data.providers.failover import FailoverMarketDataProvider
    from app.modules.market_data.providers.market_hours_gate import MarketHoursGatedProvider

    monkeypatch.setattr(
        provider_composition,
        "get_settings",
        lambda: _settings_with_failover("truedata", "angel_one"),
    )

    provider = provider_composition.get_market_data_provider()

    assert isinstance(provider, MarketHoursGatedProvider)
    inner = provider._inner  # noqa: SLF001 - intentionally reaching in to assert composition
    assert isinstance(inner, FailoverMarketDataProvider)
    assert inner.active_provider_name == "truedata"


def test_failover_backup_same_as_primary_raises(monkeypatch):
    monkeypatch.setattr(
        provider_composition,
        "get_settings",
        lambda: _settings_with_failover("angel_one", "angel_one"),
    )

    with pytest.raises(ValueError, match="must differ"):
        provider_composition.get_market_data_provider()


def test_failover_unrecognized_backup_raises(monkeypatch):
    # "shoonya" is a real, recognized *provider* -- just not (yet) a
    # supported failover *backup*, so this proves the two checks are
    # actually separate, not just re-checking _RECOGNIZED_PROVIDERS.
    monkeypatch.setattr(
        provider_composition,
        "get_settings",
        lambda: _settings_with_failover("truedata", "shoonya"),
    )

    with pytest.raises(ValueError, match="shoonya"):
        provider_composition.get_market_data_provider()


def test_failover_enabled_with_mock_provider_is_noop(monkeypatch):
    """"mock" has no real API/session for failover to protect -- same
    exclusion reasoning as MarketHoursGatedProvider's own mock exclusion.
    """
    from app.modules.market_data.providers.failover import FailoverMarketDataProvider
    from app.modules.market_data.providers.market_hours_gate import MarketHoursGatedProvider

    monkeypatch.setattr(
        provider_composition, "get_settings", lambda: _settings_with_failover("mock", "angel_one")
    )

    provider = provider_composition.get_market_data_provider()

    assert not isinstance(provider, FailoverMarketDataProvider)
    assert not isinstance(provider, MarketHoursGatedProvider)
