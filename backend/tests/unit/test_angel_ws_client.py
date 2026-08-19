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

from app.modules.market_data.providers import angel_ws_client as ws_module
from app.modules.market_data.providers.angel_ws_client import AngelWSClient, _patch_on_close_arity


class _FakeSmartWebSocketV2:
    """Mirrors the real SDK class's public shape closely enough for
    `AngelWSClient` to drive it: `on_open`/`on_data`/`on_close`/`on_error`
    callback attributes, and a blocking `connect()` that fires `on_open`
    immediately (same as the real SDK once its handshake completes) and
    then blocks until `close_connection()` is called from another thread —
    matching `websocket.WebSocketApp.run_forever`'s real blocking contract.
    """

    instances: list[_FakeSmartWebSocketV2] = []
    ROOT_URI = "wss://fake.test/smart-stream"
    HEART_BEAT_INTERVAL = 10

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

    # Thin delegating wrappers, same shape as the real SDK's own
    # underscore-prefixed dispatch methods — needed by AngelWSClient
    # ._connect_via_proxy, which (like the real SDK's own connect()) hands
    # these to websocket.WebSocketApp rather than the public on_open/etc
    # directly. Delegate at call time (not bound at __init__), since
    # AngelWSClient reassigns on_open/on_data/on_close/on_error *after*
    # construction.
    def _on_open(self, wsapp):
        self.on_open(wsapp)

    def _on_data(self, wsapp, data, data_type=2, continue_flag=False):
        self.on_data(wsapp, data)

    def _on_close(self, wsapp):
        self.on_close(wsapp)

    def _on_error(self, wsapp, error):
        self.on_error(wsapp, error)

    def _on_ping(self, wsapp, data):
        pass

    def _on_pong(self, wsapp, data):
        pass


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


def test_on_data_swallows_a_callback_exception_without_dropping_the_connection():
    """2026-08-10 audit finding: an unhandled exception in the on_tick
    callback used to propagate straight up through the SDK's own dispatch
    loop, indistinguishable from a real connection failure. Proves the fix:
    a raising callback must not tear down the WS connection (no reconnect,
    same SDK instance still in place) — only the one bad message is lost.
    """
    def _raising_on_tick(_tick):
        raise ValueError("simulated parsing bug")

    client = AngelWSClient(
        auth_token="jwt1", api_key="key1", client_code="C123", feed_token="feed1",
        on_tick=_raising_on_tick,
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
    time.sleep(0.05)

    # Still exactly one instance -- the exception did not tear the socket
    # down and force a reconnect.
    assert len(_FakeSmartWebSocketV2.instances) == 1
    assert not fake._closed.is_set()  # noqa: SLF001

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


# -- token refresh after persistent reconnect failures (2026-08-11) --------


class _FakeFailingSmartWebSocketV2:
    """`connect()` raises instantly for the first `fail_until` instantiations
    (matching the real observed failure signature — an immediate exception
    at connect/subscribe time, not a hang), then behaves like the normal
    fake from then on. Class-level so the reconnect loop's fresh instance
    per attempt is visible to the test.
    """

    instances: list = []
    fail_until = 0

    def __init__(self, auth_token, api_key, client_code, feed_token, max_retry_attempt=0):
        self.auth_token = auth_token
        self.api_key = api_key
        self.client_code = client_code
        self.feed_token = feed_token
        self.on_open = lambda wsapp: None
        self.on_data = lambda wsapp, data: None
        self.on_close = lambda wsapp: None
        self.on_error = lambda a, b: None
        self._closed = threading.Event()
        _FakeFailingSmartWebSocketV2.instances.append(self)

    def connect(self):
        if len(_FakeFailingSmartWebSocketV2.instances) <= self.fail_until:
            raise ConnectionError("simulated socket already closed")
        self.on_open(self)
        self._closed.wait(timeout=5.0)

    def close_connection(self):
        self._closed.set()


@pytest.fixture
def _fast_backoff(monkeypatch):
    """Real backoff delays (1s, 2s, 5s, ...) would make a test that needs 3+
    consecutive failures take 8+ real seconds — shrink them for this test
    only. `_run` reads the module-level name fresh on each access, so
    patching the module attribute (not a constructor param — there isn't
    one) takes effect immediately.
    """
    monkeypatch.setattr(ws_module, "_RECONNECT_BACKOFF_SECONDS", (0.02,) * 6)


def test_token_refresh_callback_fires_after_three_consecutive_failures(
    monkeypatch, _fast_backoff
):
    _FakeFailingSmartWebSocketV2.instances.clear()
    _FakeFailingSmartWebSocketV2.fail_until = 3  # first 3 attempts fail, 4th succeeds
    import SmartApi.smartWebSocketV2 as sdk_module

    monkeypatch.setattr(sdk_module, "SmartWebSocketV2", _FakeFailingSmartWebSocketV2)

    refresh_calls = 0

    def _refresh():
        nonlocal refresh_calls
        refresh_calls += 1
        return "fresh-jwt", "fresh-feed"

    client = AngelWSClient(
        auth_token="stale-jwt", api_key="key1", client_code="C123", feed_token="stale-feed",
        on_tick=lambda t: None, token_refresh_callback=_refresh,
    )
    client.start()
    time.sleep(0.5)  # 3 failed attempts + backoff, well within 0.5s at 0.02s delays

    assert refresh_calls == 1
    # The 4th (successful) attempt must have been constructed with the
    # refreshed tokens, not the original stale ones.
    assert len(_FakeFailingSmartWebSocketV2.instances) >= 4
    succeeded = _FakeFailingSmartWebSocketV2.instances[3]
    assert succeeded.auth_token == "fresh-jwt"
    assert succeeded.feed_token == "fresh-feed"

    client.stop()
    _FakeFailingSmartWebSocketV2.instances.clear()


def test_no_token_refresh_callback_does_not_break_the_reconnect_loop(monkeypatch, _fast_backoff):
    """Backward-compatible default: every existing caller that doesn't pass
    token_refresh_callback (None) must keep reconnecting exactly as before.
    """
    _FakeFailingSmartWebSocketV2.instances.clear()
    _FakeFailingSmartWebSocketV2.fail_until = 3
    import SmartApi.smartWebSocketV2 as sdk_module

    monkeypatch.setattr(sdk_module, "SmartWebSocketV2", _FakeFailingSmartWebSocketV2)

    client = AngelWSClient(
        auth_token="jwt1", api_key="key1", client_code="C123", feed_token="feed1",
        on_tick=lambda t: None,
    )
    client.start()
    time.sleep(0.5)

    assert len(_FakeFailingSmartWebSocketV2.instances) >= 4  # kept retrying past the failures

    client.stop()
    _FakeFailingSmartWebSocketV2.instances.clear()


def test_token_refresh_callback_failure_is_swallowed_and_retries_with_old_tokens(
    monkeypatch, _fast_backoff
):
    _FakeFailingSmartWebSocketV2.instances.clear()
    _FakeFailingSmartWebSocketV2.fail_until = 100  # never succeeds -- only the refresh matters
    import SmartApi.smartWebSocketV2 as sdk_module

    monkeypatch.setattr(sdk_module, "SmartWebSocketV2", _FakeFailingSmartWebSocketV2)

    def _raising_refresh():
        raise RuntimeError("REST login failed")

    client = AngelWSClient(
        auth_token="jwt1", api_key="key1", client_code="C123", feed_token="feed1",
        on_tick=lambda t: None, token_refresh_callback=_raising_refresh,
    )
    client.start()
    time.sleep(0.3)

    # Still using the original tokens -- the failed refresh must not corrupt
    # them, and the reconnect loop itself must survive the callback raising.
    assert client._auth_token == "jwt1"  # noqa: SLF001
    assert client._feed_token == "feed1"  # noqa: SLF001
    assert len(_FakeFailingSmartWebSocketV2.instances) >= 3  # kept retrying, did not crash

    client.stop()
    _FakeFailingSmartWebSocketV2.instances.clear()


# -- proxy routing for the WS connection (2026-08-11, live-confirmed finding) --


class _FakeRawWebSocketApp:
    """Stands in for `websocket.WebSocketApp` itself — the layer
    `_connect_via_proxy` drives directly (bypassing `SmartWebSocketV2
    .connect()`, which has no proxy support at all). Records what
    `run_forever` was actually called with, and fires `on_open` once so
    `AngelWSClient`'s own resubscribe-on-open logic still exercises
    normally.
    """

    instances: list[_FakeRawWebSocketApp] = []

    def __init__(
        self, url, header=None, on_open=None, on_error=None, on_close=None,
        on_data=None, on_ping=None, on_pong=None,
    ):
        self.url = url
        self.header = header
        self.on_open = on_open
        self.on_error = on_error
        self.on_close = on_close
        self.on_data = on_data
        self.run_forever_kwargs: dict | None = None
        self._closed = threading.Event()
        _FakeRawWebSocketApp.instances.append(self)

    def run_forever(self, **kwargs):
        self.run_forever_kwargs = kwargs
        if self.on_open is not None:
            self.on_open(self)
        self._closed.wait(timeout=5.0)

    def close(self):
        self._closed.set()


def test_proxy_url_routes_through_websocket_client_native_proxy_support(monkeypatch):
    """The core of the 2026-08-11 fix: when a proxy is configured, the
    connection must go through `websocket.WebSocketApp`'s own native proxy
    support with the parsed host/port/auth — not `SmartWebSocketV2.connect()`,
    which silently ignores proxy settings entirely (it has no parameter for
    one at all).
    """
    import websocket as websocket_module

    _FakeRawWebSocketApp.instances.clear()
    monkeypatch.setattr(websocket_module, "WebSocketApp", _FakeRawWebSocketApp)

    client = AngelWSClient(
        auth_token="jwt1", api_key="key1", client_code="C123", feed_token="feed1",
        on_tick=lambda t: None,
        proxy_url="http://proxyuser:proxypass@31.59.20.176:6754",
    )
    client.start()
    time.sleep(0.1)

    assert len(_FakeRawWebSocketApp.instances) == 1
    fake_app = _FakeRawWebSocketApp.instances[0]
    assert fake_app.run_forever_kwargs is not None
    assert fake_app.run_forever_kwargs["http_proxy_host"] == "31.59.20.176"
    assert fake_app.run_forever_kwargs["http_proxy_port"] == 6754
    assert fake_app.run_forever_kwargs["http_proxy_auth"] == ("proxyuser", "proxypass")
    assert fake_app.run_forever_kwargs["proxy_type"] == "http"
    # Headers still carry the real auth -- routing through the proxy must
    # not drop or alter what actually authenticates the session.
    assert fake_app.header["Authorization"] == "jwt1"
    assert fake_app.header["x-feed-token"] == "feed1"

    client.stop()
    _FakeRawWebSocketApp.instances.clear()


def test_no_proxy_url_still_uses_the_sdks_own_connect(monkeypatch):
    """Backward-compatible default: every existing caller that doesn't pass
    proxy_url (empty string) must keep using SmartWebSocketV2.connect()
    directly, never touching the proxy-routing path at all.
    """
    import websocket as websocket_module

    _FakeRawWebSocketApp.instances.clear()
    monkeypatch.setattr(websocket_module, "WebSocketApp", _FakeRawWebSocketApp)

    client = AngelWSClient(
        auth_token="jwt1", api_key="key1", client_code="C123", feed_token="feed1",
        on_tick=lambda t: None,
    )
    client.start()
    time.sleep(0.05)

    assert _FakeRawWebSocketApp.instances == []  # proxy path never touched
    assert len(_FakeSmartWebSocketV2.instances) == 1  # went through connect() as before

    client.stop()


# -- _on_close arity compatibility (2026-08-19) -----------------------------


class _RealShapedSDK:
    """Mirrors the real SmartWebSocketV2._on_close's exact 2-arg signature
    (self, wsapp) -- unlike _FakeSmartWebSocketV2 above, this class is never
    monkeypatched into SmartApi.smartWebSocketV2; it exists only to test
    _patch_on_close_arity in isolation, against a class shaped exactly like
    the real SDK, not the test harness's own fake.
    """

    def __init__(self) -> None:
        self.close_calls: list[object] = []
        self.on_close = lambda wsapp: None

    def _on_close(self, wsapp: object) -> None:
        self.on_close(wsapp)


def test_patch_on_close_arity_tolerates_the_extra_args_websocket_client_passes():
    """websocket-client==1.9.0 really does invoke on_close as
    callback(self, close_status_code, close_reason) -- 3 args -- confirmed
    directly against its installed _callback source. Before this patch,
    SmartWebSocketV2._on_close(self, wsapp) TypeErrors on every real close.
    """
    _patch_on_close_arity(_RealShapedSDK)
    instance = _RealShapedSDK()
    seen: list[object] = []
    instance.on_close = seen.append

    instance._on_close("some-wsapp", 1000, "normal closure")  # noqa: SLF001

    assert seen == ["some-wsapp"]


def test_patch_on_close_arity_is_idempotent():
    """Applied once per SmartWebSocketV2() construction (every reconnect
    attempt) via _connect_and_run, not once at import time -- must not
    double-wrap and call the original twice."""
    calls: list[object] = []

    class _Sdk:
        def _on_close(self, wsapp: object) -> None:
            calls.append(wsapp)

    _patch_on_close_arity(_Sdk)
    _patch_on_close_arity(_Sdk)
    _patch_on_close_arity(_Sdk)

    _Sdk()._on_close("wsapp-1", 1000, "normal closure")  # noqa: SLF001

    assert calls == ["wsapp-1"]


def test_connect_and_run_patches_the_real_import_targets_on_close():
    """Integration-level proof that _connect_and_run actually calls the
    patch against whatever SmartWebSocketV2 it imports -- not just that the
    patch function works in isolation."""
    client = AngelWSClient(
        auth_token="jwt1", api_key="key1", client_code="C123", feed_token="feed1",
        on_tick=lambda t: None,
    )
    client.start()
    time.sleep(0.05)

    assert getattr(_FakeSmartWebSocketV2._on_close, "_arity_patched", False) is True

    client.stop()
