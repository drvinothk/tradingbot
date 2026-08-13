"""`BrokerPortMarketDataAdapter` — per-symbol tick/depth callback dispatch.
Regression coverage for a real, live bug (2026-08-13): a single shared
`self._on_tick_external` slot, overwritten on every `subscribe_ticks()`
call, meant a second caller subscribing a *different* symbol on this same
shared provider (e.g. `PositionManager` subscribing an option contract)
silently broke the first caller's own callback for its own symbol (e.g.
`MarketDataIngestionService`'s underlying tick persistence) — the exact
shape every test below exercises directly.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.modules.broker_adapter.base.contracts import DepthSnapshot, Tick
from app.modules.market_data.providers.broker_port_shim import BrokerPortMarketDataAdapter


class _FakeBrokerPort:
    def __init__(self) -> None:
        self.on_tick = None
        self.on_depth = None
        self.subscribe_calls: list[list[str]] = []
        self.unsubscribe_calls: list[list[str]] = []

    def subscribe_quotes(self, symbols, on_tick, on_depth=None) -> None:
        self.subscribe_calls.append(list(symbols))
        self.on_tick = on_tick
        self.on_depth = on_depth

    def unsubscribe_quotes(self, symbols) -> None:
        self.unsubscribe_calls.append(list(symbols))

    def get_price_history(self, underlying, start, end, timeframe_seconds=60):
        return []

    def fire_tick(self, tick: Tick) -> None:
        assert self.on_tick is not None
        self.on_tick(tick)

    def fire_depth(self, depth: DepthSnapshot) -> None:
        assert self.on_depth is not None
        self.on_depth(depth)


def _tick(symbol: str) -> Tick:
    return Tick(
        contract_symbol=symbol, ltp=100.0, bid=99.5, ask=100.5, volume=1, oi=None,
        ts=datetime.now(UTC),
    )


def test_two_symbols_with_different_callbacks_do_not_cross_talk():
    """The exact regression shape: subscribing NIFTY (ingestion's callback)
    then subscribing an option contract (PositionManager's own, different
    callback) must not break NIFTY's own delivery.
    """
    broker = _FakeBrokerPort()
    provider = BrokerPortMarketDataAdapter(broker)
    nifty_ticks: list[Tick] = []
    option_ticks: list[Tick] = []

    provider.subscribe_ticks(["NIFTY"], on_tick=nifty_ticks.append)
    provider.subscribe_ticks(["NIFTY18AUG26C24400"], on_tick=option_ticks.append)

    broker.fire_tick(_tick("NIFTY"))
    broker.fire_tick(_tick("NIFTY18AUG26C24400"))

    assert len(nifty_ticks) == 1
    assert nifty_ticks[0].contract_symbol == "NIFTY"
    assert len(option_ticks) == 1
    assert option_ticks[0].contract_symbol == "NIFTY18AUG26C24400"


def test_a_second_subscribers_no_op_callback_does_not_silence_the_first():
    """PositionManager's real call shape: on_tick=lambda _tick: None."""
    broker = _FakeBrokerPort()
    provider = BrokerPortMarketDataAdapter(broker)
    nifty_ticks: list[Tick] = []

    provider.subscribe_ticks(["NIFTY"], on_tick=nifty_ticks.append)
    provider.subscribe_ticks(["NIFTY18AUG26C24400"], on_tick=lambda _tick: None)

    broker.fire_tick(_tick("NIFTY"))

    assert len(nifty_ticks) == 1


def test_get_latest_tick_updates_regardless_of_which_symbols_callback():
    broker = _FakeBrokerPort()
    provider = BrokerPortMarketDataAdapter(broker)
    provider.subscribe_ticks(["NIFTY"], on_tick=lambda _tick: None)
    provider.subscribe_ticks(["NIFTY18AUG26C24400"], on_tick=lambda _tick: None)

    broker.fire_tick(_tick("NIFTY"))
    broker.fire_tick(_tick("NIFTY18AUG26C24400"))

    assert provider.get_latest_tick("NIFTY") is not None
    assert provider.get_latest_tick("NIFTY18AUG26C24400") is not None


def test_unsubscribe_removes_only_that_symbols_callback():
    broker = _FakeBrokerPort()
    provider = BrokerPortMarketDataAdapter(broker)
    nifty_ticks: list[Tick] = []
    option_ticks: list[Tick] = []
    provider.subscribe_ticks(["NIFTY"], on_tick=nifty_ticks.append)
    provider.subscribe_ticks(["NIFTY18AUG26C24400"], on_tick=option_ticks.append)

    provider.unsubscribe_ticks(["NIFTY18AUG26C24400"])
    broker.fire_tick(_tick("NIFTY"))
    broker.fire_tick(_tick("NIFTY18AUG26C24400"))  # stray tick after unsubscribe, must be dropped

    assert len(nifty_ticks) == 1
    assert option_ticks == []


def test_depth_is_also_routed_per_symbol():
    broker = _FakeBrokerPort()
    provider = BrokerPortMarketDataAdapter(broker)
    nifty_depths: list[DepthSnapshot] = []
    option_depths: list[DepthSnapshot] = []

    provider.subscribe_ticks(["NIFTY"], on_tick=lambda _t: None, on_depth=nifty_depths.append)
    provider.subscribe_ticks(
        ["NIFTY18AUG26C24400"], on_tick=lambda _t: None, on_depth=option_depths.append
    )

    def _depth(symbol: str) -> DepthSnapshot:
        return DepthSnapshot(
            contract_symbol=symbol, bid_levels=(), ask_levels=(), ts=datetime.now(UTC)
        )

    broker.fire_depth(_depth("NIFTY"))
    broker.fire_depth(_depth("NIFTY18AUG26C24400"))

    assert len(nifty_depths) == 1
    assert len(option_depths) == 1


def test_no_depth_callback_passed_through_when_no_caller_wants_depth():
    """subscribe_quotes must not receive a depth dispatcher at all when
    this call's own on_depth is None -- passing one unconditionally would
    signal "subscribe depth" to the broker even for a caller that never
    asked for it.
    """
    broker = _FakeBrokerPort()
    provider = BrokerPortMarketDataAdapter(broker)

    provider.subscribe_ticks(["NIFTY"], on_tick=lambda _t: None)

    assert broker.on_depth is None
