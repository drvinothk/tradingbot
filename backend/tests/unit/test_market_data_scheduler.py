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
from app.modules.market_data.market_hours import (
    ENV_METRIC_SYMBOLS,
    TRADABLE_UNDERLYINGS,
    MarketPhase,
)


class _FakeProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def connect(self) -> None:
        self.calls.append("connect")

    def disconnect(self) -> None:
        self.calls.append("disconnect")


class _FakeAlertDB:
    """Minimal stand-in for the `Session` `_alert_if_no_session_anywhere`
    reads through -- `query(TradingSession.workspace_id).filter(...)
    .distinct().all()`, returning `(workspace_id,)` tuples the same shape a
    real single-column query would. Matches `test_failover_provider.py`'s
    own `_FakeAlertDB` pattern (a callable `alert_session_factory` that
    returns this instance, which is itself the context manager)."""

    def __init__(self, workspace_ids: list) -> None:
        self._workspace_ids = workspace_ids

    def query(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def distinct(self):
        return self

    def all(self):
        return [(ws_id,) for ws_id in self._workspace_ids]

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def _scheduler_with_phase_sequence(
    monkeypatch,
    phases: list[MarketPhase],
    provider: _FakeProvider,
    subscribe_calls: list[str] | None = None,
    reset_calls: list[None] | None = None,
    resubscribe_calls: list[None] | None = None,
):
    it = iter(phases)
    monkeypatch.setattr(scheduler_module, "current_phase", lambda: next(it))
    monkeypatch.setattr(scheduler_module, "get_market_data_provider", lambda: provider)
    # Weekend rest mode: default every test to "awake" so the phase logic
    # runs regardless of the real day of week. The dormancy tests below
    # override this back to False explicitly.
    monkeypatch.setattr(scheduler_module.weekend_rest, "is_system_awake", lambda: True)
    calls = subscribe_calls if subscribe_calls is not None else []
    monkeypatch.setattr(
        registry_module, "ensure_ingestion_running", lambda symbol: calls.append(symbol)
    )
    resets = reset_calls if reset_calls is not None else []
    monkeypatch.setattr(
        registry_module, "reset_daily_indicators", lambda: resets.append(None)
    )
    resubscribes = resubscribe_calls if resubscribe_calls is not None else []
    monkeypatch.setattr(
        registry_module, "reset_subscriptions_for_new_day", lambda: resubscribes.append(None)
    )
    return MarketDataScheduler(tick_seconds=1.0)


def test_transition_into_pre_market_disconnects_connects_subscribes_and_resets_vwap(
    monkeypatch,
):
    provider = _FakeProvider()
    subscribe_calls: list[str] = []
    reset_calls: list[None] = []
    resubscribe_calls: list[None] = []
    sched = _scheduler_with_phase_sequence(
        monkeypatch,
        [MarketPhase.PRE_MARKET],
        provider,
        subscribe_calls,
        reset_calls,
        resubscribe_calls,
    )
    sched.run_once()
    assert provider.calls == ["disconnect", "connect"]
    assert subscribe_calls == list(TRADABLE_UNDERLYINGS) + list(ENV_METRIC_SYMBOLS)
    assert len(reset_calls) == 1
    assert len(resubscribe_calls) == 1


def test_pre_market_resets_subscription_bookkeeping_before_disconnecting(monkeypatch):
    """2026-08-19 regression: the daily PRE_MARKET transition disconnects
    and reconnects the provider -- a genuinely new WS session -- but must
    invalidate registry._subscribed_symbols' "already subscribed"
    bookkeeping *before* that, or a symbol subscribed yesterday reads as
    "already handled" against today's brand-new connection and never gets
    a real subscribe request sent on it. Ordering is asserted via the same
    shared call-order list `_FakeProvider.calls` already records
    disconnect/connect into, not just that both eventually fire.
    """
    provider = _FakeProvider()
    sched = _scheduler_with_phase_sequence(monkeypatch, [MarketPhase.PRE_MARKET], provider)
    monkeypatch.setattr(
        registry_module,
        "reset_subscriptions_for_new_day",
        lambda: provider.calls.append("resubscribe"),
    )

    sched.run_once()

    assert provider.calls == ["resubscribe", "disconnect", "connect"]


def test_fresh_startup_into_active_market_does_not_reset_subscription_bookkeeping(monkeypatch):
    """A mid-day process (re)start already has fresh, empty registry state
    (the module was just reloaded) -- no stale bookkeeping to invalidate,
    and this transition is only ever reached with from_phase=None, never
    from a live PRE_MARKET->ACTIVE_MARKET day-to-day flow, so it must not
    call the daily resubscribe reset.
    """
    provider = _FakeProvider()
    resubscribe_calls: list[None] = []
    sched = _scheduler_with_phase_sequence(
        monkeypatch, [MarketPhase.ACTIVE_MARKET], provider, resubscribe_calls=resubscribe_calls
    )
    sched.run_once()
    assert resubscribe_calls == []


def test_transition_into_closed_does_not_reset_subscription_bookkeeping(monkeypatch):
    provider = _FakeProvider()
    resubscribe_calls: list[None] = []
    sched = _scheduler_with_phase_sequence(
        monkeypatch, [MarketPhase.CLOSED], provider, resubscribe_calls=resubscribe_calls
    )
    sched.run_once()
    assert resubscribe_calls == []


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
    assert subscribe_calls == list(TRADABLE_UNDERLYINGS) + list(ENV_METRIC_SYMBOLS)


def test_fresh_startup_into_active_market_defers_subscription_when_shoonya_not_connected(
    monkeypatch,
):
    """2026-08-14 regression: a restart during market hours reaches this
    transition within one tick, independent of and before
    `strategy_engine.recovery.resume_strategy_runners`'s own reconnect-aware guard --
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
    monkeypatch.setattr(scheduler_module, "is_market_data_ready", lambda: False)

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
    monkeypatch.setattr(scheduler_module, "is_market_data_ready", lambda: False)

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


def test_subscribe_known_underlyings_isolates_first_symbol_failure(monkeypatch, caplog):
    """2026-08-26 live incident: NIFTY (first in TRADABLE_UNDERLYINGS) raised
    during a session-expiry window, which used to propagate straight out of
    the whole subscribe loop and leave `_last_phase` unset (see the
    CLOSED->ACTIVE_MARKET test below for the consequence of that). One
    symbol failing must never block the rest of either loop.
    """
    provider = _FakeProvider()
    subscribe_calls: list[str] = []
    sched = _scheduler_with_phase_sequence(
        monkeypatch, [MarketPhase.ACTIVE_MARKET], provider, subscribe_calls
    )
    failing_symbol = TRADABLE_UNDERLYINGS[0]

    def _flaky_ensure_ingestion_running(symbol: str) -> None:
        if symbol == failing_symbol:
            raise RuntimeError("broker session expired")
        subscribe_calls.append(symbol)

    monkeypatch.setattr(
        registry_module, "ensure_ingestion_running", _flaky_ensure_ingestion_running
    )

    with caplog.at_level("ERROR"):
        sched.run_once()

    assert subscribe_calls == list(TRADABLE_UNDERLYINGS[1:]) + list(ENV_METRIC_SYMBOLS)
    assert sched._last_phase == MarketPhase.ACTIVE_MARKET  # noqa: SLF001
    assert any(failing_symbol in record.message for record in caplog.records)


def test_subscribe_known_underlyings_isolates_env_metric_symbol_failure(monkeypatch, caplog):
    """Mirrors the real INDIA VIX incident: a failure in the *second* loop
    (ENV_METRIC_SYMBOLS) must not affect TRADABLE_UNDERLYINGS, which is
    attempted first and already succeeded by the time this one raises.
    """
    provider = _FakeProvider()
    subscribe_calls: list[str] = []
    sched = _scheduler_with_phase_sequence(
        monkeypatch, [MarketPhase.ACTIVE_MARKET], provider, subscribe_calls
    )
    failing_symbol = ENV_METRIC_SYMBOLS[0]

    def _flaky_ensure_ingestion_running(symbol: str) -> None:
        if symbol == failing_symbol:
            raise RuntimeError("no cached broker token")
        subscribe_calls.append(symbol)

    monkeypatch.setattr(
        registry_module, "ensure_ingestion_running", _flaky_ensure_ingestion_running
    )

    with caplog.at_level("ERROR"):
        sched.run_once()

    assert subscribe_calls == list(TRADABLE_UNDERLYINGS) + [
        s for s in ENV_METRIC_SYMBOLS if s != failing_symbol
    ]
    assert sched._last_phase == MarketPhase.ACTIVE_MARKET  # noqa: SLF001
    assert any(failing_symbol in record.message for record in caplog.records)


def test_closed_to_active_market_transition_connects_and_subscribes(monkeypatch):
    """2026-08-26 live incident: if the scheduler's own PRE_MARKET handling
    never got recorded (from_phase stuck at CLOSED -- previously possible
    when a symbol's subscribe raised uncaught, before the isolation fix
    above), the 09:00 IST transition into ACTIVE_MARKET used to match no
    branch at all and silently subscribed nothing for the rest of the day.
    `from_phase=CLOSED` must be handled the same as `from_phase=None`, plus
    the once-per-day resets PRE_MARKET's own entry would have run.
    """
    provider = _FakeProvider()
    subscribe_calls: list[str] = []
    reset_calls: list[None] = []
    resubscribe_calls: list[None] = []
    sched = _scheduler_with_phase_sequence(
        monkeypatch,
        [MarketPhase.CLOSED, MarketPhase.ACTIVE_MARKET],
        provider,
        subscribe_calls,
        reset_calls,
        resubscribe_calls,
    )
    sched.run_once()  # startup -> closed: disconnect only
    provider.calls.clear()

    sched.run_once()  # closed -> active_market: the transition under test

    assert provider.calls == ["connect"]
    assert subscribe_calls == list(TRADABLE_UNDERLYINGS) + list(ENV_METRIC_SYMBOLS)
    assert len(reset_calls) == 1
    assert len(resubscribe_calls) == 1


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


def test_health_check_fires_and_retries_subscription_during_active_market(monkeypatch):
    """2026-08-26: broadened from PRE_MARKET-only -- a symbol that failed to
    subscribe during PRE_MARKET previously had no real retry path once the
    day reached ACTIVE_MARKET. The health check now also re-attempts
    subscription, not just `provider.connect()`.
    """
    provider = _FakeProvider()
    subscribe_calls: list[str] = []
    tick_seconds = 1.0
    ticks_needed = int(PRE_MARKET_HEALTH_CHECK_SECONDS / tick_seconds)
    phases = [MarketPhase.ACTIVE_MARKET] * (ticks_needed + 1)
    sched = _scheduler_with_phase_sequence(monkeypatch, phases, provider, subscribe_calls)
    sched._tick_seconds = tick_seconds  # noqa: SLF001

    sched.run_once()  # the initial from_phase=None -> active_market transition itself
    provider.calls.clear()
    subscribe_calls.clear()

    for _ in range(ticks_needed - 2):
        sched.run_once()
    assert provider.calls == []  # one tick short of due

    sched.run_once()
    assert provider.calls == ["connect"]  # the health check itself
    assert subscribe_calls == list(TRADABLE_UNDERLYINGS) + list(ENV_METRIC_SYMBOLS)


def test_dormant_weekend_takes_no_action_and_never_reads_phase(monkeypatch):
    """On a dormant weekend run_once must short-circuit before touching
    current_phase() or the provider at all -- no connect/subscribe/health
    churn against a closed market."""
    provider = _FakeProvider()
    sched = _scheduler_with_phase_sequence(monkeypatch, [], provider)

    def _boom():
        raise AssertionError("current_phase must not be consulted while dormant")

    monkeypatch.setattr(scheduler_module, "current_phase", _boom)
    monkeypatch.setattr(scheduler_module.weekend_rest, "is_system_awake", lambda: False)

    sched.run_once()

    assert provider.calls == []
    assert sched._last_phase is None  # noqa: SLF001


def test_awake_to_dormant_edge_disconnects_once_and_resets_phase(monkeypatch):
    """A logout mid-session flips the system dormant. The next run_once
    should tear the connection down exactly once and null _last_phase so a
    later re-login re-triggers a real connect transition."""
    provider = _FakeProvider()
    awake = {"v": True}
    sched = _scheduler_with_phase_sequence(
        monkeypatch, [MarketPhase.ACTIVE_MARKET], provider
    )
    monkeypatch.setattr(
        scheduler_module.weekend_rest, "is_system_awake", lambda: awake["v"]
    )

    sched.run_once()  # awake: from_phase=None -> ACTIVE_MARKET connect+subscribe
    assert provider.calls == ["connect"]
    assert sched._last_phase == MarketPhase.ACTIVE_MARKET  # noqa: SLF001

    awake["v"] = False
    provider.calls.clear()
    sched.run_once()  # dormant edge: one disconnect, phase nulled
    assert provider.calls == ["disconnect"]
    assert sched._last_phase is None  # noqa: SLF001

    provider.calls.clear()
    sched.run_once()  # still dormant: nothing more
    assert provider.calls == []


def test_health_check_still_does_not_fire_during_closed(monkeypatch):
    provider = _FakeProvider()
    phases = [MarketPhase.CLOSED] * 20
    sched = _scheduler_with_phase_sequence(monkeypatch, phases, provider)
    sched._tick_seconds = 1.0  # noqa: SLF001

    sched.run_once()  # the initial startup -> closed transition itself
    provider.calls.clear()

    for _ in range(19):
        sched.run_once()
    assert provider.calls == []


def test_alerts_market_data_no_session_when_neither_broker_is_live(monkeypatch):
    """2026-09-01: the real gap this closes -- `is_market_data_ready()` is
    Shoonya-only and passive, so it alone can't tell you the failback
    (Alice Blue) is also down. With both probes False and a real
    `alert_session_factory` injected, a deferred subscription must raise
    `market_data_no_session`.
    """
    import app.modules.alerting.manager as alerting_manager
    import app.modules.broker_adapter.composition as broker_composition
    import app.modules.market_data.providers.alice_blue_session as alice_blue_session_module

    provider = _FakeProvider()
    sched = _scheduler_with_phase_sequence(monkeypatch, [MarketPhase.PRE_MARKET], provider)
    monkeypatch.setattr(scheduler_module, "is_market_data_ready", lambda: False)
    monkeypatch.setattr(broker_composition, "shoonya_connection_live", lambda: False)
    monkeypatch.setattr(alice_blue_session_module, "alice_blue_connection_live", lambda: False)

    alert_calls: list[dict] = []
    monkeypatch.setattr(
        alerting_manager, "send_alert", lambda db, **kwargs: alert_calls.append(kwargs)
    )
    sched._alert_session_factory = lambda: _FakeAlertDB(["ws-1"])  # noqa: SLF001

    sched.run_once()

    assert len(alert_calls) == 1
    assert alert_calls[0]["category"] == "market_data_no_session"
    assert alert_calls[0]["workspace_id"] == "ws-1"


def test_no_alert_session_factory_stays_silent_by_default(monkeypatch):
    """The constructor default (`alert_session_factory=None`) must stay a
    true no-op -- no exception, no alert call -- since this is exactly what
    every pre-existing test in this file relies on implicitly by
    constructing `MarketDataScheduler(tick_seconds=1.0)` with no awareness
    of alerting at all.
    """
    import app.modules.alerting.manager as alerting_manager
    import app.modules.broker_adapter.composition as broker_composition
    import app.modules.market_data.providers.alice_blue_session as alice_blue_session_module

    provider = _FakeProvider()
    sched = _scheduler_with_phase_sequence(monkeypatch, [MarketPhase.PRE_MARKET], provider)
    monkeypatch.setattr(scheduler_module, "is_market_data_ready", lambda: False)
    monkeypatch.setattr(broker_composition, "shoonya_connection_live", lambda: False)
    monkeypatch.setattr(alice_blue_session_module, "alice_blue_connection_live", lambda: False)

    alert_calls: list[dict] = []
    monkeypatch.setattr(
        alerting_manager, "send_alert", lambda db, **kwargs: alert_calls.append(kwargs)
    )
    assert sched._alert_session_factory is None  # noqa: SLF001 - the constructor default itself

    sched.run_once()  # must not raise

    assert alert_calls == []


def test_no_alert_when_at_least_one_broker_is_live(monkeypatch):
    """The actual point of this feature: `is_market_data_ready()` being
    False (Shoonya-only) is not itself the signal to notify on -- if the
    failback (Alice Blue) is live, nothing needs to alert.
    """
    import app.modules.alerting.manager as alerting_manager
    import app.modules.broker_adapter.composition as broker_composition
    import app.modules.market_data.providers.alice_blue_session as alice_blue_session_module

    provider = _FakeProvider()
    sched = _scheduler_with_phase_sequence(monkeypatch, [MarketPhase.PRE_MARKET], provider)
    monkeypatch.setattr(scheduler_module, "is_market_data_ready", lambda: False)
    monkeypatch.setattr(broker_composition, "shoonya_connection_live", lambda: False)
    monkeypatch.setattr(alice_blue_session_module, "alice_blue_connection_live", lambda: True)

    alert_calls: list[dict] = []
    monkeypatch.setattr(
        alerting_manager, "send_alert", lambda db, **kwargs: alert_calls.append(kwargs)
    )
    sched._alert_session_factory = lambda: _FakeAlertDB(["ws-1"])  # noqa: SLF001

    sched.run_once()

    assert alert_calls == []
