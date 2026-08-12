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

    def __init__(self, *, subscribe_error: Exception | None = None) -> None:
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.close_calls = 0
        self.subscribe_attempts = 0
        self.subscribe_calls: list[list[str]] = []
        self.unsubscribe_calls: list[list[str]] = []
        self.latest_tick_calls: list[str] = []
        self.history_calls: list[str] = []
        self.subscribe_error = subscribe_error
        self.on_tick = None
        self.on_depth = None

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
    depth = DepthSnapshot(
        contract_symbol="NIFTY26AUGC24000", bid_levels=(), ask_levels=(), ts=datetime.now(UTC)
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
