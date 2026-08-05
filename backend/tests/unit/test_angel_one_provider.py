"""Unit tests for the Angel One market-data provider stack. Raw binary tick
unpacking is delegated to `smartapi-python` (not this codebase's own code —
see `angel_ws_client.py`'s own docstring), so these tests focus on what this
codebase actually owns: translating the SDK's already-parsed dicts into our
internal dataclasses, constructing the SDK client correctly, our own
reconnect/backoff supervision layer, symbol resolution via a fake
`ScripMasterService`, and the REST login call/response handling. Fakes
mirror `test_shoonya_adapter.py`'s `_FakeRestClient` / `test_shoonya_ws_client.py`
seam-injection style.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import AngelOneSettings
from app.modules.broker_adapter.base.errors import BrokerConnectivityError
from app.modules.market_data.providers import angel_one
from app.modules.market_data.providers.angel_one import AngelOneMarketDataProvider
from app.modules.market_data.providers.angel_rest_client import (
    AngelOneLoginError,
    AngelOneRestClient,
)
from app.modules.market_data.providers.angel_ws_client import (
    parse_angel_depth,
    parse_angel_tick,
)

SETTINGS = AngelOneSettings(
    api_key="key1",
    client_code="C123",
    password=SecretStr("pin"),
    totp_secret=SecretStr("JBSWY3DPEHPK3PXP"),  # a valid base32 TOTP secret
    mac_address="AA:BB:CC:DD:EE:FF",
)

SNAP_QUOTE_TICK = {
    "subscription_mode": 3,
    "exchange_type": 2,
    "token": "58784",
    "sequence_number": 1,
    "exchange_timestamp": 1735000000000,
    "last_traded_price": 2456785,  # -> 24567.85
    "last_traded_quantity": 75,
    "average_traded_price": 2450000,
    "volume_trade_for_the_day": 125000,
    "total_buy_quantity": 1000.0,
    "total_sell_quantity": 900.0,
    "open_price_of_the_day": 2440000,
    "high_price_of_the_day": 2460000,
    "low_price_of_the_day": 2430000,
    "closed_price": 2445000,
    "last_traded_timestamp": 1735000000,
    "open_interest": 98000,
    "open_interest_change_percentage": 1.5,
    "best_5_buy_data": [{"flag": 0, "quantity": 75, "price": 2456500, "no of orders": 3}],
    "best_5_sell_data": [{"flag": 1, "quantity": 75, "price": 2457000, "no of orders": 2}],
}


# -- parse_angel_tick / parse_angel_depth --------------------------------------


def test_parse_angel_tick_converts_paise_to_rupees():
    """Confirmed directly from the installed SDK source: SNAP_QUOTE price
    fields are raw integers requiring /100 to get real rupees.
    """
    tick = parse_angel_tick(SNAP_QUOTE_TICK)
    assert tick is not None
    assert tick.token == "58784"
    assert tick.ltp == 24567.85
    assert tick.bid == 24565.00
    assert tick.ask == 24570.00
    assert tick.volume == 125000
    assert tick.oi == 98000


def test_parse_angel_tick_falls_back_to_ltp_when_no_best5_data():
    raw = {**SNAP_QUOTE_TICK, "best_5_buy_data": [], "best_5_sell_data": []}
    tick = parse_angel_tick(raw)
    assert tick is not None
    assert tick.bid == tick.ask == tick.ltp


def test_parse_angel_tick_returns_none_for_a_control_message():
    assert parse_angel_tick({"subscription_mode": 0}) is None


def test_parse_angel_depth_reads_best_5_levels():
    depth = parse_angel_depth(SNAP_QUOTE_TICK)
    assert depth is not None
    assert depth.token == "58784"
    assert depth.bid_levels[0].price == 24565.00
    assert depth.bid_levels[0].qty == 75
    assert depth.ask_levels[0].price == 24570.00


def test_parse_angel_depth_returns_none_without_best5_data():
    raw = {"token": "1"}
    assert parse_angel_depth(raw) is None


# -- AngelOneRestClient.login_by_password ----------------------------------------


def _rest_client_with_transport(handler) -> AngelOneRestClient:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return AngelOneRestClient(
        "https://apiconnect.angelone.in",
        api_key="key1",
        mac_address="AA:BB:CC:DD:EE:FF",
        http_client=http_client,
    )


def test_login_by_password_returns_data_on_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-PrivateKey"] == "key1"
        assert request.headers["X-MACAddress"] == "AA:BB:CC:DD:EE:FF"
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {"jwtToken": "jwt1", "refreshToken": "ref1", "feedToken": "feed1"},
            },
        )

    client = _rest_client_with_transport(handler)
    data = client.login_by_password("C123", "pin", "123456")
    assert data["jwtToken"] == "jwt1"
    assert data["feedToken"] == "feed1"


def test_login_by_password_raises_on_rejected_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": False, "message": "Invalid TOTP", "errorcode": "AB1010"}
        )

    client = _rest_client_with_transport(handler)
    with pytest.raises(AngelOneLoginError, match="Invalid TOTP"):
        client.login_by_password("C123", "pin", "000000")


def test_login_by_password_raises_connectivity_error_on_network_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _rest_client_with_transport(handler)
    with pytest.raises(BrokerConnectivityError):
        client.login_by_password("C123", "pin", "000000")


def test_login_by_password_raises_connectivity_not_auth_error_on_proxy_failure():
    """A proxy being unreachable is "retry next cycle," never "credentials
    are dead" — misclassifying it as BrokerAuthError would risk
    PositionManager._handle_broker_auth_error firing a spurious degraded_mode
    transition on a guarded-live/live session over a transient relay blip.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ProxyError("proxy unreachable")

    client = _rest_client_with_transport(handler)
    with pytest.raises(BrokerConnectivityError):
        client.login_by_password("C123", "pin", "000000")


def test_auth_proxy_is_passed_to_the_default_http_client(monkeypatch):
    captured: dict = {}
    real_client_cls = httpx.Client

    def _spy_client(*args, **kwargs):
        captured.update(kwargs)
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _spy_client)

    AngelOneRestClient(
        "https://apiconnect.angelone.in",
        api_key="key1",
        mac_address="AA:BB:CC:DD:EE:FF",
        auth_proxy="http://relay.example.internal:8080",
    )

    assert captured.get("proxy") == "http://relay.example.internal:8080"


def test_no_auth_proxy_means_a_plain_direct_client(monkeypatch):
    captured: dict = {}
    real_client_cls = httpx.Client

    def _spy_client(*args, **kwargs):
        captured.update(kwargs)
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _spy_client)

    AngelOneRestClient(
        "https://apiconnect.angelone.in", api_key="key1", mac_address="AA:BB:CC:DD:EE:FF"
    )

    assert captured.get("proxy") is None


# -- AngelOneMarketDataProvider --------------------------------------------------


class _FakeRestClient:
    def __init__(self):
        self.login_calls: list[tuple] = []

    def login_by_password(self, client_code, password, totp):
        self.login_calls.append((client_code, password, totp))
        return {"jwtToken": "jwt1", "refreshToken": "ref1", "feedToken": "feed1"}


class _FakeScripMaster:
    def __init__(self):
        self.tokens = {"NIFTY30JUL2624000CE": "111", "NIFTY": "26000"}
        self.exchanges = {"NIFTY30JUL2624000CE": "NFO", "NIFTY": "NSE"}
        self.reverse = {v: k for k, v in self.tokens.items()}

    def get_angel_token(self, symbol):
        return self.tokens.get(symbol)

    def get_angel_exchange_segment(self, symbol):
        return self.exchanges.get(symbol)

    def get_symbol_for_angel_token(self, token):
        return self.reverse.get(token)


class _FakeWSClient:
    """Stands in for AngelWSClient — records subscribe/unsubscribe calls,
    lets the test manually fire ticks/depth via the provider's own
    callbacks rather than opening a real connection.
    """

    instances: list[_FakeWSClient] = []

    def __init__(self, *, auth_token, api_key, client_code, feed_token, on_tick, on_depth=None):
        self.auth_token = auth_token
        self.api_key = api_key
        self.client_code = client_code
        self.feed_token = feed_token
        self.on_tick = on_tick
        self.on_depth = on_depth
        self.started = False
        self.subscribed: list[tuple] = []
        self.unsubscribed: list[tuple] = []
        _FakeWSClient.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def subscribe(self, entries):
        self.subscribed.extend(entries)

    def unsubscribe(self, entries):
        self.unsubscribed.extend(entries)


@pytest.fixture(autouse=True)
def _reset_fake_ws_instances():
    _FakeWSClient.instances.clear()
    yield
    _FakeWSClient.instances.clear()


@pytest.fixture
def provider(monkeypatch) -> AngelOneMarketDataProvider:
    monkeypatch.setattr(angel_one, "AngelWSClient", _FakeWSClient)
    rest = _FakeRestClient()
    return AngelOneMarketDataProvider(SETTINGS, _FakeScripMaster(), rest_client=rest)  # type: ignore[arg-type]


def test_connect_generates_a_live_totp_and_stores_tokens(provider: AngelOneMarketDataProvider):
    provider.connect()
    assert provider._feed_token == "feed1"  # noqa: SLF001
    assert provider._jwt_token == "jwt1"  # noqa: SLF001
    rest: _FakeRestClient = provider._rest  # type: ignore[assignment]  # noqa: SLF001
    assert rest.login_calls[0][0] == "C123"
    assert len(rest.login_calls[0][2]) == 6  # a live 6-digit TOTP code


def test_connect_is_idempotent(provider: AngelOneMarketDataProvider):
    provider.connect()
    provider.connect()
    rest: _FakeRestClient = provider._rest  # type: ignore[assignment]  # noqa: SLF001
    assert len(rest.login_calls) == 1


def test_subscribe_ticks_resolves_tokens_and_subscribes(provider: AngelOneMarketDataProvider):
    received: list = []
    provider.subscribe_ticks(["NIFTY30JUL2624000CE", "NIFTY"], on_tick=received.append)

    ws = _FakeWSClient.instances[0]
    assert ws.started
    assert ("111", 2) in ws.subscribed  # NFO exchange type
    assert ("26000", 1) in ws.subscribed  # NSE exchange type


def test_subscribe_ticks_skips_unmapped_symbols_without_raising(
    provider: AngelOneMarketDataProvider,
):
    provider.subscribe_ticks(["UNKNOWN-SYMBOL"], on_tick=lambda t: None)
    ws = _FakeWSClient.instances[0]
    assert ws.subscribed == []


def test_incoming_tick_updates_latest_tick_cache_and_forwards_to_callback(
    provider: AngelOneMarketDataProvider,
):
    received: list = []
    provider.subscribe_ticks(["NIFTY30JUL2624000CE"], on_tick=received.append)
    ws = _FakeWSClient.instances[0]

    from app.modules.market_data.providers.angel_ws_client import RawAngelTick

    raw_tick = RawAngelTick(
        token="111", ltp=120.5, bid=120.0, ask=121.0, volume=500, oi=1000, ts=datetime.now(UTC)
    )
    ws.on_tick(raw_tick)

    assert len(received) == 1
    assert received[0].contract_symbol == "NIFTY30JUL2624000CE"
    assert received[0].ltp == 120.5

    cached = provider.get_latest_tick("NIFTY30JUL2624000CE")
    assert cached is not None
    assert cached.ltp == 120.5


def test_unsubscribe_ticks_clears_the_latest_tick_cache(provider: AngelOneMarketDataProvider):
    from app.modules.market_data.providers.angel_ws_client import RawAngelTick

    provider.subscribe_ticks(["NIFTY30JUL2624000CE"], on_tick=lambda t: None)
    ws = _FakeWSClient.instances[0]
    ws.on_tick(
        RawAngelTick(
            token="111", ltp=100.0, bid=99.5, ask=100.5, volume=10, oi=None, ts=datetime.now(UTC)
        )
    )
    assert provider.get_latest_tick("NIFTY30JUL2624000CE") is not None

    provider.unsubscribe_ticks(["NIFTY30JUL2624000CE"])

    assert provider.get_latest_tick("NIFTY30JUL2624000CE") is None
    assert ("111", 2) in ws.unsubscribed


def test_get_latest_tick_returns_none_for_never_subscribed_symbol(
    provider: AngelOneMarketDataProvider,
):
    assert provider.get_latest_tick("NEVER-SUBSCRIBED") is None
