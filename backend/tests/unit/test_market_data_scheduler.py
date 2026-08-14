"""`MarketDataScheduler` — driven via `run_once()` directly (same reasoning
`HealthCheckScheduler`'s own tests use it instead of the real threaded
loop: deterministic, no sleeping, no flakiness), with `current_phase` and
`get_market_data_provider` both monkeypatched so nothing here depends on
wall-clock time or a real provider. `ensure_ingestion_running`/
`reset_daily_indicators` (both imported locally inside `_handle_transition`)
are patched at their source module, `market_data.registry`, matching
`test_startup_recovery.py`'s established pattern for this exact
local-import shape.
"""

from __future__ import annotations

import app.modules.market_data.market_data_scheduler as scheduler_module
import app.modules.market_data.registry as registry_module
from app.modules.market_data.market_data_scheduler import (
    PRE_MARKET_HEALTH_CHECK_SECONDS,
    MarketDataScheduler,
)
from app.modules.market_data.market_hours import TRADABLE_UNDERLYINGS, MarketPhase


class _FakeProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def connect(self) -> None:
        self.calls.append("connect")

    def disconnect(self) -> None:
        self.calls.append("disconnect")


def _scheduler_with_phase_sequence(
    monkeypatch,
    phases: list[MarketPhase],
    provider: _FakeProvider,
    subscribe_calls: list[str] | None = None,
    reset_calls: list[None] | None = None,
):
    it = iter(phases)
    monkeypatch.setattr(scheduler_module, "current_phase", lambda: next(it))
    monkeypatch.setattr(scheduler_module, "get_market_data_provider", lambda: provider)
    calls = subscribe_calls if subscribe_calls is not None else []
    monkeypatch.setattr(
        registry_module, "ensure_ingestion_running", lambda symbol: calls.append(symbol)
    )
    resets = reset_calls if reset_calls is not None else []
    monkeypatch.setattr(
        registry_module, "reset_daily_indicators", lambda: resets.append(None)
    )
    return MarketDataScheduler(tick_seconds=1.0)


def test_transition_into_pre_market_disconnects_connects_subscribes_and_resets_vwap(
    monkeypatch,
):
    provider = _FakeProvider()
    subscribe_calls: list[str] = []
    reset_calls: list[None] = []
    sched = _scheduler_with_phase_sequence(
        monkeypatch, [MarketPhase.PRE_MARKET], provider, subscribe_calls, reset_calls
    )
    sched.run_once()
    assert provider.calls == ["disconnect", "connect"]
    assert subscribe_calls == list(TRADABLE_UNDERLYINGS)
    assert len(reset_calls) == 1


def test_fresh_startup_into_active_market_does_not_reset_vwap(monkeypatch):
    """A mid-day process (re)start shouldn't reset VWAP -- there's no new
    trading day to reset for, and doing so would throw away whatever VWAP
    state (if any) a still-running IndicatorEngine already has for today.
    """
    provider = _FakeProvider()
    reset_calls: list[None] = []
    sched = _scheduler_with_phase_sequence(
        monkeypatch, [MarketPhase.ACTIVE_MARKET], provider, reset_calls=reset_calls
    )
    sched.run_once()
    assert reset_calls == []


def test_transition_into_closed_disconnects_only(monkeypatch):
    provider = _FakeProvider()
    sched = _scheduler_with_phase_sequence(monkeypatch, [MarketPhase.CLOSED], provider)
    sched.run_once()
    assert provider.calls == ["disconnect"]


def test_fresh_startup_into_active_market_connects_and_subscribes(monkeypatch):
    """A process started (or restarted) mid-day, already past pre_market --
    from_phase is None on this, the very first observed transition. Today
    this used to be a true no-op; now it must connect + subscribe, since
    otherwise a restart during market hours would never auto-start
    ingestion until a strategy is manually started.
    """
    provider = _FakeProvider()
    subscribe_calls: list[str] = []
    sched = _scheduler_with_phase_sequence(
        monkeypatch, [MarketPhase.ACTIVE_MARKET], provider, subscribe_calls
    )
    sched.run_once()
    assert provider.calls == ["connect"]
    assert subscribe_calls == list(TRADABLE_UNDERLYINGS)


def test_fresh_startup_into_active_market_defers_subscription_when_shoonya_not_connected(
    monkeypatch,
):
    """2026-08-14 regression: a restart during market hours reaches this
    transition within one tick, independent of and before
    `app.main._resume_strategy_runners`'s own reconnect-aware guard --
    without this, `ensure_ingestion_running` starts a real background
    thread writing fabricated prices to price_bars/quote_ticks in the
    window between a restart and a human reconnecting. `connect()` itself
    still fires (harmless — a no-op for the real Shoonya-wrapping adapter);
    only the subscribe call is deferred.
    """
    provider = _FakeProvider()
    subscribe_calls: list[str] = []
    sched = _scheduler_with_phase_sequence(
        monkeypatch, [MarketPhase.ACTIVE_MARKET], provider, subscribe_calls
    )
    monkeypatch.setattr(scheduler_module, "is_shoonya_market_data_ready", lambda: False)

    sched.run_once()

    assert provider.calls == ["connect"]
    assert subscribe_calls == []


def test_pre_market_defers_subscription_when_shoonya_not_connected(monkeypatch):
    provider = _FakeProvider()
    subscribe_calls: list[str] = []
    reset_calls: list[None] = []
    sched = _scheduler_with_phase_sequence(
        monkeypatch, [MarketPhase.PRE_MARKET], provider, subscribe_calls, reset_calls
    )
    monkeypatch.setattr(scheduler_module, "is_shoonya_market_data_ready", lambda: False)

    sched.run_once()

    assert provider.calls == ["disconnect", "connect"]
    assert subscribe_calls == []
    assert len(reset_calls) == 1  # VWAP reset is unaffected -- unrelated to the broker guard


def test_no_transition_takes_no_action_on_second_call(monkeypatch):
    provider = _FakeProvider()
    subscribe_calls: list[str] = []
    sched = _scheduler_with_phase_sequence(
        monkeypatch,
        [MarketPhase.ACTIVE_MARKET, MarketPhase.ACTIVE_MARKET],
        provider,
        subscribe_calls,
    )
    sched.run_once()
    provider.calls.clear()
    subscribe_calls.clear()
    sched.run_once()
    assert provider.calls == []
    assert subscribe_calls == []


def test_pre_market_to_active_market_transition_has_no_action(monkeypatch):
    """Only pre_market's *entry* connects+subscribes; the normal daily
    pre_market -> active_market transition stays a true no-op -- the
    existing strategy-driven WS/REST-fallback machinery just continues
    working with whatever connection/subscription pre_market already
    established, and nothing is double-subscribed or double-connected.
    """
    provider = _FakeProvider()
    subscribe_calls: list[str] = []
    sched = _scheduler_with_phase_sequence(
        monkeypatch,
        [MarketPhase.PRE_MARKET, MarketPhase.ACTIVE_MARKET],
        provider,
        subscribe_calls,
    )
    sched.run_once()
    provider.calls.clear()
    subscribe_calls.clear()
    sched.run_once()
    assert provider.calls == []
    assert subscribe_calls == []


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

    sched.run_once()  # the initial from_phase=None -> active_market transition itself
    provider.calls.clear()

    for _ in range(19):
        sched.run_once()
    assert provider.calls == []
