from __future__ import annotations

from datetime import date

import pytest
from pydantic import SecretStr

from app.config.settings import ShoonyaSettings
from app.modules.broker_adapter.base.contracts import (
    AuthResult,
    OrderRequest,
    OrderSide,
    OrderType,
)
from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter
from app.modules.broker_adapter.shoonya.rest_client import ShoonyaApiError

SETTINGS = ShoonyaSettings(
    client_id="X",
    secret_code=SecretStr("Y"),
    user_id="FA1",
    api_host="https://api.shoonya.test/NorenWClientAPI",
    ws_host="wss://api.shoonya.test/NorenWSAPI/",
)
AUTH_RESULT = AuthResult(session_token="tok", account_id="FA1")


class _FakeRestClient:
    """Records calls and returns canned responses — the adapter's own
    tests care about *its* logic (idempotency, token caching, status
    follow-up), not the REST wire format, which `test_shoonya_rest_client.py`
    already covers directly.
    """

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self.place_order_response: dict = {"norenordno": "ORD1", "status": "COMPLETE"}
        self.order_status_response: list[dict] = [{"norenordno": "ORD1", "status": "COMPLETE"}]
        self.search_scrip_response: list[dict] = []

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def search_scrip(self, uid, exchange, search_text):
        self._record("search_scrip", uid, exchange, search_text)
        return self.search_scrip_response

    def get_quotes(self, uid, exchange, token):
        self._record("get_quotes", uid, exchange, token)
        return {"lp": "100.0", "bp1": "99.5", "sp1": "100.5", "v": "10", "oi": "5"}

    def get_option_chain(self, uid, exchange, tradingsymbol, strike_price, count=10):
        self._record("get_option_chain", uid, exchange, tradingsymbol, strike_price)
        return []

    def place_order(self, payload):
        self._record("place_order", payload)
        return self.place_order_response

    def modify_order(self, payload):
        self._record("modify_order", payload)
        return {"norenordno": "ORD1", "status": "OPEN"}

    def cancel_order(self, uid, broker_order_id):
        self._record("cancel_order", uid, broker_order_id)
        return {"norenordno": broker_order_id, "status": "CANCELED"}

    def order_book(self, uid):
        self._record("order_book", uid)
        return []

    def single_order_history(self, uid, broker_order_id):
        self._record("single_order_history", uid, broker_order_id)
        return self.order_status_response

    def position_book(self, uid, actid):
        self._record("position_book", uid, actid)
        return [{"tsym": "NIFTY30JUL2624000CE", "netqty": "25", "netavgprc": "119.5"}]

    def close(self):
        pass


def _adapter(
    rest_client: _FakeRestClient | None = None,
) -> tuple[ShoonyaBrokerAdapter, _FakeRestClient]:
    rest = rest_client or _FakeRestClient()
    adapter = ShoonyaBrokerAdapter(SETTINGS, AUTH_RESULT, rest_client=rest)  # type: ignore[arg-type]
    return adapter, rest


def test_authenticate_returns_the_held_auth_result():
    adapter, _ = _adapter()
    assert adapter.authenticate() == AUTH_RESULT


def test_get_instrument_master_queries_known_underlyings_only():
    rest = _FakeRestClient()
    rest.search_scrip_response = [
        {"tsym": "NIFTY", "ls": "25", "ti": "0.05", "token": "26000", "instname": "IDX"}
    ]
    adapter, _ = _adapter(rest)

    infos = adapter.get_instrument_master("NFO")

    searched_underlyings = [call[1][2] for call in rest.calls if call[0] == "search_scrip"]
    assert searched_underlyings == ["NIFTY", "BANKNIFTY"]
    assert len(infos) == 2  # one row returned per search call in this fake
    assert infos[0].symbol == "NIFTY"


def test_get_quote_requires_resolved_token_first():
    adapter, _ = _adapter()
    with pytest.raises(ShoonyaApiError, match="no cached broker token"):
        adapter.get_quote("NIFTY30JUL2624000CE")


def test_get_quote_after_instrument_master_resolves_token():
    rest = _FakeRestClient()
    rest.search_scrip_response = [
        {
            "tsym": "NIFTY30JUL2624000CE",
            "ls": "25",
            "ti": "0.05",
            "token": "12345",
            "instname": "OPTIDX",
            "symname": "NIFTY",
            "exd": "30-JUL-2026",
            "strprc": "24000",
            "optt": "CE",
        }
    ]
    adapter, _ = _adapter(rest)
    adapter.get_instrument_master("NFO")

    tick = adapter.get_quote("NIFTY30JUL2624000CE")
    assert tick.ltp == 100.0


def test_place_order_is_idempotent_on_key():
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    request = OrderRequest(
        idempotency_key="key-1",
        contract_symbol="NIFTY30JUL2624000CE",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        qty=25,
        limit_price=120.0,
    )

    first = adapter.place_order(request)
    second = adapter.place_order(request)

    assert first == second
    place_order_calls = [c for c in rest.calls if c[0] == "place_order"]
    assert len(place_order_calls) == 1, "second call must not hit the broker again"


def test_place_order_follows_up_with_status_when_pending():
    rest = _FakeRestClient()
    rest.place_order_response = {"norenordno": "ORD1"}  # no status -> normalizer defaults PENDING
    rest.order_status_response = [{"norenordno": "ORD1", "status": "COMPLETE", "fillshares": "25"}]
    adapter, _ = _adapter(rest)

    request = OrderRequest(
        idempotency_key="key-2",
        contract_symbol="NIFTY30JUL2624000CE",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        qty=25,
    )
    result = adapter.place_order(request)

    assert result.status.value == "filled"
    assert any(c[0] == "single_order_history" for c in rest.calls)


def test_get_positions_maps_all_rows():
    adapter, _ = _adapter()
    positions = adapter.get_positions()
    assert len(positions) == 1
    assert positions[0].qty == 25


def test_get_option_chain_filters_by_expiry():
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    # No cached token for NIFTY yet -> falls back to search_scrip internally.
    rest.search_scrip_response = [{"tsym": "NIFTY", "token": "26000"}]

    snapshot = adapter.get_option_chain("NIFTY", date(2026, 7, 30))
    assert snapshot.underlying == "NIFTY"
    assert snapshot.entries == ()
