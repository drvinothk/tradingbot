"""No real socket involved — `_run`/`_receive_loop` (the actual connect/
reconnect loop) need a live server to exercise meaningfully, which this
phase has no account to test against anyway. These tests cover the pure
logic instead: subscription bookkeeping and inbound-message dispatch,
which is exactly what a field-name mismatch (this whole package's running
caveat) would break.
"""

from __future__ import annotations

import json

import pytest

from app.modules.broker_adapter.shoonya.ws_client import ShoonyaWSClient, _SubscriptionEntry


def _client(on_depth=None, **kwargs) -> tuple[ShoonyaWSClient, list, list]:
    ticks: list = []
    depths: list = []
    client = ShoonyaWSClient(
        "wss://example.test/ws",
        uid="FA1",
        actid="FA1",
        access_token="tok",
        on_tick=lambda t: ticks.append(t),
        on_depth=(lambda d: depths.append(d)) if on_depth is None else on_depth,
        **kwargs,
    )
    return client, ticks, depths


class _FakeConnection:
    """Stands in for `websockets.sync.connection.Connection` — `_authenticate`
    only ever calls `.send`/`.recv` on it.
    """

    def __init__(self, ack: dict):
        self.sent: list[str] = []
        self._ack = json.dumps(ack)

    def send(self, message: str) -> None:
        self.sent.append(message)

    def recv(self, timeout: float | None = None) -> str:
        return self._ack


def test_subscription_entry_key_is_exchange_pipe_token():
    entry = _SubscriptionEntry(contract_symbol="NIFTY30JUL2624000CE", exchange="NFO", token="12345")
    assert entry.key == "NFO|12345"


def test_subscribe_populates_entries_by_key():
    client, _, _ = _client()
    client.subscribe([("NIFTY30JUL2624000CE", "NFO", "12345")])
    assert "NFO|12345" in client._entries_by_key
    assert client._entries_by_key["NFO|12345"].contract_symbol == "NIFTY30JUL2624000CE"


def test_unsubscribe_removes_matching_entries():
    client, _, _ = _client()
    client.subscribe([("NIFTY30JUL2624000CE", "NFO", "12345")])
    client.unsubscribe(["NIFTY30JUL2624000CE"])
    assert client._entries_by_key == {}


def test_send_is_a_noop_without_a_live_connection():
    client, _, _ = _client()
    # Must not raise even though no `connect()` has ever happened.
    client._send({"t": "t", "k": "NFO|12345"})


def test_handle_message_dispatches_tick_to_on_tick():
    client, ticks, _ = _client()
    client.subscribe([("NIFTY30JUL2624000CE", "NFO", "12345")])

    message = json.dumps(
        {"t": "tk", "e": "NFO", "tk": "12345", "lp": "123.45", "bp1": "123.0", "sp1": "124.0"}
    )
    client._handle_message(message)

    assert len(ticks) == 1
    assert ticks[0].contract_symbol == "NIFTY30JUL2624000CE"
    assert ticks[0].ltp == 123.45


def test_handle_message_dispatches_depth_to_on_depth():
    client, _, depths = _client()
    client.subscribe([("NIFTY30JUL2624000CE", "NFO", "12345")])

    raw = {"t": "dk", "e": "NFO", "tk": "12345"}
    raw.update({f"bp{i}": str(100 - i) for i in range(1, 6)})
    raw.update({f"bq{i}": "10" for i in range(1, 6)})
    raw.update({f"sp{i}": str(100 + i) for i in range(1, 6)})
    raw.update({f"sq{i}": "10" for i in range(1, 6)})
    client._handle_message(json.dumps(raw))

    assert len(depths) == 1
    assert depths[0].contract_symbol == "NIFTY30JUL2624000CE"


def test_handle_message_ignores_unknown_token():
    client, ticks, _ = _client()
    client.subscribe([("NIFTY30JUL2624000CE", "NFO", "12345")])

    message = json.dumps({"t": "tk", "e": "NFO", "tk": "99999", "lp": "1.0"})
    client._handle_message(message)

    assert ticks == []


def test_handle_message_ignores_malformed_json():
    client, ticks, _ = _client()
    client.subscribe([("NIFTY30JUL2624000CE", "NFO", "12345")])
    client._handle_message("not json")  # must not raise
    assert ticks == []


def test_handle_message_swallows_normalization_error():
    client, ticks, _ = _client()
    client.subscribe([("NIFTY30JUL2624000CE", "NFO", "12345")])
    # Missing 'lp' -> parse_tick raises NormalizationError, caught and logged.
    message = json.dumps({"t": "tk", "e": "NFO", "tk": "12345"})
    client._handle_message(message)
    assert ticks == []


def test_authenticate_defaults_source_to_api():
    client, _, _ = _client()
    ws = _FakeConnection({"t": "ck", "s": "OK"})
    client._authenticate(ws)
    sent = json.loads(ws.sent[0])
    assert sent == {
        "t": "c",
        "uid": "FA1",
        "actid": "FA1",
        "susertoken": "tok",
        "source": "API",
    }


def test_authenticate_sends_configured_source():
    client, _, _ = _client(source="WEB")
    ws = _FakeConnection({"t": "ck", "s": "OK"})
    client._authenticate(ws)
    sent = json.loads(ws.sent[0])
    assert sent["source"] == "WEB"


def test_authenticate_raises_on_not_ok_ack():
    client, _, _ = _client()
    ws = _FakeConnection({"t": "ck", "s": "NOT_OK"})
    with pytest.raises(ConnectionError):
        client._authenticate(ws)


