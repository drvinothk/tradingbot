"""`MarketDataScheduler` — driven via `run_once()` directly (same reasoning
`HealthCheckScheduler`'s own tests use it instead of the real threaded
loop: deterministic, no sleeping, no flakiness), with `current_phase` and
`get_market_data_provider` both monkeypatched so nothing here depends on
wall-clock time or a real provider.
"""

from __future__ import annotations

import app.modules.market_data.market_data_scheduler as scheduler_module
from app.modules.market_data.market_data_scheduler import (
    PRE_MARKET_HEALTH_CHECK_SECONDS,
    MarketDataScheduler,
)
from app.modules.market_data.market_hours import MarketPhase


class _FakeProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def connect(self) -> None:
        self.calls.append("connect")

    def disconnect(self) -> None:
        self.calls.append("disconnect")


def _scheduler_with_phase_sequence(monkeypatch, phases: list[MarketPhase], provider: _FakeProvider):
    it = iter(phases)
    monkeypatch.setattr(scheduler_module, "current_phase", lambda: next(it))
    monkeypatch.setattr(scheduler_module, "get_market_data_provider", lambda: provider)
    return MarketDataScheduler(tick_seconds=1.0)


def test_transition_into_pre_market_disconnects_then_connects(monkeypatch):
    provider = _FakeProvider()
    sched = _scheduler_with_phase_sequence(
        monkeypatch, [MarketPhase.PRE_MARKET], provider
    )
    sched.run_once()
    assert provider.calls == ["disconnect", "connect"]


def test_transition_into_closed_disconnects_only(monkeypatch):
    provider = _FakeProvider()
    sched = _scheduler_with_phase_sequence(monkeypatch, [MarketPhase.CLOSED], provider)
    sched.run_once()
    assert provider.calls == ["disconnect"]


def test_transition_into_active_market_takes_no_action(monkeypatch):
    provider = _FakeProvider()
    sched = _scheduler_with_phase_sequence(monkeypatch, [MarketPhase.ACTIVE_MARKET], provider)
    sched.run_once()
    assert provider.calls == []


def test_no_transition_takes_no_action_on_second_call(monkeypatch):
    provider = _FakeProvider()
    sched = _scheduler_with_phase_sequence(
        monkeypatch, [MarketPhase.ACTIVE_MARKET, MarketPhase.ACTIVE_MARKET], provider
    )
    sched.run_once()
    sched.run_once()
    assert provider.calls == []  # still no action -- active_market itself has none


def test_pre_market_to_active_market_transition_has_no_action(monkeypatch):
    """Only pre_market's *entry* connects; leaving it for active_market is
    a no-op transition -- the existing strategy-driven WS/REST-fallback
    machinery just continues working with whatever connection pre_market
    already established.
    """
    provider = _FakeProvider()
    sched = _scheduler_with_phase_sequence(
        monkeypatch, [MarketPhase.PRE_MARKET, MarketPhase.ACTIVE_MARKET], provider
    )
    sched.run_once()
    provider.calls.clear()
    sched.run_once()
    assert provider.calls == []


def test_pre_market_health_check_fires_after_interval(monkeypatch):
    """The first run_once() call both transitions (disconnect+connect) *and*
    accumulates its own tick (1.0s) toward the health-check interval, so
    reaching exactly PRE_MARKET_HEALTH_CHECK_SECONDS needs
    (ticks_needed - 1) more calls after that first one, not ticks_needed.
    """
    provider = _FakeProvider()
    tick_seconds = 1.0
    ticks_needed = int(PRE_MARKET_HEALTH_CHECK_SECONDS / tick_seconds)
    phases = [MarketPhase.PRE_MARKET] * (ticks_needed + 1)
    sched = _scheduler_with_phase_sequence(monkeypatch, phases, provider)
    sched._tick_seconds = tick_seconds  # noqa: SLF001

    sched.run_once()  # the initial transition itself: disconnect + connect; 1 tick accumulated
    provider.calls.clear()

    for _ in range(ticks_needed - 2):
        sched.run_once()
    assert provider.calls == []  # one tick short of due

    sched.run_once()
    assert provider.calls == ["connect"]  # the health check itself


def test_pre_market_health_check_does_not_fire_during_active_market(monkeypatch):
    provider = _FakeProvider()
    phases = [MarketPhase.ACTIVE_MARKET] * 20
    sched = _scheduler_with_phase_sequence(monkeypatch, phases, provider)
    sched._tick_seconds = 1.0  # noqa: SLF001

    for _ in range(20):
        sched.run_once()
    assert provider.calls == []
