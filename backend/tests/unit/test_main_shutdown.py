"""Confirms `app.main.lifespan`'s shutdown sequence actually disconnects the
market-data provider — the real gap found during a 2026-08-11 live-WS
troubleshooting audit: `stop_market_data_scheduler()` alone only stops that
class's own polling thread, it never tears down the provider's own
connection, so every prior app restart let any open WS connection die
abruptly instead of closing cleanly.

Drives the real `lifespan` async context manager end to end (not a
reimplementation of its logic) with every DB-touching / real-background-
thread-starting dependency replaced by a no-op fake — same reasoning
`test_api_auth_and_sessions.py`'s own docstring gives for why its
`TestClient` is deliberately *not* used as a context manager (that would
trigger this same expensive real lifespan for every API test). This file is
the one place that real lifespan sequence is actually exercised.
"""

from __future__ import annotations

import asyncio

import pytest

import app.main as main_module
from app.core import locking as locking_module
from app.modules.execution_engine.paper import registry as position_manager_registry
from app.modules.market_data import market_data_scheduler, provider_composition
from app.modules.market_data import scrip_master_scheduler as scrip_master_scheduler_module
from app.modules.scheduler import health_check as health_check_module


class _FakeSingletonConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeProvider:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture
def _patched_lifespan_dependencies(monkeypatch):
    """Neutralizes every DB/real-background-thread dependency `lifespan`
    touches, so it can run end to end against nothing but fakes -- see
    module docstring. Returns the fakes the test itself needs to assert on.
    """
    fake_singleton_connection = _FakeSingletonConnection()
    fake_provider = _FakeProvider()

    monkeypatch.setattr(main_module, "_run_startup_health_checks", lambda: None)
    monkeypatch.setattr(
        main_module, "_acquire_process_singleton_lock", lambda: fake_singleton_connection
    )
    monkeypatch.setattr(main_module, "_sync_mock_instrument_universe", lambda: None)
    monkeypatch.setattr(main_module, "_sync_angel_one_scrip_master", lambda: None)
    monkeypatch.setattr(main_module, "_run_startup_recovery_check", lambda: None)
    monkeypatch.setattr(main_module, "_resume_strategy_runners", lambda: None)

    monkeypatch.setattr(locking_module, "release_advisory_lock", lambda *a, **kw: None)
    monkeypatch.setattr(
        position_manager_registry, "stop_all", lambda: None
    )
    monkeypatch.setattr(
        health_check_module, "ensure_health_check_scheduler_running", lambda: None
    )
    monkeypatch.setattr(health_check_module, "stop_health_check_scheduler", lambda: None)
    monkeypatch.setattr(
        market_data_scheduler, "ensure_market_data_scheduler_running", lambda: None
    )
    monkeypatch.setattr(market_data_scheduler, "stop_market_data_scheduler", lambda: None)
    monkeypatch.setattr(
        scrip_master_scheduler_module, "stop_scrip_master_refresh_scheduler", lambda: None
    )
    monkeypatch.setattr(provider_composition, "get_market_data_provider", lambda: fake_provider)

    return fake_singleton_connection, fake_provider


def test_shutdown_closes_the_market_data_provider(_patched_lifespan_dependencies):
    _fake_singleton_connection, fake_provider = _patched_lifespan_dependencies

    async def _drive() -> None:
        async with main_module.lifespan(main_module.app):
            pass  # entering runs startup; leaving the block runs shutdown

    asyncio.run(_drive())

    assert fake_provider.close_calls == 1


def test_shutdown_does_not_raise_when_the_provider_has_no_close_method(
    _patched_lifespan_dependencies, monkeypatch
):
    """The `mock` provider (and any future one) may not define `close()` at
    all -- the `getattr(..., "close", None)` guard must make that a
    harmless no-op, not a crash during shutdown.
    """

    class _ProviderWithoutClose:
        pass

    monkeypatch.setattr(
        provider_composition, "get_market_data_provider", lambda: _ProviderWithoutClose()
    )

    async def _drive() -> None:
        async with main_module.lifespan(main_module.app):
            pass

    asyncio.run(_drive())  # must not raise
