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


@dataclass
class _FakeSettings:
    market_data: _FakeMarketDataSettings
    # Unused by TrueDataProvider.__init__ itself (only read lazily inside
    # connect(), never called here) -- any placeholder satisfies the
    # attribute access in get_market_data_provider's "truedata" branch.
    truedata: object = None


def _settings_with_provider(name: str) -> _FakeSettings:
    return _FakeSettings(market_data=_FakeMarketDataSettings(provider=name))


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
