"""`AngelWSClient`'s own connection-supervision layer — the part of the
Angel One integration this codebase actually owns (binary tick unpacking is
delegated to `smartapi-python`, see that module's own docstring). A fake
`SmartWebSocketV2` is monkeypatched into `SmartApi.smartWebSocketV2` (the
exact module `AngelWSClient._connect_and_run` imports from, locally, on
every connection attempt) so no real network call ever happens.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.modules.market_data.providers.angel_ws_client import AngelWSClient


class _FakeSmartWebSocketV2:
    """Mirrors the real SDK class's public shape closely enough for
    `AngelWSClient` to drive it: `on_open`/`on_data`/`on_close`/`on_error`
    callback attributes, and a blocking `connect()` that fires `on_open`
    immediately (same as the real SDK once its handshake completes) and
    then blocks until `close_connection()` is called from another thread —
    matching `websocket.WebSocketApp.run_forever`'s real blocking contract.
    """

    instances: list[_FakeSmartWebSocketV2] = []

    def __init__(self, auth_token, api_key, client_code, feed_token, max_retry_attempt=0):
        self.auth_token = auth_token
        self.api_key = api_key
        self.client_code = client_code
        self.feed_token = feed_token
        self.on_open = lambda wsapp: None
        self.on_data = lambda wsapp, data: None
        self.on_close = lambda wsapp: None
        self.on_error = lambda a, b: None
        self.subscribe_calls: list[tuple] = []
        self.unsubscribe_calls: list[tuple] = []
        self._closed = threading.Event()
        _FakeSmartWebSocketV2.instances.append(self)

    def subscribe(self, correlation_id, mode, token_list):
        self.subscribe_calls.append((correlation_id, mode, token_list))

    def unsubscribe(self, correlation_id, mode, token_list):
        self.unsubscribe_calls.append((correlation_id, mode, token_list))

    def connect(self):
        self.on_open(self)
        self._closed.wait(timeout=5.0)

    def close_connection(self):
        self._closed.set()


@pytest.fixture(autouse=True)
def _patch_sdk(monkeypatch):
    _FakeSmartWebSocketV2.instances.clear()
    import SmartApi.smartWebSocketV2 as sdk_module

    monkeypatch.setattr(sdk_module, "SmartWebSocketV2", _FakeSmartWebSocketV2)
    yield
    _FakeSmartWebSocketV2.instances.clear()


def test_start_connects_and_resubscribes_queued_entries():
    received: list = []
    client = AngelWSClient(
        auth_token="jwt1", api_key="key1", client_code="C123", feed_token="feed1",
        on_tick=received.append,
    )
    client.subscribe([("111", 2)])  # queued before connect, per ShoonyaWSClient's own contract

    client.start()
    time.sleep(0.05)

    fake = _FakeSmartWebSocketV2.instances[0]
    assert fake.subscribe_calls, "queued subscription must be replayed once connected"
    tokens_sent = fake.subscribe_calls[0][2]
    assert tokens_sent == [{"exchangeType": 2, "tokens": ["111"]}]

    client.stop()


def test_on_data_dispatches_a_snap_quote_tick_to_the_callback():
    received: list = []
    client = AngelWSClient(
        auth_token="jwt1", api_key="key1", client_code="C123", feed_token="feed1",
        on_tick=received.append,
    )
    client.start()
    time.sleep(0.05)
    fake = _FakeSmartWebSocketV2.instances[0]

    fake.on_data(
        fake,
        {
            "subscription_mode": 3,
            "token": "111",
            "last_traded_price": 12345,
            "exchange_timestamp": 1735000000000,
            "volume_trade_for_the_day": 10,
            "open_interest": 500,
            "best_5_buy_data": [],
            "best_5_sell_data": [],
        },
    )

    assert len(received) == 1
    assert received[0].token == "111"
    assert received[0].ltp == 123.45

    client.stop()


def test_reconnects_after_close_with_a_fresh_sdk_instance():
    client = AngelWSClient(
        auth_token="jwt1", api_key="key1", client_code="C123", feed_token="feed1",
        on_tick=lambda t: None,
    )
    client.start()
    time.sleep(0.05)
    assert len(_FakeSmartWebSocketV2.instances) == 1

    _FakeSmartWebSocketV2.instances[0].close_connection()
    # Capped backoff's first delay is 1s (_RECONNECT_BACKOFF_SECONDS[0]) —
    # wait past it for the outer loop to reconnect with a brand-new instance.
    time.sleep(1.3)

    assert len(_FakeSmartWebSocketV2.instances) == 2
    client.stop()
