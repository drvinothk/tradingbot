"""`MarketHoursGatedProvider` — confirms the actual "strict zero-activity"
mechanism: outside market hours, the inner provider's connect/subscribe/
history methods are never even called, not just logged-and-called-anyway.
Also confirms `disconnect`/`unsubscribe_ticks`/`get_latest_tick` are never
gated, and that `allow_offhours=True` bypasses the gate entirely.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.modules.market_data.providers.market_hours_gate import MarketHoursGatedProvider


class _FakeInnerProvider:
    def __init__(self) -> None:
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.subscribe_calls: list[list[str]] = []
        self.unsubscribe_calls: list[list[str]] = []
        self.history_calls: list[str] = []
        self.latest_tick_calls: list[str] = []

    def connect(self) -> None:
        self.connect_calls += 1

    def disconnect(self) -> None:
        self.disconnect_calls += 1

    def subscribe_ticks(self, symbols, on_tick, on_depth=None) -> None:
        self.subscribe_calls.append(symbols)

    def unsubscribe_ticks(self, symbols) -> None:
        self.unsubscribe_calls.append(symbols)

    def get_latest_tick(self, symbol):
        self.latest_tick_calls.append(symbol)
        return None

    def get_price_history(self, underlying, start, end, timeframe_seconds=60):
        self.history_calls.append(underlying)
        return ["a-candle"]  # sentinel, just needs to be non-empty/truthy


def _gated(inner, *, allow_offhours: bool, blocked: bool, monkeypatch) -> MarketHoursGatedProvider:
    import app.modules.market_data.providers.market_hours_gate as gate_module

    monkeypatch.setattr(gate_module, "is_within_market_hours", lambda: not blocked)
    return MarketHoursGatedProvider(inner, allow_offhours=allow_offhours)


def test_connect_blocked_outside_market_hours(monkeypatch):
    inner = _FakeInnerProvider()
    gated = _gated(inner, allow_offhours=False, blocked=True, monkeypatch=monkeypatch)
    gated.connect()
    assert inner.connect_calls == 0


def test_connect_allowed_within_market_hours(monkeypatch):
    inner = _FakeInnerProvider()
    gated = _gated(inner, allow_offhours=False, blocked=False, monkeypatch=monkeypatch)
    gated.connect()
    assert inner.connect_calls == 1


def test_connect_allowed_offhours_when_override_set(monkeypatch):
    inner = _FakeInnerProvider()
    gated = _gated(inner, allow_offhours=True, blocked=True, monkeypatch=monkeypatch)
    gated.connect()
    assert inner.connect_calls == 1


def test_subscribe_ticks_blocked_outside_market_hours(monkeypatch):
    inner = _FakeInnerProvider()
    gated = _gated(inner, allow_offhours=False, blocked=True, monkeypatch=monkeypatch)
    gated.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)
    assert inner.subscribe_calls == []


def test_get_price_history_blocked_outside_market_hours_returns_empty(monkeypatch):
    inner = _FakeInnerProvider()
    gated = _gated(inner, allow_offhours=False, blocked=True, monkeypatch=monkeypatch)
    result = gated.get_price_history("NIFTY", datetime.now(UTC), datetime.now(UTC))
    assert result == []
    assert inner.history_calls == []


def test_get_price_history_allowed_within_market_hours(monkeypatch):
    inner = _FakeInnerProvider()
    gated = _gated(inner, allow_offhours=False, blocked=False, monkeypatch=monkeypatch)
    result = gated.get_price_history("NIFTY", datetime.now(UTC), datetime.now(UTC))
    assert result == ["a-candle"]
    assert inner.history_calls == ["NIFTY"]


def test_disconnect_is_never_gated(monkeypatch):
    inner = _FakeInnerProvider()
    gated = _gated(inner, allow_offhours=False, blocked=True, monkeypatch=monkeypatch)
    gated.disconnect()
    assert inner.disconnect_calls == 1


def test_unsubscribe_ticks_is_never_gated(monkeypatch):
    inner = _FakeInnerProvider()
    gated = _gated(inner, allow_offhours=False, blocked=True, monkeypatch=monkeypatch)
    gated.unsubscribe_ticks(["NIFTY"])
    assert inner.unsubscribe_calls == [["NIFTY"]]


def test_get_latest_tick_is_never_gated(monkeypatch):
    inner = _FakeInnerProvider()
    gated = _gated(inner, allow_offhours=False, blocked=True, monkeypatch=monkeypatch)
    gated.get_latest_tick("NIFTY")
    assert inner.latest_tick_calls == ["NIFTY"]
