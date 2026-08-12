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
    entry = _SubscriptionEntry(contract_symbol="NIFTY30JUL26C24000", exchange="NFO", token="12345")
    assert entry.key == "NFO|12345"


def test_subscribe_populates_entries_by_key():
    client, _, _ = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])
    assert "NFO|12345" in client._entries_by_key
    assert client._entries_by_key["NFO|12345"].contract_symbol == "NIFTY30JUL26C24000"


def test_unsubscribe_removes_matching_entries():
    client, _, _ = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])
    client.unsubscribe(["NIFTY30JUL26C24000"])
    assert client._entries_by_key == {}


def test_has_subscriptions_reflects_remaining_entries():
    """This is what a caller that only owns *some* of a shared connection's
    subscriptions (e.g. a diagnostic call) checks before deciding to
    `stop()` the whole thing — must not falsely report empty while another
    caller's subscription is still live.
    """
    client, _, _ = _client()
    assert client.has_subscriptions() is False

    client.subscribe(
        [("NIFTY30JUL26C24000", "NFO", "12345"), ("BANKNIFTY30JUL26C50000", "NFO", "67890")]
    )
    assert client.has_subscriptions() is True

    client.unsubscribe(["NIFTY30JUL26C24000"])
    assert client.has_subscriptions() is True, "one entry remains, must not report empty"

    client.unsubscribe(["BANKNIFTY30JUL26C50000"])
    assert client.has_subscriptions() is False


def test_send_is_a_noop_without_a_live_connection():
    client, _, _ = _client()
    # Must not raise even though no `connect()` has ever happened.
    client._send({"t": "t", "k": "NFO|12345"})


def test_subscribe_sends_immediately_on_an_already_live_connection():
    """2026-08-12 regression test for the real bug found via a live
    market-hours diagnostic: `subscribe()` called on an already-connected
    client (any thread other than `_run`'s own) used to update
    `_entries_by_key` only, never actually writing to the socket, because
    `_send` required an explicit `ws=` that only `_run` ever passed. This
    is what `ticks_received: 0` traced back to — the subscribe request
    never left the process. `_live_ws` (set by `_run` once connected) is
    what `_send` now falls back to for exactly this case.
    """
    client, _, _ = _client()
    ws = _FakeConnection({"t": "ak", "s": "OK"})
    client._live_ws = ws

    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])

    sent = [json.loads(raw) for raw in ws.sent]
    assert {"t": "t", "k": "NFO|12345"} in sent


def test_unsubscribe_sends_immediately_on_an_already_live_connection():
    client, _, _ = _client()
    ws = _FakeConnection({"t": "ak", "s": "OK"})
    client._live_ws = ws
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])

    client.unsubscribe(["NIFTY30JUL26C24000"])

    assert json.loads(ws.sent[-1]) == {"t": "u", "k": "NFO|12345"}


def test_handle_message_dispatches_tick_to_on_tick():
    client, ticks, _ = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])

    message = json.dumps(
        {"t": "tk", "e": "NFO", "tk": "12345", "lp": "123.45", "bp1": "123.0", "sp1": "124.0"}
    )
    client._handle_message(message)

    assert len(ticks) == 1
    assert ticks[0].contract_symbol == "NIFTY30JUL26C24000"
    assert ticks[0].ltp == 123.45


def test_handle_message_dispatches_depth_to_on_depth():
    client, _, depths = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])

    raw = {"t": "dk", "e": "NFO", "tk": "12345"}
    raw.update({f"bp{i}": str(100 - i) for i in range(1, 6)})
    raw.update({f"bq{i}": "10" for i in range(1, 6)})
    raw.update({f"sp{i}": str(100 + i) for i in range(1, 6)})
    raw.update({f"sq{i}": "10" for i in range(1, 6)})
    client._handle_message(json.dumps(raw))

    assert len(depths) == 1
    assert depths[0].contract_symbol == "NIFTY30JUL26C24000"


def test_handle_message_ignores_unknown_token():
    client, ticks, _ = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])

    message = json.dumps({"t": "tk", "e": "NFO", "tk": "99999", "lp": "1.0"})
    client._handle_message(message)

    assert ticks == []


def test_handle_message_ignores_malformed_json():
    client, ticks, _ = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])
    client._handle_message("not json")  # must not raise
    assert ticks == []


def test_handle_message_swallows_normalization_error():
    client, ticks, _ = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])
    # Missing 'lp' -> parse_tick raises NormalizationError, caught and logged.
    message = json.dumps({"t": "tk", "e": "NFO", "tk": "12345"})
    client._handle_message(message)
    assert ticks == []


def test_authenticate_defaults_source_to_api():
    """Payload shape per Shoonya support's own 2026-08-11 reply — "t": "a"
    and "accesstoken", replacing the old "t": "c"/"susertoken" pair that
    never once got past NOT_OK against the previous OAuth-migration-era
    payload.
    """
    client, _, _ = _client()
    ws = _FakeConnection({"t": "ak", "s": "OK"})
    client._authenticate(ws)
    sent = json.loads(ws.sent[0])
    assert sent == {
        "t": "a",
        "uid": "FA1",
        "actid": "FA1",
        "accesstoken": "tok",
        "source": "API",
    }


def test_authenticate_sends_configured_source():
    client, _, _ = _client(source="WEB")
    ws = _FakeConnection({"t": "ak", "s": "OK"})
    client._authenticate(ws)
    sent = json.loads(ws.sent[0])
    assert sent["source"] == "WEB"


def test_authenticate_raises_on_not_ok_ack():
    client, _, _ = _client()
    ws = _FakeConnection({"t": "ak", "s": "NOT_OK"})
    with pytest.raises(ConnectionError):
        client._authenticate(ws)


