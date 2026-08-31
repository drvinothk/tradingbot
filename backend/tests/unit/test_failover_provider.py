"""`FailoverMarketDataProvider` — the 5s-trip/90s-anti-flap state machine
that switches between a primary and backup `BaseMarketDataProvider`. Ticks
arrive asynchronously (a real WS callback thread in production), so unlike
`MarketHoursGatedProvider`'s tests this needs an injectable clock rather
than pure state inspection — `run_once()` is called directly against a
fake, controllable clock, never via the real background thread's own
wall-clock polling (see `make_provider` fixture below for why that thread
still gets started for real, and why a huge `poll_interval_seconds` keeps
it from interfering).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.modules.broker_adapter.base.contracts import DepthSnapshot, Tick
from app.modules.market_data.providers.base import BaseMarketDataProvider
from app.modules.market_data.providers.failover import FailoverMarketDataProvider

_THRESHOLD = 5.0
_RECOVERY = 20.0
_BACKUP_RETRY = 30.0
# Large enough that the real background thread (started for real by
# subscribe_ticks -- see failover.py's own docstring) never fires a second
# automatic run_once() during a test's real (sub-second) execution time; its
# one unavoidable immediate call at thread-start reads the fake clock before
# the test has advanced it, so it's a harmless no-op.
_POLL_INTERVAL = 1_000_000.0


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeProvider(BaseMarketDataProvider):
    """Inherits from BaseMarketDataProvider (rather than pure duck-typing
    like MarketHoursGatedProvider's own test fake) because make_provider's
    _factory below is fully type-annotated, unlike most test functions in
    this codebase -- mypy only skips checking a function body when it has
    *no* annotations at all, so a duck-typed fake would fail the nominal
    BaseMarketDataProvider parameter check here instead of being silently
    treated as Any.
    """

    def __init__(self, *, subscribe_error: Exception | None = None, ready: bool = True) -> None:
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.close_calls = 0
        self.subscribe_attempts = 0
        self.subscribe_calls: list[list[str]] = []
        self.unsubscribe_calls: list[list[str]] = []
        self.latest_tick_calls: list[str] = []
        self.history_calls: list[str] = []
        self.subscribe_error = subscribe_error
        self.ready = ready
        self.is_ready_calls = 0
        self.on_tick = None
        self.on_depth = None

    def is_ready(self) -> bool:
        self.is_ready_calls += 1
        return self.ready

    def connect(self) -> None:
        self.connect_calls += 1

    def disconnect(self) -> None:
        self.disconnect_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def subscribe_ticks(self, symbols, on_tick, on_depth=None) -> None:
        self.subscribe_attempts += 1
        if self.subscribe_error is not None:
            raise self.subscribe_error
        self.subscribe_calls.append(list(symbols))
        self.on_tick = on_tick
        self.on_depth = on_depth

    def unsubscribe_ticks(self, symbols) -> None:
        self.unsubscribe_calls.append(list(symbols))

    def get_latest_tick(self, symbol):
        self.latest_tick_calls.append(symbol)
        return None

    def get_price_history(self, underlying, start, end, timeframe_seconds=60):
        self.history_calls.append(underlying)
        return []

    def fire_tick(self, tick: Tick) -> None:
        assert self.on_tick is not None, "subscribe_ticks was never called successfully"
        self.on_tick(tick)

    def fire_depth(self, depth: DepthSnapshot) -> None:
        assert self.on_depth is not None
        self.on_depth(depth)


def _tick(symbol: str = "NIFTY") -> Tick:
    return Tick(
        contract_symbol=symbol, ltp=100.0, bid=99.5, ask=100.5, volume=1, oi=None,
        ts=datetime.now(UTC),
    )


@pytest.fixture
def make_provider():
    created: list[FailoverMarketDataProvider] = []

    def _factory(
        primary: _FakeProvider,
        backup: _FakeProvider,
        clock: _FakeClock,
        *,
        failover_threshold_seconds: float = _THRESHOLD,
        recovery_stabilization_seconds: float = _RECOVERY,
        backup_retry_seconds: float = _BACKUP_RETRY,
    ) -> FailoverMarketDataProvider:
        provider = FailoverMarketDataProvider(
            primary=primary,
            backup=backup,
            primary_name="shoonya",
            backup_name="angel_one",
            failover_threshold_seconds=failover_threshold_seconds,
            recovery_stabilization_seconds=recovery_stabilization_seconds,
            backup_retry_seconds=backup_retry_seconds,
            poll_interval_seconds=_POLL_INTERVAL,
            clock=clock,
        )
        created.append(provider)
        return provider

    yield _factory
    for provider in created:
        provider.disconnect()


def test_stays_on_primary_while_ticks_keep_arriving(make_provider):
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)

    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)
    primary.fire_tick(_tick())
    clock.advance(3.0)  # < threshold
    provider.run_once()

    assert provider.active_provider_name == "shoonya"
    assert backup.subscribe_calls == []


def test_grace_period_before_first_tick_does_not_trigger_premature_failover(make_provider):
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)

    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)
    clock.advance(3.0)  # < threshold, no tick has ever arrived yet
    provider.run_once()

    assert provider.active_provider_name == "shoonya"
    assert backup.subscribe_calls == []


def test_trips_to_backup_after_threshold_of_silence_and_lazily_subscribes(make_provider):
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)

    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)
    primary.fire_tick(_tick())
    clock.advance(_THRESHOLD + 1.0)
    provider.run_once()

    assert provider.active_provider_name == "angel_one"
    assert backup.subscribe_calls == [["NIFTY"]]


class _FakeAlertDB:
    """Minimal stand-in for the `Session` `_alert` reads/writes through --
    only `query(TradingSession).filter(...).all()`, `commit()` are ever
    called, so nothing beyond that chain needs faking."""

    def __init__(self, workspace_ids: list) -> None:
        self._workspace_ids = workspace_ids
        self.committed = False

    def query(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return [_FakeTradingSessionRow(ws_id) for ws_id in self._workspace_ids]

    def commit(self) -> None:
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return


class _FakeTradingSessionRow:
    def __init__(self, workspace_id) -> None:
        self.workspace_id = workspace_id


def test_switch_to_backup_alerts_at_warning_not_critical(make_provider, monkeypatch):
    """2026-08-31: a successful automatic failover is the system self-
    healing as designed, not something that should page a human -- only a
    genuine "both legs down" outage (see the "backup_not_ready"/"both_down"
    dedup_suffix cases) should stay CRITICAL. Regression test for the
    literal alert message the user reported: a healthy switch to backup
    must never again fire CRITICAL.
    """
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.modules.market_data.providers.failover.send_alert",
        lambda db, **kwargs: calls.append(kwargs),
    )

    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    workspace_id = "ws-1"
    provider = FailoverMarketDataProvider(
        primary=primary,
        backup=backup,
        primary_name="shoonya",
        backup_name="angel_one",
        failover_threshold_seconds=_THRESHOLD,
        recovery_stabilization_seconds=_RECOVERY,
        backup_retry_seconds=_BACKUP_RETRY,
        poll_interval_seconds=_POLL_INTERVAL,
        clock=clock,
        alert_session_factory=lambda: _FakeAlertDB([workspace_id]),
    )
    try:
        provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)
        primary.fire_tick(_tick())
        clock.advance(_THRESHOLD + 1.0)
        provider.run_once()
    finally:
        provider.disconnect()

    switched_calls = [c for c in calls if c["category"] == "market_data_failover_switch"]
    assert len(switched_calls) == 1
    from app.domain.ops.models import AlertSeverity

    assert switched_calls[0]["severity"] == AlertSeverity.WARNING


def test_only_active_leg_ticks_are_forwarded(make_provider):
    forwarded: list[Tick] = []
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)
    provider.subscribe_ticks(["NIFTY"], on_tick=forwarded.append)

    primary_tick_before = _tick()
    primary.fire_tick(primary_tick_before)
    assert forwarded == [primary_tick_before]

    clock.advance(_THRESHOLD + 1.0)
    provider.run_once()  # trips to backup
    assert provider.active_provider_name == "angel_one"

    stray_primary_tick = _tick()
    primary.fire_tick(stray_primary_tick)  # a straggler from the now-inactive leg
    assert stray_primary_tick not in forwarded

    backup_tick = _tick()
    backup.fire_tick(backup_tick)
    assert forwarded == [primary_tick_before, backup_tick]


def test_backup_subscribe_failure_retries_on_backoff_without_flipping_active(make_provider):
    primary = _FakeProvider()
    backup = _FakeProvider(subscribe_error=RuntimeError("angel login failed"))
    clock = _FakeClock()
    provider = make_provider(primary, backup, clock, backup_retry_seconds=30.0)

    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)
    clock.advance(_THRESHOLD + 1.0)
    provider.run_once()

    assert provider.active_provider_name == "shoonya"  # stays on primary, even though unhealthy
    assert backup.subscribe_attempts == 1

    clock.advance(5.0)  # still within the 30s backoff window
    provider.run_once()
    assert backup.subscribe_attempts == 1  # not retried yet

    clock.advance(30.0)  # now past the backoff window
    provider.run_once()
    assert backup.subscribe_attempts == 2


def test_backup_not_ready_never_attempts_subscribe_and_stays_on_primary(make_provider):
    """2026-08-25: a backup with no live session (e.g. Alice Blue before a
    human completes the browser login) must never even attempt
    subscribe_ticks() -- see is_ready's own docstring on
    BaseMarketDataProvider for why that call would always fail the same way
    anyway. Primary stays "active" (still unhealthy, still retried every
    cycle) rather than being marked "tripped" to a leg that can't actually
    take over.
    """
    primary = _FakeProvider()
    backup = _FakeProvider(ready=False)
    clock = _FakeClock()
    provider = make_provider(primary, backup, clock)

    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)
    clock.advance(_THRESHOLD + 1.0)
    provider.run_once()

    assert provider.active_provider_name == "shoonya"
    assert backup.is_ready_calls == 1
    assert backup.subscribe_attempts == 0


def test_backup_not_ready_rechecks_on_backoff_not_every_cycle(make_provider):
    primary = _FakeProvider()
    backup = _FakeProvider(ready=False)
    clock = _FakeClock()
    provider = make_provider(primary, backup, clock, backup_retry_seconds=30.0)

    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)
    clock.advance(_THRESHOLD + 1.0)
    provider.run_once()
    assert backup.is_ready_calls == 1

    clock.advance(5.0)  # still within the 30s backoff window
    provider.run_once()
    assert backup.is_ready_calls == 1  # not rechecked yet

    clock.advance(30.0)  # now past the backoff window
    provider.run_once()
    assert backup.is_ready_calls == 2


def test_backup_becoming_ready_allows_a_later_trip(make_provider):
    """The readiness gate must not permanently latch "unready" -- once the
    backoff window passes and the backup has since become ready (a human
    connected it), the next recheck must actually subscribe and trip.
    """
    primary = _FakeProvider()
    backup = _FakeProvider(ready=False)
    clock = _FakeClock()
    provider = make_provider(primary, backup, clock, backup_retry_seconds=30.0)

    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)
    clock.advance(_THRESHOLD + 1.0)
    provider.run_once()
    assert provider.active_provider_name == "shoonya"

    backup.ready = True
    clock.advance(30.0)
    provider.run_once()

    assert provider.active_provider_name == "angel_one"
    assert backup.subscribe_calls == [["NIFTY"]]


def test_manual_override_to_a_not_ready_backup_raises_and_does_not_apply(make_provider):
    primary = _FakeProvider()
    backup = _FakeProvider(ready=False)
    clock = _FakeClock()
    provider = make_provider(primary, backup, clock)
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)

    with pytest.raises(RuntimeError):
        provider.set_manual_override("angel_one")

    assert provider.active_provider_name == "shoonya"
    assert provider.manual_override is None
    assert backup.subscribe_attempts == 0


def _subscribe_and_trip_to_backup(
    provider: FailoverMarketDataProvider, primary: _FakeProvider, clock: _FakeClock
) -> None:
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)
    _trip_to_backup(provider, primary, clock)


def _trip_to_backup(
    provider: FailoverMarketDataProvider, primary: _FakeProvider, clock: _FakeClock
) -> None:
    """Assumes subscribe_ticks was already called by the caller -- kept
    separate from _subscribe_and_trip_to_backup so a test that already
    subscribed (e.g. to capture its own on_tick/on_depth) doesn't have that
    clobbered by a second, different subscribe_ticks call here.
    """
    primary.fire_tick(_tick())
    clock.advance(_THRESHOLD + 1.0)
    provider.run_once()
    assert provider.active_provider_name == "angel_one"


def test_recovery_requires_the_full_stabilization_window(make_provider):
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock, recovery_stabilization_seconds=_RECOVERY)
    _subscribe_and_trip_to_backup(provider, primary, clock)

    # First healthy observation after the trip starts the recovery timer.
    recovery_start = clock.now
    primary.fire_tick(_tick())
    provider.run_once()
    assert provider.active_provider_name == "angel_one"

    # A fresh tick just short of the full window -- still not recovered.
    clock.now = recovery_start + _RECOVERY - 1.0
    primary.fire_tick(_tick())
    provider.run_once()
    assert provider.active_provider_name == "angel_one"

    # Cross the full window.
    clock.now = recovery_start + _RECOVERY + 1.0
    primary.fire_tick(_tick())
    provider.run_once()
    assert provider.active_provider_name == "shoonya"


def test_recovery_timer_resets_on_a_drop_mid_window(make_provider):
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock, recovery_stabilization_seconds=_RECOVERY)
    _subscribe_and_trip_to_backup(provider, primary, clock)

    # Recovery starts...
    recovery_start = clock.now
    primary.fire_tick(_tick())
    provider.run_once()
    assert provider.active_provider_name == "angel_one"

    # ...then primary drops again before the window completes -- a genuine
    # gap longer than failover_threshold_seconds with no tick at all.
    clock.now = recovery_start + _THRESHOLD + 1.0
    provider.run_once()
    assert provider.active_provider_name == "angel_one"  # timer reset, not recovered

    # If the reset hadn't happened, this much more time since the *original*
    # recovery start would already exceed the stabilization window. Since it
    # did reset, one fresh tick this soon after the drop isn't enough yet.
    clock.now = recovery_start + _RECOVERY - 1.0
    primary.fire_tick(_tick())
    provider.run_once()
    assert provider.active_provider_name == "angel_one"


def test_recovery_unsubscribes_backup_and_flips_back(make_provider):
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock, recovery_stabilization_seconds=_RECOVERY)
    _subscribe_and_trip_to_backup(provider, primary, clock)

    recovery_start = clock.now
    primary.fire_tick(_tick())
    provider.run_once()
    assert provider.active_provider_name == "angel_one"

    clock.now = recovery_start + _RECOVERY + 1.0
    primary.fire_tick(_tick())
    provider.run_once()

    assert provider.active_provider_name == "shoonya"
    assert backup.unsubscribe_calls == [["NIFTY"]]


def test_get_latest_tick_and_get_price_history_reflect_active_leg(make_provider):
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)

    provider.get_latest_tick("NIFTY")
    provider.get_price_history("NIFTY", datetime.now(UTC), datetime.now(UTC))
    assert primary.latest_tick_calls == ["NIFTY"]
    assert primary.history_calls == ["NIFTY"]
    assert backup.latest_tick_calls == []

    _trip_to_backup(provider, primary, clock)
    provider.get_latest_tick("NIFTY")
    provider.get_price_history("NIFTY", datetime.now(UTC), datetime.now(UTC))
    assert backup.latest_tick_calls == ["NIFTY"]
    assert backup.history_calls == ["NIFTY"]


def test_depth_is_also_gated_to_the_active_leg(make_provider):
    forwarded: list[DepthSnapshot] = []
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)
    # Must match the subscribed symbol -- depth routing is per-symbol now
    # (2026-08-13 fix), not one shared slot that fired for any symbol.
    depth = DepthSnapshot(
        contract_symbol="NIFTY", bid_levels=(), ask_levels=(), ts=datetime.now(UTC)
    )
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None, on_depth=forwarded.append)

    primary.fire_depth(depth)
    assert forwarded == [depth]

    _trip_to_backup(provider, primary, clock)
    primary.fire_depth(depth)  # stray, from the now-inactive leg
    assert forwarded == [depth]
    backup.fire_depth(depth)
    assert forwarded == [depth, depth]


def test_multiple_subscribe_calls_accumulate_symbols_not_overwrite(make_provider):
    """Regression: MarketDataIngestionService/registry.ensure_ingestion_running
    call subscribe_ticks once per *new* underlying, additively -- a second
    call must not drop the first call's symbol from what a later failover
    subscribes on backup.
    """
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)

    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)
    provider.subscribe_ticks(["BANKNIFTY"], on_tick=lambda t: None)
    primary.fire_tick(_tick("NIFTY"))
    clock.advance(_THRESHOLD + 1.0)
    provider.run_once()

    assert backup.subscribe_calls == [["BANKNIFTY", "NIFTY"]]


def test_close_and_disconnect_duck_type_close_both_legs_and_stop_thread(make_provider):
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)
    _subscribe_and_trip_to_backup(provider, primary, clock)

    provider.close()

    assert primary.close_calls == 1
    assert backup.close_calls == 1


class _MinimalFakeProvider(BaseMarketDataProvider):
    """No `close()` at all -- the exact real-world shape of
    `BrokerPortMarketDataAdapter` (Shoonya), which has never implemented
    one. `_FakeProvider` above always has, which is exactly why the
    2026-08-14 bug (`close()` silently never calling `disconnect()`)
    passed every existing test in this file undetected.
    """

    def __init__(self) -> None:
        self.disconnect_calls = 0

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        self.disconnect_calls += 1

    def subscribe_ticks(self, symbols, on_tick, on_depth=None) -> None:
        pass

    def unsubscribe_ticks(self, symbols) -> None:
        pass

    def get_latest_tick(self, symbol):
        return None

    def get_price_history(self, underlying, start, end, timeframe_seconds=60):
        return []


def test_close_tears_down_a_leg_that_has_no_close_method_of_its_own(make_provider):
    """The exact real-world regression: a leg with no `close()` at all
    (matching `BrokerPortMarketDataAdapter`) must still have its
    `disconnect()` called by `FailoverMarketDataProvider.close()` — proving
    the fix doesn't depend on the leg happening to implement an optional
    extra method.
    """
    primary = _MinimalFakeProvider()
    backup = _MinimalFakeProvider()
    provider = FailoverMarketDataProvider(
        primary=primary,
        backup=backup,
        primary_name="shoonya",
        backup_name="angel_one",
        failover_threshold_seconds=_THRESHOLD,
        recovery_stabilization_seconds=_RECOVERY,
        backup_retry_seconds=_BACKUP_RETRY,
        poll_interval_seconds=_POLL_INTERVAL,
    )
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda _t: None)

    provider.close()

    assert primary.disconnect_calls == 1


def test_stuck_watchdog_join_logs_loudly_instead_of_silently_proceeding(
    make_provider, monkeypatch, caplog
):
    """Defense-in-depth added alongside the 2026-08-14 teardown fixes: if
    `_stop_watchdog`'s `join(timeout=...)` times out (plausibly because the
    thread is currently blocked inside `_ensure_backup_subscribed`'s
    network call to a struggling backup provider — the same kind of
    connectivity issue that could have triggered this teardown in the
    first place), the old code proceeded silently as if teardown had fully
    succeeded. The thread itself can't be force-killed from here (Python
    has no safe API for that), but the fact that it didn't stop must not
    be invisible.
    """
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda _t: None)
    assert provider._thread is not None  # noqa: SLF001
    monkeypatch.setattr(provider._thread, "join", lambda timeout=None: None)  # noqa: SLF001
    monkeypatch.setattr(provider._thread, "is_alive", lambda: True)  # noqa: SLF001

    with caplog.at_level("ERROR", logger="app.market_data.failover"):
        provider.disconnect()

    assert "did not stop" in caplog.text


def test_close_also_calls_disconnect_on_both_legs(make_provider):
    """Real, live bug fixed 2026-08-14: `close()` used to *only* probe both
    legs for an optional `close()` method and never called `disconnect()`
    at all — invisible in this file's own tests because `_FakeProvider`
    (unlike the real `BrokerPortMarketDataAdapter`, which has never
    implemented `close()`) happens to implement both. `disconnect()` is the
    one `BaseMarketDataProvider` method every real provider is guaranteed
    to have; `close()` must call it unconditionally, not just probe for an
    optional extra.
    """
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)
    _subscribe_and_trip_to_backup(provider, primary, clock)

    provider.close()

    assert primary.disconnect_calls == 1
    assert backup.disconnect_calls == 1


def test_two_symbols_with_different_callbacks_do_not_cross_talk(make_provider):
    """Regression, 2026-08-13: a single shared on_tick slot (not per-symbol)
    meant a second subscribe_ticks call for a different symbol (e.g.
    PositionManager subscribing an option contract) silently broke the
    first call's own callback for its own symbol (e.g.
    MarketDataIngestionService's underlying tick persistence).
    """
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)
    underlying_ticks: list[Tick] = []
    option_ticks: list[Tick] = []

    provider.subscribe_ticks(["NIFTY"], on_tick=underlying_ticks.append)
    provider.subscribe_ticks(["NIFTY18AUG26C24400"], on_tick=option_ticks.append)

    primary.fire_tick(_tick("NIFTY"))
    primary.fire_tick(_tick("NIFTY18AUG26C24400"))

    assert len(underlying_ticks) == 1
    assert underlying_ticks[0].contract_symbol == "NIFTY"
    assert len(option_ticks) == 1
    assert option_ticks[0].contract_symbol == "NIFTY18AUG26C24400"


def test_resubscribing_the_same_symbol_with_a_different_callback_does_not_clobber_it(
    make_provider,
):
    """Real, live bug fixed 2026-08-17: the 2026-08-13 fix above assumed
    ingestion and PositionManager subscribe *disjoint* symbol sets
    (underlyings vs option contracts) -- true for options, false for the
    underlying itself. PositionManager._ensure_symbol_subscribed also
    subscribes the underlying (a no-op callback, purely to warm
    get_latest_tick() for its own live-price read on an open position),
    colliding on the exact symbol MarketDataIngestionService already
    registered its real persistence callback for. Live-confirmed: NIFTY's
    quote_ticks stopped incrementing at the exact moment a position opened
    and PositionManager's own underlying re-subscribe fired, while real WS
    frames kept arriving on the wire uninterrupted -- a client-side
    registration bug, not a broker-side drop. First-registrant-wins
    (setdefault) fixes this.
    """
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)
    underlying_ticks: list[Tick] = []

    provider.subscribe_ticks(["NIFTY"], on_tick=underlying_ticks.append)
    # PositionManager's own later re-subscribe of the *same* underlying,
    # for its own live-price read -- must not steal NIFTY's callback slot.
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda _tick: None)

    primary.fire_tick(_tick("NIFTY"))

    assert len(underlying_ticks) == 1


def test_per_symbol_routing_composes_correctly_with_active_leg_routing(make_provider):
    """Two symbols, two callbacks, subscribed before a failover trip -- once
    tripped to backup, ticks for each symbol from backup must still reach
    only their own correct callback (not just "some" callback).
    """
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)
    underlying_ticks: list[Tick] = []
    option_ticks: list[Tick] = []

    provider.subscribe_ticks(["NIFTY"], on_tick=underlying_ticks.append)
    provider.subscribe_ticks(["NIFTY18AUG26C24400"], on_tick=option_ticks.append)
    _trip_to_backup(provider, primary, clock)
    # _trip_to_backup's own internal primary.fire_tick() (default symbol
    # "NIFTY") legitimately reaches underlying_ticks too, since primary was
    # still active at that moment -- clear both lists so what follows only
    # counts ticks from *this* test's own backup.fire_tick calls below.
    underlying_ticks.clear()
    option_ticks.clear()

    backup.fire_tick(_tick("NIFTY"))
    backup.fire_tick(_tick("NIFTY18AUG26C24400"))

    assert len(underlying_ticks) == 1
    assert underlying_ticks[0].contract_symbol == "NIFTY"
    assert len(option_ticks) == 1
    assert option_ticks[0].contract_symbol == "NIFTY18AUG26C24400"


# -- Ops-Hardening Phase 4: set_manual_override ------------------------------


def test_manual_override_to_backup_subscribes_and_activates_it(make_provider):
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)

    provider.set_manual_override("angel_one")

    assert provider.active_provider_name == "angel_one"
    assert provider.manual_override == "angel_one"
    assert backup.subscribe_calls == [["NIFTY"]]


def test_manual_override_to_primary_does_not_touch_backup(make_provider):
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)

    provider.set_manual_override("shoonya")

    assert provider.active_provider_name == "shoonya"
    assert backup.subscribe_calls == []


def test_manual_override_rejects_an_unrecognized_provider_name(make_provider):
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)

    with pytest.raises(ValueError):
        provider.set_manual_override("truedata")


def test_manual_override_suspends_automatic_failover(make_provider):
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)
    provider.set_manual_override("shoonya")

    # Primary goes stale well past the trip threshold -- without an
    # override this would trip to backup (see _trip_to_backup above).
    primary.fire_tick(_tick())
    clock.advance(_THRESHOLD + 1.0)
    provider.run_once()

    assert provider.active_provider_name == "shoonya"
    assert backup.subscribe_calls == []


def test_manual_override_backup_subscribe_failure_raises_and_does_not_apply(make_provider):
    primary = _FakeProvider()
    backup = _FakeProvider(subscribe_error=RuntimeError("connection refused"))
    clock = _FakeClock()
    provider = make_provider(primary, backup, clock)
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)

    with pytest.raises(RuntimeError):
        provider.set_manual_override("angel_one")

    assert provider.active_provider_name == "shoonya"
    assert provider.manual_override is None


def test_clearing_override_resumes_automatic_recovery_not_instant_snapback(make_provider):
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock, recovery_stabilization_seconds=_RECOVERY)
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)
    provider.set_manual_override("angel_one")

    # Primary has been healthy the whole time (never actually failed) --
    # clearing the override must still go through the normal stabilization
    # window, not snap straight back just because the override is gone.
    primary.fire_tick(_tick())
    provider.set_manual_override(None)
    provider.run_once()
    assert provider.active_provider_name == "angel_one"  # not yet -- window hasn't elapsed

    clock.advance(_RECOVERY + 1.0)
    primary.fire_tick(_tick())
    provider.run_once()
    assert provider.active_provider_name == "shoonya"  # now recovered for real


def test_manual_override_property_defaults_to_none(make_provider):
    primary, backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, backup, clock)

    assert provider.manual_override is None


# -- replace_backup ------------------------------------------------------


def test_replace_backup_swaps_reference_when_dormant_without_touching_primary(make_provider):
    """The common case (primary healthy, backup never subscribed) — a
    Shoonya reconnect while it's only the backup must be able to refresh
    the stale reference without disturbing anything else. Neither the old
    nor the new backup should see any subscribe/disconnect call, since
    backup was never active in the first place.
    """
    primary, old_backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, old_backup, clock)
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)
    new_backup = _FakeProvider()

    provider.replace_backup(new_backup)

    assert old_backup.disconnect_calls == 0
    assert new_backup.subscribe_attempts == 0

    # The swap is real, not cosmetic: a later real trip must use the *new*
    # backup, not the discarded one.
    clock.advance(_THRESHOLD + 1.0)
    provider.run_once()
    assert provider.active_provider_name == "angel_one"
    assert new_backup.subscribe_calls == [["NIFTY"]]
    assert old_backup.subscribe_calls == []


def test_replace_backup_resubscribes_immediately_when_backup_already_active(make_provider):
    """A real primary outage is already in progress (backup is the active
    leg) when Shoonya reconnects -- the swap must not go dark: the old
    backup is disconnected and the new one is subscribed with the same
    symbols right away, not deferred to the next failover trip.
    """
    primary, old_backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, old_backup, clock)
    forwarded: list[Tick] = []
    provider.subscribe_ticks(["NIFTY"], on_tick=forwarded.append)
    clock.advance(_THRESHOLD + 1.0)
    provider.run_once()
    assert provider.active_provider_name == "angel_one"

    new_backup = _FakeProvider()
    provider.replace_backup(new_backup)

    assert old_backup.disconnect_calls == 1
    assert new_backup.subscribe_calls == [["NIFTY"]]

    tick = _tick()
    new_backup.fire_tick(tick)
    assert forwarded[-1] is tick


def test_replace_backup_new_subscribe_failure_is_swallowed_not_raised(make_provider):
    """Mirrors _ensure_backup_subscribed's own failure handling — a
    reconnect-triggered refresh must never itself raise (it runs from
    oauth_callback's best-effort tail, see that call site's own comment)
    even if the freshly-swapped-in backup immediately fails to subscribe.
    """
    primary, old_backup, clock = _FakeProvider(), _FakeProvider(), _FakeClock()
    provider = make_provider(primary, old_backup, clock)
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)
    clock.advance(_THRESHOLD + 1.0)
    provider.run_once()
    assert provider.active_provider_name == "angel_one"

    failing_backup = _FakeProvider(subscribe_error=RuntimeError("shoonya ws down"))

    provider.replace_backup(failing_backup)  # must not raise
