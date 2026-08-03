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
        # Keyed by exchange — get_option_chain now searches both NSE (the
        # underlying's own token, for later quote/subscribe use) and NFO
        # (a real futures contract to anchor the GetOptionChain call
        # itself), so a single flat canned response isn't enough once both
        # are exercised in the same test.
        self.search_scrip_response_by_exchange: dict[str, list[dict]] = {}
        self.get_quotes_response: dict = {
            "lp": "100.0",
            "bp1": "99.5",
            "sp1": "100.5",
            "v": "10",
            "oi": "5",
        }

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def search_scrip(self, uid, exchange, search_text):
        self._record("search_scrip", uid, exchange, search_text)
        if exchange in self.search_scrip_response_by_exchange:
            return self.search_scrip_response_by_exchange[exchange]
        return self.search_scrip_response

    def get_quotes(self, uid, exchange, token):
        self._record("get_quotes", uid, exchange, token)
        return self.get_quotes_response

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


def _configure_search_scrip_for_option_chain(rest: _FakeRestClient) -> None:
    """Both underlying-token resolution (NSE) and futures-anchor resolution
    (NFO) run during get_option_chain now — see the three "live-corrected"
    rounds documented on `_resolve_underlying_token`/`_resolve_futures_anchor_tsym`.
    """
    rest.search_scrip_response_by_exchange["NSE"] = [{"tsym": "Nifty 50", "token": "26000"}]
    rest.search_scrip_response_by_exchange["NFO"] = [
        {
            "tsym": "NIFTY28AUG25F",
            "instname": "FUTIDX",
            "symname": "NIFTY",
            "exd": "28-AUG-2025",
            "token": "12345",
        }
    ]


def test_get_option_chain_filters_by_expiry():
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    _configure_search_scrip_for_option_chain(rest)

    snapshot = adapter.get_option_chain("NIFTY", date(2026, 7, 30))
    assert snapshot.underlying == "NIFTY"
    assert snapshot.entries == ()


def test_get_option_chain_resolves_underlying_token_on_nse_not_nfo():
    """Live-corrected against a real account: NFO only lists derivative
    contracts on NIFTY/BANKNIFTY (e.g. NIFTY28AUG25F), never a bare "NIFTY"
    tsym — only NSE (the cash/index segment) has the underlying itself.
    Even on NSE, the index's own tsym isn't the bare underlying name —
    it's "Nifty 50", confirmed via a live diagnostic log of real
    search_scrip results that also included "Nifty Bank", "Nifty Next 50",
    and a dozen unrelated NIFTYxxx-EQ ETF tickers (see
    `_UNDERLYING_INDEX_TSYM`'s own docstring).
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NSE"] = [
        {"tsym": "Nifty Bank", "token": "99999"},
        {"tsym": "Nifty 50", "token": "26000"},
    ]
    rest.search_scrip_response_by_exchange["NFO"] = [
        {
            "tsym": "NIFTY28AUG25F",
            "instname": "FUTIDX",
            "symname": "NIFTY",
            "exd": "28-AUG-2025",
            "token": "12345",
        }
    ]

    adapter.get_option_chain("NIFTY", date(2026, 7, 30))

    search_calls = [call[1] for call in rest.calls if call[0] == "search_scrip"]
    assert ("FA1", "NSE", "NIFTY") in search_calls


def test_resolve_underlying_token_searches_nse_with_fixed_nifty_anchor_for_banknifty():
    """Live-corrected a third time: searching NSE with search_text=
    "BANKNIFTY" (the underlying's own name) returned only an unrelated ETF
    ticker ("BANKNIFTY1-EQ") — "Nifty Bank" never appeared, confirmed via a
    live diagnostic log. Both known display-style tsyms share the "Nifty"
    prefix, so the search text is now always the fixed anchor "NIFTY",
    never the underlying's own name — this is what actually finds
    "Nifty Bank" for BANKNIFTY.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NSE"] = [
        {"tsym": "BANKNIFTY1-EQ", "token": "1"},
        {"tsym": "Nifty Bank", "token": "88888"},
        {"tsym": "Nifty 50", "token": "26000"},
    ]

    exchange, token = adapter._resolve_underlying_token("BANKNIFTY")

    assert (exchange, token) == ("NSE", "88888")
    search_calls = [call[1] for call in rest.calls if call[0] == "search_scrip"]
    assert search_calls == [("FA1", "NSE", "NIFTY")]


def test_get_option_chain_uses_nearest_futures_contract_as_anchor():
    """Live-corrected three times against a real account: "NIFTY",
    "Nifty 50", and quote_plus-encoded "Nifty+50" were all rejected by
    GetOptionChain as "Invalid Trading Symbol" — Shoonya's own docs define
    `tsym` there as "Trading symbol of any of the option or future," so it
    needs a real contract, not any form of the index name. Picks the
    nearest-expiry NFO futures contract on the underlying (matched via
    `instname` startswith `FUT` + `symname`, not fragile tsym pattern
    matching) as the anchor.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NSE"] = [{"tsym": "Nifty 50", "token": "26000"}]
    rest.search_scrip_response_by_exchange["NFO"] = [
        # A stock future that merely contains "NIFTY" in its name (decoy —
        # wrong symname) plus two real NIFTY index futures at different
        # expiries, to prove both the symname filter and nearest-expiry
        # selection are doing real work, not just picking whatever's first.
        {
            "tsym": "NIFTYBEES28AUG25F",
            "instname": "FUTSTK",
            "symname": "NIFTYBEES",
            "exd": "28-AUG-2025",
            "token": "1",
        },
        {
            "tsym": "NIFTY25SEP25F",
            "instname": "FUTIDX",
            "symname": "NIFTY",
            "exd": "25-SEP-2025",
            "token": "2",
        },
        {
            "tsym": "NIFTY28AUG25F",
            "instname": "FUTIDX",
            "symname": "NIFTY",
            "exd": "28-AUG-2025",
            "token": "3",
        },
    ]

    adapter.get_option_chain("NIFTY", date(2026, 7, 30))

    search_calls = [call[1] for call in rest.calls if call[0] == "search_scrip"]
    assert ("FA1", "NFO", "NIFTY") in search_calls
    chain_calls = [call[1] for call in rest.calls if call[0] == "get_option_chain"]
    assert chain_calls[0][2] == "NIFTY28AUG25F"
    assert chain_calls[0][1] == "NFO"


def test_resolve_futures_anchor_tsym_caches_within_expiry():
    """The futures anchor almost never changes within a day (`get_option_chain`
    runs on every periodic freshness-gate refresh, not just at strategy
    start) — a fresh NFO SearchScrip call every time was pure waste. A
    second call for the same underlying, still before the cached contract's
    own expiry, must reuse the cached tsym rather than searching again.

    Uses a real relative-future expiry (`today + 30 days`), not a hardcoded
    date string — a hardcoded past-looking date is exactly the class of
    wall-clock-dependent test trap this codebase has hit before (see
    CLAUDE.md's `TradingSession.cutoff_time` note), and this fixture data
    would otherwise silently start failing once real time caught up to it.
    """
    from datetime import timedelta

    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    future_expiry = date.today() + timedelta(days=30)
    rest.search_scrip_response_by_exchange["NFO"] = [
        {
            "tsym": "NIFTY28AUG25F",
            "instname": "FUTIDX",
            "symname": "NIFTY",
            "exd": future_expiry.strftime("%d-%b-%Y").upper(),
            "token": "3",
        },
    ]

    first = adapter._resolve_futures_anchor_tsym("NIFTY")
    second = adapter._resolve_futures_anchor_tsym("NIFTY")

    assert first == second == "NIFTY28AUG25F"
    search_calls = [call for call in rest.calls if call[0] == "search_scrip"]
    assert len(search_calls) == 1


def test_resolve_futures_anchor_tsym_refetches_once_cached_contract_expires():
    """A naive forever-cache would be a real, delayed bug: the cached
    contract eventually expires and stops being a valid GetOptionChain
    anchor. Simulates rollover by injecting an already-expired cache entry
    directly, then confirms a stale cache is never trusted — it re-resolves
    against live data instead of silently returning the expired symbol.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NFO"] = [
        {
            "tsym": "NIFTY25SEP25F",
            "instname": "FUTIDX",
            "symname": "NIFTY",
            "exd": "25-SEP-2025",
            "token": "2",
        },
    ]
    adapter._futures_anchor_cache["NIFTY"] = ("NIFTY28AUG25F", date(2020, 1, 1))

    tsym = adapter._resolve_futures_anchor_tsym("NIFTY")

    assert tsym == "NIFTY25SEP25F"
    search_calls = [call for call in rest.calls if call[0] == "search_scrip"]
    assert len(search_calls) == 1


def test_get_option_chain_uses_underlying_ltp_as_strike_price():
    """Live-corrected: `strprc=0.0` (the original hardcoded placeholder)
    was rejected outright ("Invalid strprc") — Shoonya's docs call it the
    "mid price" to center the chain on (near ATM), not an optional/zero
    default. Fetches the underlying's own current quote (same "lp" field
    `normalizer.parse_tick` already reads) and uses that as the strike
    price GetOptionChain is anchored on.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    _configure_search_scrip_for_option_chain(rest)
    rest.get_quotes_response = {"lp": "24567.85"}

    adapter.get_option_chain("NIFTY", date(2026, 7, 30))

    quote_calls = [call[1] for call in rest.calls if call[0] == "get_quotes"]
    assert ("FA1", "NSE", "26000") in quote_calls
    chain_calls = [call[1] for call in rest.calls if call[0] == "get_option_chain"]
    assert chain_calls[0][3] == 24567.85
