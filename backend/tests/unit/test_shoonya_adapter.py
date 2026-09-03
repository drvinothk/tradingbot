from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import ShoonyaSettings
from app.core.rate_limiter import TokenBucket
from app.modules.broker_adapter.base.contracts import (
    AuthResult,
    BrokerOrderStatus,
    InstrumentInfo,
    OptionType,
    OrderRequest,
    OrderSide,
    OrderType,
)
from app.modules.broker_adapter.shoonya import scrip_master as shoonya_scrip_master
from app.modules.broker_adapter.shoonya.adapter import (
    _CHAIN_QUOTE_STRIKE_RADIUS,
    ShoonyaBrokerAdapter,
)
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
        # (a real option contract to anchor the GetOptionChain call
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
        # Tokens for which get_quotes should raise instead of returning
        # get_quotes_response — simulates a single bad per-strike quote call
        # without aborting the whole chain fetch.
        self.raise_on_get_quotes_tokens: set[str] = set()
        # GetOptionChain rows are purely structural (no live quote fields) —
        # see normalizer.parse_option_chain_entry's own docstring.
        self.get_option_chain_response: list[dict] = []
        self.get_time_price_series_response: list[dict] = []
        # Per-token override (index token vs its front-month future return
        # different volume) — falls through to the flat response above.
        self.get_time_price_series_response_by_token: dict[str, list[dict]] = {}
        self.order_book_response: list[dict] = []
        # When set, place_order raises this instead of returning
        # place_order_response — used to simulate an ack-timeout/dropped
        # connection (with __cause__ set to an httpx.HTTPError) vs a clean
        # stat:Not_Ok rejection (no httpx cause).
        self.place_order_raises: Exception | None = None
        self.user_details_response: dict = {"exarr": [], "prarr": []}
        self.user_details_raises: Exception | None = None

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def search_scrip(self, uid, exchange, search_text):
        self._record("search_scrip", uid, exchange, search_text)
        if exchange in self.search_scrip_response_by_exchange:
            return self.search_scrip_response_by_exchange[exchange]
        return self.search_scrip_response

    def get_quotes(self, uid, exchange, token):
        self._record("get_quotes", uid, exchange, token)
        if token in self.raise_on_get_quotes_tokens:
            raise ShoonyaApiError("GetQuotes", "simulated failure")
        return self.get_quotes_response

    def get_option_chain(self, uid, exchange, tradingsymbol, strike_price, count=10):
        self._record("get_option_chain", uid, exchange, tradingsymbol, strike_price, count=count)
        return self.get_option_chain_response

    def get_time_price_series(
        self, uid, exchange, token, start_time, end_time, interval_minutes=1
    ):
        self._record(
            "get_time_price_series", uid, exchange, token, start_time, end_time, interval_minutes
        )
        if token in self.get_time_price_series_response_by_token:
            return self.get_time_price_series_response_by_token[token]
        return self.get_time_price_series_response

    def place_order(self, payload):
        self._record("place_order", payload)
        if self.place_order_raises is not None:
            raise self.place_order_raises
        return self.place_order_response

    def modify_order(self, payload):
        self._record("modify_order", payload)
        return {"norenordno": "ORD1", "status": "OPEN"}

    def cancel_order(self, uid, broker_order_id):
        self._record("cancel_order", uid, broker_order_id)
        return {"norenordno": broker_order_id, "status": "CANCELED"}

    def order_book(self, uid):
        self._record("order_book", uid)
        return self.order_book_response

    def single_order_history(self, uid, broker_order_id):
        self._record("single_order_history", uid, broker_order_id)
        return self.order_status_response

    def position_book(self, uid, actid):
        self._record("position_book", uid, actid)
        return [{"tsym": "NIFTY30JUL26C24000", "netqty": "25", "netavgprc": "119.5"}]

    def user_details(self, uid):
        self._record("user_details", uid)
        if self.user_details_raises is not None:
            raise self.user_details_raises
        return self.user_details_response

    def close(self):
        pass


def _adapter(
    rest_client: _FakeRestClient | None = None,
    *,
    chain_quote_limiter=None,
) -> tuple[ShoonyaBrokerAdapter, _FakeRestClient]:
    rest = rest_client or _FakeRestClient()
    adapter = ShoonyaBrokerAdapter(
        SETTINGS,
        AUTH_RESULT,
        rest_client=rest,  # type: ignore[arg-type]
        chain_quote_limiter=chain_quote_limiter,
    )
    return adapter, rest


def test_authenticate_returns_the_held_auth_result():
    adapter, _ = _adapter()
    assert adapter.authenticate() == AUTH_RESULT


def test_modify_order_derives_exch_tsym_and_translates_field_names():
    """Real bug fixed before ever going live: the original `modify_order`
    sent only `uid`/`norenordno`, missing Shoonya's own *required*
    `exch`/`tsym` fields (confirmed via Shoonya-Dev's own README) — never
    caught earlier since Phase B found zero production callers. This locks
    in the fix: `contract_symbol` derives `exch`/`tsym`, and broker-
    agnostic kwarg names translate to Noren's own field names.
    """
    rest = _FakeRestClient()
    adapter, rest = _adapter(rest)

    adapter.modify_order(
        "ORD1", contract_symbol="NIFTY25AUG26C24250", trigger_price=90.35, limit_price=90.15
    )

    call = next(c for c in rest.calls if c[0] == "modify_order")
    payload = call[1][0]
    assert payload["norenordno"] == "ORD1"
    assert payload["uid"] == "FA1"
    assert payload["exch"] == "NFO"
    assert payload["tsym"] == "NIFTY25AUG26C24250"
    assert payload["trgprc"] == "90.35"
    assert payload["prc"] == "90.15"
    assert "contract_symbol" not in payload
    assert "trigger_price" not in payload
    assert "limit_price" not in payload


def test_cancel_order_follows_up_with_get_order_status_on_the_real_status_less_ack():
    """Live incident 2026-09-02: Shoonya's real `CancelOrder` ack is
    `{"stat": "Ok", "result": "<id>"}` -- no `status` field at all, per
    `normalizer.parse_order_result`'s own docstring -- which
    `parse_order_result` defaults to `PENDING`.
    `cancel_resting_protective_stop` only proceeds to place a fresh exit
    order on a real `BrokerOrderStatus.CANCELLED`, so a bare `PENDING`
    always fell into its "ambiguous, don't proceed" branch against a real
    account. This pins the fix: `cancel_order` must follow up with
    `get_order_status` (same pattern `place_order` already has for its own
    `pending` ack) to get the real post-cancel state.
    """
    rest = _FakeRestClient()

    def _status_less_cancel(uid, broker_order_id):
        rest._record("cancel_order", uid, broker_order_id)
        return {"stat": "Ok", "result": broker_order_id}

    rest.cancel_order = _status_less_cancel
    rest.order_status_response = [{"norenordno": "ORD1", "status": "CANCELED"}]
    adapter, rest = _adapter(rest)

    result = adapter.cancel_order("ORD1")

    assert result.status == BrokerOrderStatus.CANCELLED
    assert [c[0] for c in rest.calls] == ["cancel_order", "single_order_history"]


def test_cancel_order_does_not_follow_up_when_the_ack_already_carries_a_real_status():
    """The default fake's `cancel_order` response includes a `status` field
    (not the real, status-less Shoonya shape -- see the test above) --
    exercises the common/back-compat case where no follow-up is needed,
    confirming the fix doesn't add an unconditional extra round-trip.
    """
    adapter, rest = _adapter()

    result = adapter.cancel_order("ORD1")

    assert result.status == BrokerOrderStatus.CANCELLED
    assert [c[0] for c in rest.calls] == ["cancel_order"]


def test_cancel_order_keeps_the_original_ack_when_the_follow_up_itself_fails():
    """A transient failure fetching post-cancel status must not turn 'cancel
    request accepted' into a false failure -- same reasoning as
    `place_order`'s identical `except ShoonyaApiError` branch.
    """
    rest = _FakeRestClient()
    rest.cancel_order = lambda uid, broker_order_id: {"stat": "Ok", "result": broker_order_id}

    def _raise(uid, broker_order_id):
        raise ShoonyaApiError("SingleOrdHist", "simulated failure")

    rest.single_order_history = _raise
    adapter, rest = _adapter(rest)

    result = adapter.cancel_order("ORD1")

    assert result.status == BrokerOrderStatus.PENDING
    assert result.broker_order_id == "ORD1"


def test_modify_order_requires_contract_symbol():
    adapter, _ = _adapter()

    with pytest.raises(ValueError, match="contract_symbol"):
        adapter.modify_order("ORD1", trigger_price=90.35)


def test_get_product_capabilities_derives_nfo_bo_co_flags_from_prarr():
    """Bracket-order research Phase A — confirms the derivation logic
    correctly reads a recognizable exch/prd shape, without asserting that
    this is actually the real Shoonya `prarr` object shape (unconfirmed,
    see `_derive_nfo_bo_co_flags`'s own docstring).
    """
    rest = _FakeRestClient()
    rest.user_details_response = {
        "exarr": ["NFO", "NSE"],
        "prarr": [
            {"exch": "NSE", "prd": "C"},
            {"exch": "NFO", "prd": "M"},
            {"exch": "NFO", "prd": "B"},
        ],
    }
    adapter, rest = _adapter(rest)

    result = adapter.get_product_capabilities()

    assert result["read_only"] is True
    assert result["raw_exarr"] == ["NFO", "NSE"]
    assert result["nfo_bo_enabled"] is True
    assert result["nfo_co_enabled"] is False
    assert [c[0] for c in rest.calls] == ["user_details"]


def test_get_product_capabilities_handles_live_confirmed_list_shaped_exch():
    """2026-08-21, live-confirmed against a real account: `exch` is a list
    of exchange codes per product, not a single string
    (`{'prd': 'B', 's_prdt_ali': 'BO', 'exch': ['NSE', 'NFO', ...]}`) — the
    real regression this test locks in, found via the live diagnostic
    itself. This account has both BO ('B') and CO ('H') enabled for NFO.
    """
    rest = _FakeRestClient()
    rest.user_details_response = {
        "exarr": ["NSE", "NFO", "CDS", "BSE", "MCX", "BCD", "NIPO", "BFO", "BIPO"],
        "prarr": [
            {"prd": "B", "s_prdt_ali": "BO", "exch": ["NSE", "NFO", "CDS", "MCX", "BSE"]},
            {"prd": "C", "s_prdt_ali": "CNC", "exch": ["NSE", "BSE", "NIPO", "BIPO"]},
            {"prd": "H", "s_prdt_ali": "CO", "exch": ["NSE", "NFO", "CDS", "MCX", "BSE"]},
            {
                "prd": "I",
                "s_prdt_ali": "MIS",
                "exch": ["NSE", "BSE", "NFO", "BFO", "CDS", "BCD", "MCX"],
            },
            {"prd": "M", "s_prdt_ali": "NRML", "exch": ["NFO", "BFO", "CDS", "BCD", "MCX"]},
        ],
    }
    adapter, _ = _adapter(rest)

    result = adapter.get_product_capabilities()

    assert result["nfo_bo_enabled"] is True
    assert result["nfo_co_enabled"] is True


def test_get_product_capabilities_returns_none_flags_when_prarr_shape_unrecognized():
    """Must fail closed — a `prarr` object shape that doesn't match any
    known field name must never be silently reported as `False`
    (indistinguishable from a confirmed "not enabled").
    """
    rest = _FakeRestClient()
    rest.user_details_response = {"exarr": ["NFO"], "prarr": [{"unexpected_field": "NFO/B"}]}
    adapter, _ = _adapter(rest)

    result = adapter.get_product_capabilities()

    assert result["nfo_bo_enabled"] is None
    assert result["nfo_co_enabled"] is None
    assert result["raw_prarr"] == [{"unexpected_field": "NFO/B"}]


def test_get_product_capabilities_survives_user_details_failure():
    rest = _FakeRestClient()
    rest.user_details_raises = ShoonyaApiError("UserDetails", "no such endpoint")
    adapter, _ = _adapter(rest)

    result = adapter.get_product_capabilities()

    assert result["read_only"] is True
    assert "no such endpoint" in result["error"]


def test_get_instrument_master_prefers_the_static_scrip_master_for_nfo(monkeypatch):
    """2026-08-12: the real gap this closes — SearchScrip live-confirmed to
    return different, non-overlapping expiry subsets across separate calls
    and an empty broker_token for recently-synced rows; the static file has
    neither failure mode. `SearchScrip` must not be called at all when the
    static file succeeds.
    """
    static_info = InstrumentInfo(
        symbol="NIFTY18AUG26C18550",
        exchange="NFO",
        lot_size=65,
        tick_size=0.05,
        is_option=True,
        underlying="NIFTY",
        expiry=date(2026, 8, 18),
        strike=18550.0,
        option_type=OptionType.CE,
        broker_token="48407",
    )
    monkeypatch.setattr(
        shoonya_scrip_master, "download_nfo_scrip_master", lambda client: b"fake-zip-bytes"
    )
    monkeypatch.setattr(
        shoonya_scrip_master, "parse_nfo_scrip_master", lambda zip_bytes: [static_info]
    )
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)

    infos = adapter.get_instrument_master("NFO")

    assert infos == [static_info]
    assert not any(call[0] == "search_scrip" for call in rest.calls)


def test_get_instrument_master_falls_back_to_search_scrip_when_download_fails(monkeypatch):
    monkeypatch.setattr(shoonya_scrip_master, "download_nfo_scrip_master", lambda client: None)
    rest = _FakeRestClient()
    rest.search_scrip_response = [
        {"tsym": "NIFTY", "ls": "25", "ti": "0.05", "token": "26000", "instname": "IDX"}
    ]
    adapter, _ = _adapter(rest)

    infos = adapter.get_instrument_master("NFO")

    assert any(call[0] == "search_scrip" for call in rest.calls)
    assert len(infos) == 2  # one row per underlying in this fake, via the fallback


def test_get_instrument_master_falls_back_to_search_scrip_when_parse_yields_nothing(monkeypatch):
    """A reachable file that parses to zero NIFTY/BANKNIFTY rows (e.g. a
    genuinely empty or entirely-unrelated download) must fall back exactly
    like a failed download -- not silently return an empty instrument
    master.
    """
    monkeypatch.setattr(
        shoonya_scrip_master, "download_nfo_scrip_master", lambda client: b"fake-zip-bytes"
    )
    monkeypatch.setattr(shoonya_scrip_master, "parse_nfo_scrip_master", lambda zip_bytes: [])
    rest = _FakeRestClient()
    rest.search_scrip_response = [
        {"tsym": "NIFTY", "ls": "25", "ti": "0.05", "token": "26000", "instname": "IDX"}
    ]
    adapter, _ = _adapter(rest)

    infos = adapter.get_instrument_master("NFO")

    assert any(call[0] == "search_scrip" for call in rest.calls)
    assert len(infos) == 2


def test_get_instrument_master_non_nfo_exchange_never_tries_the_static_file(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        shoonya_scrip_master,
        "download_nfo_scrip_master",
        lambda client: calls.append("download") or None,
    )
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)

    adapter.get_instrument_master("NSE")

    assert calls == []


def test_get_instrument_master_queries_known_underlyings_only():
    rest = _FakeRestClient()
    rest.search_scrip_response = [
        {"tsym": "NIFTY", "ls": "25", "ti": "0.05", "token": "26000", "instname": "IDX"}
    ]
    adapter, _ = _adapter(rest)

    infos = adapter._get_instrument_master_via_search_scrip("NFO")

    searched_underlyings = [call[1][2] for call in rest.calls if call[0] == "search_scrip"]
    assert searched_underlyings == ["NIFTY", "BANKNIFTY"]
    assert len(infos) == 2  # one row returned per search call in this fake
    assert infos[0].symbol == "NIFTY"


def test_get_instrument_master_skips_futures_rows():
    """Live-found: this system only ever trades index options, never
    futures, but a real NFO SearchScrip search for "NIFTY"/"BANKNIFTY" also
    matches futures contracts (`NIFTY25AUG26F`) and unrelated decoys sharing
    the substring (`NIFTYNXT5025AUG26F`, an ETF). Treating every non-option
    row as a tradable underlying synced those in as permanently-orphaned
    `Instrument` rows polluting the frontend's instrument picker with
    entries that could never be selected usefully — real option contracts
    always attach to the pre-existing NIFTY/BANKNIFTY rows instead, never to
    one of these.
    """
    rest = _FakeRestClient()
    rest.search_scrip_response = [
        {
            "tsym": "NIFTY25AUG26F",
            "ls": "65",
            "ti": "0.05",
            "token": "1",
            "instname": "FUTIDX",
        },
        {
            "tsym": "NIFTYNXT5025AUG26F",
            "ls": "10",
            "ti": "0.05",
            "token": "2",
            "instname": "FUTSTK",
        },
        {"tsym": "NIFTY", "ls": "25", "ti": "0.05", "token": "26000", "instname": "IDX"},
    ]
    adapter, _ = _adapter(rest)

    infos = adapter._get_instrument_master_via_search_scrip("NFO")

    # One kept row (IDX) per underlying searched (NIFTY, BANKNIFTY) in this
    # fake — both futures rows skipped for both.
    assert len(infos) == 2
    assert all(info.symbol == "NIFTY" for info in infos)


def test_get_instrument_master_skips_unparseable_rows_rather_than_aborting():
    """Live-found: a real NFO SearchScrip response for NIFTY included option
    rows with no `strprc` field at all (`normalizer._strike_from_tsym` now
    recovers strike from the symbol itself for those — see
    `test_shoonya_normalizer.py`), but a row can still be genuinely
    unparseable (e.g. no `exd`, no fallback for that). The old code let one
    bad row abort parsing of every row after it, so a real sync silently
    synced *nothing* for the whole underlying. One bad row must not cost
    every other, otherwise-valid row.
    """
    rest = _FakeRestClient()
    rest.search_scrip_response = [
        {
            "tsym": "NIFTY04AUG26C18500",
            "ls": "65",
            "ti": "0.05",
            "token": "48399",
            "instname": "OPTIDX",
            "symname": "NIFTY",
            "optt": "CE",
            "strprc": "18500",
            # no exd — genuinely unparseable, no fallback exists for this one
        },
        {
            "tsym": "NIFTY04AUG26C18600",
            "ls": "65",
            "ti": "0.05",
            "token": "48400",
            "instname": "OPTIDX",
            "symname": "NIFTY",
            "exd": "04-AUG-2026",
            "strprc": "18600",
            "optt": "CE",
        },
    ]
    adapter, _ = _adapter(rest)

    infos = adapter._get_instrument_master_via_search_scrip("NFO")

    # Two underlyings searched (NIFTY, BANKNIFTY), each returning the same
    # two canned rows in this fake — one bad + one good per underlying, so
    # exactly 2 good rows should survive (the malformed one skipped, not
    # fatal to the other).
    assert len(infos) == 2
    assert all(info.symbol == "NIFTY04AUG26C18600" for info in infos)


def test_get_quote_requires_resolved_token_first():
    adapter, _ = _adapter()
    with pytest.raises(ShoonyaApiError, match="no cached broker token"):
        adapter.get_quote("NIFTY30JUL26C24000")


def test_get_quote_after_instrument_master_resolves_token():
    rest = _FakeRestClient()
    rest.search_scrip_response = [
        {
            "tsym": "NIFTY30JUL26C24000",
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
    adapter._get_instrument_master_via_search_scrip("NFO")

    tick = adapter.get_quote("NIFTY30JUL26C24000")
    assert tick.ltp == 100.0


def test_place_order_is_idempotent_on_key():
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    request = OrderRequest(
        idempotency_key="key-1",
        contract_symbol="NIFTY30JUL26C24000",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        qty=25,
        lot_size=25,
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
        contract_symbol="NIFTY30JUL26C24000",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        qty=25,
        lot_size=25,
    )
    result = adapter.place_order(request)

    assert result.status.value == "filled"
    assert any(c[0] == "single_order_history" for c in rest.calls)


def test_place_order_ack_timeout_finds_the_real_order_via_order_history():
    """The exact scenario this fallback exists for: `PlaceOrder`'s own
    request-response round trip never completes (timeout, dropped
    connection) — genuinely ambiguous whether the broker received it. A
    caller that blindly retries risks placing a real duplicate order, so
    this must check order history by idempotency_key (echoed back in
    `remarks`) before ever concluding the placement failed.
    """
    rest = _FakeRestClient()
    timeout_cause = httpx.ConnectTimeout("connection timed out")
    try:
        raise ShoonyaApiError("PlaceOrder", "request failed: timeout") from timeout_cause
    except ShoonyaApiError as exc:
        rest.place_order_raises = exc
    rest.order_book_response = [
        {
            "norenordno": "ORD-REAL-1",
            "status": "COMPLETE",
            "remarks": "key-timeout-1",
            "fillshares": "25",
            "avgprc": "120.5",
        },
    ]
    adapter, _ = _adapter(rest)
    request = OrderRequest(
        idempotency_key="key-timeout-1",
        contract_symbol="NIFTY30JUL26C24000",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        qty=25,
        lot_size=25,
        limit_price=120.0,
    )

    result = adapter.place_order(request)

    assert result.broker_order_id == "ORD-REAL-1"
    assert result.status.value == "filled"
    assert any(c[0] == "order_book" for c in rest.calls)
    # Cached like a normal successful placement — a repeated call for the
    # same key must not hit the broker again either.
    second = adapter.place_order(request)
    assert second == result
    assert len([c for c in rest.calls if c[0] == "place_order"]) == 1


def test_place_order_ack_timeout_reraises_when_order_genuinely_not_found():
    rest = _FakeRestClient()
    timeout_cause = httpx.ConnectTimeout("connection timed out")
    try:
        raise ShoonyaApiError("PlaceOrder", "request failed: timeout") from timeout_cause
    except ShoonyaApiError as exc:
        rest.place_order_raises = exc
    rest.order_book_response = []  # never actually reached the broker
    adapter, _ = _adapter(rest)
    request = OrderRequest(
        idempotency_key="key-timeout-2",
        contract_symbol="NIFTY30JUL26C24000",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        qty=25,
        lot_size=25,
        limit_price=120.0,
    )

    with pytest.raises(ShoonyaApiError, match="timeout"):
        adapter.place_order(request)


def test_place_order_clean_rejection_does_not_trigger_order_history_fallback():
    """A clean `stat: Not_Ok` rejection is definitive — the broker
    unambiguously answered "no". Falling back to order-history lookup here
    would just be wasted work, not a safety concern, but it should still
    never happen: this is the control case proving the fallback is keyed
    specifically on the httpx-cause ambiguity, not on every ShoonyaApiError.
    """
    rest = _FakeRestClient()
    rest.place_order_raises = ShoonyaApiError("PlaceOrder", "Not_Ok: margin exceeded")
    adapter, _ = _adapter(rest)
    request = OrderRequest(
        idempotency_key="key-rejected-1",
        contract_symbol="NIFTY30JUL26C24000",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        qty=25,
        lot_size=25,
        limit_price=120.0,
    )

    with pytest.raises(ShoonyaApiError, match="margin exceeded"):
        adapter.place_order(request)

    assert not any(c[0] == "order_book" for c in rest.calls)


def test_get_positions_maps_all_rows():
    adapter, _ = _adapter()
    positions = adapter.get_positions()
    assert len(positions) == 1
    assert positions[0].qty == 25


def _configure_search_scrip_for_option_chain(
    rest: _FakeRestClient, expiry: date = date(2026, 7, 30)
) -> None:
    """Both underlying-token resolution (NSE) and option-anchor resolution
    (NFO) run during get_option_chain now — see the "live-corrected" rounds
    documented on `_resolve_underlying_token`/`_resolve_option_anchor_tsym`.
    The NFO row must be an *option* (`OPTIDX`) matching the requested
    `expiry` exactly, not a futures contract — anchoring via futures always
    returns the monthly chain regardless of what expiry was asked for (see
    `_resolve_option_anchor_tsym`'s own docstring), which is exactly the bug
    this whole set of tests exists to catch a regression of.
    """
    rest.search_scrip_response_by_exchange["NSE"] = [{"tsym": "Nifty 50", "token": "26000"}]
    rest.search_scrip_response_by_exchange["NFO"] = [
        {
            "tsym": "NIFTY30JUL26C24000",
            "instname": "OPTIDX",
            "symname": "NIFTY",
            "exd": expiry.strftime("%d-%b-%Y").upper(),
            "strprc": "24000",
            "optt": "CE",
            "token": "12345",
        }
    ]


def test_get_option_chain_resolves_underlying_token_on_nse_not_nfo():
    """Live-corrected against a real account: NFO only lists derivative
    contracts on NIFTY/BANKNIFTY (e.g. NIFTY30JUL26C24000), never a bare
    "NIFTY" tsym — only NSE (the cash/index segment) has the underlying
    itself. Even on NSE, the index's own tsym isn't the bare underlying
    name — it's "Nifty 50", confirmed via a live diagnostic log of real
    search_scrip results that also included "Nifty Bank", "Nifty Next 50",
    and a dozen unrelated NIFTYxxx-EQ ETF tickers (see
    `_UNDERLYING_INDEX_TSYM`'s own docstring).
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    _configure_search_scrip_for_option_chain(rest)

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


def test_resolve_underlying_token_searches_nse_with_vix_anchor_for_india_vix():
    """2026-08-19: INDIA VIX's real tsym ("INDIAVIX", confirmed live via
    GET /shoonya/search-scrip) shares no substring with the fixed "NIFTY"
    anchor NIFTY/BANKNIFTY both resolve through, so the old hardcoded
    "NIFTY" search text would never have found it -- this is the real bug
    `_UNDERLYING_INDEX_SEARCH_TEXT` fixes, caught before it ever shipped.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NSE"] = [
        {"tsym": "Nifty 50", "token": "26000"},
        {"tsym": "INDIAVIX", "token": "26017"},
    ]

    exchange, token = adapter._resolve_underlying_token("INDIA VIX")

    assert (exchange, token) == ("NSE", "26017")
    search_calls = [call[1] for call in rest.calls if call[0] == "search_scrip"]
    assert search_calls == [("FA1", "NSE", "VIX")]


def test_resolve_underlying_token_rejects_a_tsym_match_with_an_option_or_future_instname():
    """2026-08-20: defense-in-depth after a live incident where the "NIFTY"
    underlying's own WS subscription ended up carrying a different real
    instrument's price for an extended period -- see market_data.ingestion's
    own _MIN_PLAUSIBLE_PRICE_BY_SYMBOL docstring for the primary,
    mechanism-agnostic fix. This is the secondary guard: a tsym match whose
    own instname looks like an option/future must never be trusted/cached
    as the underlying's token, even if the string match itself looks
    correct -- the search must keep going past a false-positive tsym match
    rather than stop at the first one.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NSE"] = [
        {"tsym": "Nifty 50", "token": "99999", "instname": "OPTIDX"},  # must be rejected
        {"tsym": "Nifty 50", "token": "26000", "instname": "UNDIND"},  # the real index row
    ]

    exchange, token = adapter._resolve_underlying_token("NIFTY")

    assert (exchange, token) == ("NSE", "26000")


def test_resolve_underlying_token_raises_when_only_a_bad_instname_match_exists():
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NSE"] = [
        {"tsym": "Nifty 50", "token": "99999", "instname": "FUTIDX"},
    ]

    with pytest.raises(ShoonyaApiError):
        adapter._resolve_underlying_token("NIFTY")


def test_resolve_symbol_token_for_a_known_underlying_falls_back_to_a_live_search():
    """2026-08-12: real gap found via a live-hours QC pass, not a live
    account this time -- `MarketDataIngestionService` subscribes to an
    underlying's own tick (via `subscribe_quotes` -> `_resolve_symbol_
    token`) *before* any strategy has ever called `get_option_chain` for
    it in this process, so the plain `_resolve_token` (no fallback) used
    to raise `ShoonyaApiError` on the very first such subscribe of a
    fresh session. `_resolve_symbol_token` now routes known underlyings
    through `_resolve_underlying_token`'s existing cache+live-search
    fallback instead, same as `get_price_history` already did.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NSE"] = [{"tsym": "Nifty 50", "token": "26000"}]

    exchange, token = adapter._resolve_symbol_token("NIFTY")

    assert (exchange, token) == ("NSE", "26000")


def test_resolve_symbol_token_for_a_known_underlying_uses_the_cache_when_already_resolved():
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NSE"] = [{"tsym": "Nifty 50", "token": "26000"}]
    adapter._resolve_underlying_token("NIFTY")  # warms the cache
    rest.calls.clear()

    exchange, token = adapter._resolve_symbol_token("NIFTY")

    assert (exchange, token) == ("NSE", "26000")
    assert rest.calls == [], "must not re-search once cached"


def test_resolve_symbol_token_for_india_vix_falls_back_to_a_live_search():
    """2026-08-27: VIX was stale for days because `_resolve_symbol_token`
    only routed NIFTY/BANKNIFTY through `_resolve_underlying_token`; INDIA
    VIX (no option chain, no instrument-master path to warm its token) fell
    through to the cache-only `_resolve_token` and always raised on
    subscribe. The gate now covers every `_UNDERLYING_INDEX_TSYM` key.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NSE"] = [
        {"tsym": "Nifty 50", "token": "26000"},
        {"tsym": "INDIAVIX", "token": "26017"},
    ]

    exchange, token = adapter._resolve_symbol_token("INDIA VIX")

    assert (exchange, token) == ("NSE", "26017")
    search_calls = [call[1] for call in rest.calls if call[0] == "search_scrip"]
    assert search_calls == [("FA1", "NSE", "VIX")]


def test_resolve_symbol_token_for_an_option_contract_still_raises_when_uncached():
    """An option contract's token has no reasonable blind fallback (no
    expiry/strike context to search on) -- only known underlyings get the
    live-search fallback, an uncached option symbol must still raise
    exactly as before this fix.
    """
    adapter, _ = _adapter()

    with pytest.raises(ShoonyaApiError):
        adapter._resolve_symbol_token("NIFTY18AUG26C24400")


def test_get_option_chain_anchors_on_exact_expiry_option_contract():
    """Live-corrected three times against a real account for the tsym
    format itself ("NIFTY", "Nifty 50", and quote_plus-encoded "Nifty+50"
    were all rejected by GetOptionChain as "Invalid Trading Symbol"), then
    live-corrected a fourth time for *which* contract to anchor on: an
    earlier version of this anchored on the nearest-expiry NFO *futures*
    contract, which always returns the monthly chain (NFO futures are
    monthly-only) regardless of what expiry was requested — live-confirmed
    via diagnostic logging against a real account (both NIFTY and BANKNIFTY
    chains anchored that way came back on the identical monthly date, even
    though NIFTY still lists weekly options). The fix anchors on a real
    *option* contract already matching the exact requested expiry instead.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NSE"] = [{"tsym": "Nifty 50", "token": "26000"}]
    rest.search_scrip_response_by_exchange["NFO"] = [
        # A stock option that merely contains "NIFTY" in its name (decoy —
        # wrong symname), a real NIFTY option at the wrong expiry, and the
        # correct-expiry option, to prove both the symname filter and the
        # exact-expiry match are doing real work, not just picking whatever
        # comes back first.
        {
            "tsym": "NIFTYBEES30JUL26C24000",
            "instname": "OPTSTK",
            "symname": "NIFTYBEES",
            "exd": "30-JUL-2026",
            "token": "1",
        },
        {
            "tsym": "NIFTY06AUG26C24000",
            "instname": "OPTIDX",
            "symname": "NIFTY",
            "exd": "06-AUG-2026",
            "token": "2",
        },
        {
            "tsym": "NIFTY30JUL26C24000",
            "instname": "OPTIDX",
            "symname": "NIFTY",
            "exd": "30-JUL-2026",
            "token": "3",
        },
    ]

    adapter.get_option_chain("NIFTY", date(2026, 7, 30))

    search_calls = [call[1] for call in rest.calls if call[0] == "search_scrip"]
    assert ("FA1", "NFO", "NIFTY") in search_calls
    chain_calls = [call[1] for call in rest.calls if call[0] == "get_option_chain"]
    assert chain_calls[0][2] == "NIFTY30JUL26C24000"
    assert chain_calls[0][1] == "NFO"


def test_seed_option_anchor_avoids_a_live_search_scrip_call():
    """2026-08-12: `SearchScrip` proved itself unreliable live (returning
    empty results for a real, currently-listed underlying+expiry, more
    than once in the same session) -- `seed_option_anchor` lets a caller
    that already knows a good anchor from this system's own DB skip the
    live call entirely for that `(underlying, expiry)`, since an exact
    calendar expiry's anchor can never go stale once known.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)

    adapter.seed_option_anchor("NIFTY", date(2026, 8, 18), "NIFTY18AUG26C24400")
    rest.search_scrip_response_by_exchange["NSE"] = [{"tsym": "Nifty 50", "token": "26000"}]
    rest.search_scrip_response_by_exchange["NFO"] = []  # would fail resolution if ever called

    adapter.get_option_chain("NIFTY", date(2026, 8, 18))

    nfo_calls = [
        call[1] for call in rest.calls if call[0] == "search_scrip" and call[1][1] == "NFO"
    ]
    assert nfo_calls == [], "a seeded anchor must never trigger a live NFO SearchScrip call"
    chain_calls = [call[1] for call in rest.calls if call[0] == "get_option_chain"]
    assert chain_calls[0][2] == "NIFTY18AUG26C24400"


def test_resolve_option_anchor_tsym_raises_when_no_matching_expiry_exists():
    """A strategy silently trading a different expiry than the one it asked
    for (e.g. the monthly series when a weekly was requested) is a worse
    failure mode than an explicit error — this is what actually enforces
    that, unlike the old futures-anchor approach which had no way to even
    detect the mismatch (`GetOptionChain` rows carry no `exd`). Models the
    real BANKNIFTY case: NSE discontinued BANKNIFTY weekly options, so
    requesting one should fail loud, not silently substitute the monthly
    contract.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NFO"] = [
        {
            "tsym": "BANKNIFTY25AUG26C57900",
            "instname": "OPTIDX",
            "symname": "BANKNIFTY",
            "exd": "25-AUG-2026",
            "token": "1",
        },
    ]

    with pytest.raises(ShoonyaApiError, match="no NFO option contract found"):
        adapter._resolve_option_anchor_tsym("BANKNIFTY", date(2026, 8, 6))


def test_resolve_option_anchor_tsym_caches_by_underlying_and_expiry():
    """`get_option_chain` runs on every periodic freshness-gate refresh, not
    just at strategy start — a fresh NFO SearchScrip call every time would
    be pure waste for data that can't change once resolved (unlike the old
    futures-anchor cache, an exact calendar date never goes stale). A
    second call for the same (underlying, expiry) pair must reuse the
    cached tsym rather than searching again.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    expiry = date(2026, 7, 30)
    rest.search_scrip_response_by_exchange["NFO"] = [
        {
            "tsym": "NIFTY30JUL26C24000",
            "instname": "OPTIDX",
            "symname": "NIFTY",
            "exd": "30-JUL-2026",
            "token": "3",
        },
    ]

    first = adapter._resolve_option_anchor_tsym("NIFTY", expiry)
    second = adapter._resolve_option_anchor_tsym("NIFTY", expiry)

    assert first == second == "NIFTY30JUL26C24000"
    search_calls = [call for call in rest.calls if call[0] == "search_scrip"]
    assert len(search_calls) == 1


def test_resolve_option_anchor_tsym_refetches_for_a_different_expiry():
    """The cache is keyed by (underlying, expiry), not just underlying —
    a different requested expiry must never reuse a previously-cached
    contract for a different date.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NFO"] = [
        {
            "tsym": "NIFTY30JUL26C24000",
            "instname": "OPTIDX",
            "symname": "NIFTY",
            "exd": "30-JUL-2026",
            "token": "3",
        },
        {
            "tsym": "NIFTY06AUG26C24000",
            "instname": "OPTIDX",
            "symname": "NIFTY",
            "exd": "06-AUG-2026",
            "token": "4",
        },
    ]

    first = adapter._resolve_option_anchor_tsym("NIFTY", date(2026, 7, 30))
    second = adapter._resolve_option_anchor_tsym("NIFTY", date(2026, 8, 6))

    assert first == "NIFTY30JUL26C24000"
    assert second == "NIFTY06AUG26C24000"
    search_calls = [call for call in rest.calls if call[0] == "search_scrip"]
    assert len(search_calls) == 2


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


def test_get_option_chain_entries_include_live_per_strike_quotes():
    """Live-confirmed via diagnostic logging against a real account:
    `GetOptionChain` rows are purely structural (token/tsym/strprc/optt/...)
    and never carry live quote fields, unlike `GetQuotes`/WS touchline
    pushes. Real per-strike pricing needs one `GetQuotes` call per contract
    token, joined back onto the structural row.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    _configure_search_scrip_for_option_chain(rest)
    rest.get_option_chain_response = [
        {
            "tsym": "NIFTY30JUL26C24000",
            "token": "111",
            "strprc": "24000.00",
            "optt": "CE",
            "instname": "OPTIDX",
        },
        {
            "tsym": "NIFTY30JUL26P24000",
            "token": "112",
            "strprc": "24000.00",
            "optt": "PE",
            "instname": "OPTIDX",
        },
    ]
    rest.get_quotes_response = {
        "lp": "142.35",
        "bp1": "142.00",
        "sp1": "142.70",
        "v": "125000",
        "oi": "980000",
    }

    snapshot = adapter.get_option_chain("NIFTY", date(2026, 7, 30))

    assert len(snapshot.entries) == 2
    entry = snapshot.entries[0]
    assert entry.contract_symbol == "NIFTY30JUL26C24000"
    assert entry.ltp == 142.35
    assert entry.bid == 142.00
    assert entry.ask == 142.70
    assert entry.volume == 125000
    assert entry.oi == 980000
    # underlying quote (NSE, once) + one GetQuotes per chain entry (NFO, x2)
    quote_calls = [call[1] for call in rest.calls if call[0] == "get_quotes"]
    assert ("FA1", "NFO", "111") in quote_calls
    assert ("FA1", "NFO", "112") in quote_calls


def test_get_option_chain_zero_fills_entry_when_live_quote_fetch_fails():
    """A transient failure fetching one contract's live quote shouldn't
    discard the whole snapshot — every other strike's data is still good.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    _configure_search_scrip_for_option_chain(rest)
    rest.get_option_chain_response = [
        {
            "tsym": "NIFTY30JUL26C24000",
            "token": "111",
            "strprc": "24000.00",
            "optt": "CE",
            "instname": "OPTIDX",
        },
    ]
    rest.raise_on_get_quotes_tokens = {"111"}

    snapshot = adapter.get_option_chain("NIFTY", date(2026, 7, 30))

    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].ltp == 0.0
    assert snapshot.entries[0].oi == 0


def test_get_option_chain_requests_a_narrowed_strike_count():
    """2026-09-03 rate-limit incident: GetOptionChain used to fetch Shoonya's
    default count=10 (~40 structural rows), then fired one GetQuotes REST
    call per row -- a burst ~4x Shoonya's documented 10/sec ceiling. No
    strategy ever ranks/trades beyond ATM+/-3 (plus a small DTE-aware
    margin), so the chain fetch itself is now narrowed at the source via
    Shoonya's own `count` (strikes-each-side-of-anchor) parameter.
    """
    rest = _FakeRestClient()
    adapter, rest = _adapter(rest)
    _configure_search_scrip_for_option_chain(rest)

    adapter.get_option_chain("NIFTY", date(2026, 7, 30))

    chain_calls = [call for call in rest.calls if call[0] == "get_option_chain"]
    assert chain_calls[0][2]["count"] == _CHAIN_QUOTE_STRIKE_RADIUS


def test_get_option_chain_paces_per_strike_quotes_through_dedicated_limiter():
    """Same incident as above: narrowing the row count alone (previous test)
    isn't sufficient -- even ~24 calls completing in under a second is still
    ~2.4x Shoonya's ceiling, since nothing paces them beyond raw network RTT.
    A dedicated, tighter TokenBucket (separate from ShoonyaRestClient's own
    general-purpose one) gates every per-strike GetQuotes call in this loop.
    """
    rest = _FakeRestClient()
    rest.get_option_chain_response = [
        {
            "tsym": "NIFTY30JUL26C24000",
            "token": "111",
            "strprc": "24000.00",
            "optt": "CE",
            "instname": "OPTIDX",
        },
        {
            "tsym": "NIFTY30JUL26P24000",
            "token": "112",
            "strprc": "24000.00",
            "optt": "PE",
            "instname": "OPTIDX",
        },
    ]

    class _CountingBucket(TokenBucket):
        def __init__(self):
            super().__init__(capacity=100, refill_rate_per_second=100.0)
            self.acquire_calls = 0

        def acquire_blocking(self, cost=1.0, timeout=None):
            self.acquire_calls += 1
            return super().acquire_blocking(cost, timeout)

    limiter = _CountingBucket()
    adapter, rest = _adapter(rest, chain_quote_limiter=limiter)
    _configure_search_scrip_for_option_chain(rest)

    adapter.get_option_chain("NIFTY", date(2026, 7, 30))

    assert limiter.acquire_calls == 2


def test_get_option_chain_zero_fills_entry_when_chain_quote_limiter_times_out():
    """A stuck/exhausted dedicated limiter must degrade the same way a
    per-strike GetQuotes failure already does -- zero-filled entry, not a
    crash or a discarded snapshot."""
    rest = _FakeRestClient()
    rest.get_option_chain_response = [
        {
            "tsym": "NIFTY30JUL26C24000",
            "token": "111",
            "strprc": "24000.00",
            "optt": "CE",
            "instname": "OPTIDX",
        },
    ]

    class _NeverAvailableBucket(TokenBucket):
        def __init__(self):
            super().__init__(capacity=1, refill_rate_per_second=1.0)

        def acquire_blocking(self, cost=1.0, timeout=None):
            return False

    adapter, rest = _adapter(rest, chain_quote_limiter=_NeverAvailableBucket())
    _configure_search_scrip_for_option_chain(rest)

    snapshot = adapter.get_option_chain("NIFTY", date(2026, 7, 30))

    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].ltp == 0.0
    # Only the underlying's own strike-price quote (NSE) fires -- the
    # per-strike NFO GetQuotes call is never attempted once the dedicated
    # limiter refuses to grant it.
    nfo_quote_calls = [
        call for call in rest.calls if call[0] == "get_quotes" and call[1][1] == "NFO"
    ]
    assert nfo_quote_calls == []


def test_get_price_history_resolves_underlying_and_calls_tpseries():
    """Live-confirmed against a real account: `TPSeries` works for NSE
    index tokens (NIFTY/BANKNIFTY spot), returning real OHLC — this wraps
    that call for the REST-polling fallback path in `market_data.ingestion`.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NSE"] = [{"tsym": "Nifty 50", "token": "26000"}]
    rest.get_time_price_series_response = [
        {
            "ssboe": "1785831900",
            "into": "24446.40",
            "inth": "24448.40",
            "intl": "24444.90",
            "intc": "24446.80",
            "intv": "0",
        },
    ]
    start = datetime(2026, 8, 4, 13, 0, tzinfo=UTC)
    end = datetime(2026, 8, 4, 14, 0, tzinfo=UTC)

    candles = adapter.get_price_history("NIFTY", start, end, timeframe_seconds=60)

    assert len(candles) == 1
    assert candles[0].close == 24446.80
    tpseries_calls = [call[1] for call in rest.calls if call[0] == "get_time_price_series"]
    assert tpseries_calls == [
        ("FA1", "NSE", "26000", int(start.timestamp()), int(end.timestamp()), 1)
    ]


def test_get_price_history_converts_timeframe_seconds_to_whole_minutes():
    """Shoonya's own docs only support whole-minute intervals
    ("1","3","5","10","15","30","60",...) — never seconds on the wire."""
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NSE"] = [{"tsym": "Nifty 50", "token": "26000"}]
    start = datetime(2026, 8, 4, 13, 0, tzinfo=UTC)
    end = datetime(2026, 8, 4, 14, 0, tzinfo=UTC)

    adapter.get_price_history("NIFTY", start, end, timeframe_seconds=300)

    tpseries_calls = [call[1] for call in rest.calls if call[0] == "get_time_price_series"]
    assert tpseries_calls[0][-1] == 5


# -- Order tagging + lot sizing -----------------------------------------
# Ops-Hardening Phase 5 originally added a hardcoded 1-lot cap here as
# defense-in-depth alongside Risk Service's own `per_trade_lot_cap`.
# Removed 2026-09-03: `per_trade_lot_cap` (`risk_limit_configs`) is now a
# real, UI-editable workspace setting (`GET`/`PATCH
# /system-settings/max-lots-per-trade`) rather than a stuck-at-1, no-UI
# value, so a second, adapter-level, non-editable copy of the same check
# was no longer earning its complexity -- Risk Service's own cap (now
# operator-controlled) plus real broker margin rejection are the
# backstops for order size. See that endpoint's own comment and
# `strategy_engine.sizing.resolve_qty_lots` for the full reasoning.


def test_place_order_reaches_the_broker_for_a_multi_lot_order():
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    request = OrderRequest(
        idempotency_key="multi-lot-1",
        contract_symbol="NIFTY30JUL26C24000",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        qty=75,
        lot_size=25,
    )

    adapter.place_order(request)  # must not raise -- no adapter-level lot cap anymore

    assert any(c[0] == "place_order" for c in rest.calls)


def test_remarks_combines_idempotency_key_and_tag():
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    request = OrderRequest(
        idempotency_key="tag-test-1",
        contract_symbol="NIFTY30JUL26C24000",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        qty=25,
        lot_size=25,
        tag="session:abc123",
    )

    adapter.place_order(request)

    place_order_calls = [c for c in rest.calls if c[0] == "place_order"]
    payload = place_order_calls[0][1][0]
    assert payload["remarks"] == "tag-test-1|session:abc123"


def test_remarks_is_bare_idempotency_key_when_tag_is_empty():
    """Byte-for-byte the pre-Phase-5 format for every caller that never
    sets tag -- no behavior change for anything not opting in.
    """
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    request = OrderRequest(
        idempotency_key="tag-test-2",
        contract_symbol="NIFTY30JUL26C24000",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        qty=25,
        lot_size=25,
    )

    adapter.place_order(request)

    place_order_calls = [c for c in rest.calls if c[0] == "place_order"]
    payload = place_order_calls[0][1][0]
    assert payload["remarks"] == "tag-test-2"


def test_find_order_by_remarks_matches_tagged_remarks_via_prefix():
    rest = _FakeRestClient()
    timeout_cause = httpx.ConnectTimeout("connection timed out")
    try:
        raise ShoonyaApiError("PlaceOrder", "request failed: timeout") from timeout_cause
    except ShoonyaApiError as exc:
        rest.place_order_raises = exc
    rest.order_book_response = [
        {
            "norenordno": "ORD-TAGGED-1",
            "status": "COMPLETE",
            "remarks": "key-tagged-1|session:abc123",
            "fillshares": "25",
            "avgprc": "120.5",
        },
    ]
    adapter, _ = _adapter(rest)
    request = OrderRequest(
        idempotency_key="key-tagged-1",
        contract_symbol="NIFTY30JUL26C24000",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        qty=25,
        lot_size=25,
        limit_price=120.0,
        tag="session:abc123",
    )

    result = adapter.place_order(request)

    assert result.broker_order_id == "ORD-TAGGED-1"


def test_handle_order_update_populates_the_cache():
    adapter, _ = _adapter()

    adapter._handle_order_update(
        {
            "norenordno": "26082000267157",
            "status": "COMPLETE",
            "fillshares": "65",
            "avgprc": "137.45",
        }
    )

    result = adapter.peek_cached_order_update("26082000267157")
    assert result is not None
    assert result.status == BrokerOrderStatus.FILLED
    assert result.filled_qty == 65
    assert result.avg_fill_price == 137.45


def test_peek_cached_order_update_returns_none_when_nothing_cached():
    adapter, _ = _adapter()
    assert adapter.peek_cached_order_update("no-such-order") is None


def test_handle_order_update_swallows_a_malformed_message():
    """Missing norenordno -> parse_order_result raises NormalizationError;
    must be logged and skipped, never crash the WS receive thread that
    calls this.
    """
    adapter, _ = _adapter()
    adapter._handle_order_update({"status": "COMPLETE"})  # no norenordno
    assert adapter.peek_cached_order_update("") is None


def test_handle_order_update_overwrites_a_stale_cache_entry_for_the_same_order():
    adapter, _ = _adapter()
    adapter._handle_order_update({"norenordno": "1", "status": "OPEN"})
    adapter._handle_order_update(
        {"norenordno": "1", "status": "COMPLETE", "fillshares": "10", "avgprc": "50.0"}
    )

    result = adapter.peek_cached_order_update("1")
    assert result is not None
    assert result.status == BrokerOrderStatus.FILLED


# -- subscribe_quotes: single-callback-slot contract --------------------------


class _FakeWSClient:
    """No real networking -- records the callback pair it was constructed
    with and every `subscribe()` call, mirroring the real `ShoonyaWSClient`'s
    constructor/start/subscribe shape closely enough for this contract.
    """

    def __init__(self, ws_host, *, uid, actid, access_token, on_tick, on_depth=None, **kwargs):
        self.on_tick = on_tick
        self.on_depth = on_depth
        self.subscribe_calls: list[list[tuple[str, str, str]]] = []
        self.volume_proxy_calls: list[tuple[tuple, tuple]] = []

    def start(self) -> None:
        pass

    def set_callbacks(self, on_tick, on_depth=None) -> None:
        self.on_tick = on_tick
        self.on_depth = on_depth

    def subscribe(self, entries: list[tuple[str, str, str]]) -> None:
        self.subscribe_calls.append(entries)

    def set_volume_proxy(self, target: tuple, source: tuple) -> None:
        self.volume_proxy_calls.append((target, source))


def test_subscribe_quotes_second_call_with_the_same_callback_does_not_raise(monkeypatch):
    """The real production shape: `BrokerPortMarketDataAdapter` calls
    `subscribe_quotes` once per symbol batch, always passing its own stable
    `self._handle_tick` bound method -- a *fresh* bound-method object each
    time (Python never caches these), but `==`-equal to the one already
    bound, since it's the same underlying method on the same instance. This
    must not be mistaken for a genuinely different callback.
    """
    import app.modules.broker_adapter.shoonya.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "ShoonyaWSClient", _FakeWSClient)
    rest = _FakeRestClient()
    rest.search_scrip_response_by_exchange["NSE"] = [{"tsym": "Nifty 50", "token": "26000"}]
    adapter, _ = _adapter(rest)

    class _Dispatcher:
        def on_tick(self, tick) -> None:
            pass

    dispatcher = _Dispatcher()
    adapter.subscribe_quotes(["NIFTY"], on_tick=dispatcher.on_tick)
    # A second, later call with a *fresh* bound-method object for the same
    # underlying dispatcher -- must be treated as the same callback.
    adapter.subscribe_quotes(["NIFTY"], on_tick=dispatcher.on_tick)

    assert adapter._ws.subscribe_calls == [
        [("NIFTY", "NSE", "26000")],
        [("NIFTY", "NSE", "26000")],
    ]


def test_subscribe_quotes_second_call_with_a_different_callback_rebinds(monkeypatch):
    """A Shoonya reconnect rebuilds the process's shared
    `BrokerPortMarketDataAdapter`, so the still-alive `ShoonyaWSClient` gets
    handed a fresh (behaviourally identical) dispatcher on the next
    re-subscribe. That must re-point the live client (last-writer-wins),
    not raise -- the old behaviour turned every post-reconnect re-subscribe
    into a hard market-data + strategy-resume outage (2026-08-27).
    """
    import app.modules.broker_adapter.shoonya.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "ShoonyaWSClient", _FakeWSClient)
    rest = _FakeRestClient()
    rest.search_scrip_response_by_exchange["NSE"] = [{"tsym": "Nifty 50", "token": "26000"}]
    adapter, _ = _adapter(rest)

    def _first_callback(tick) -> None:
        pass

    def _second_callback(tick) -> None:
        pass

    adapter.subscribe_quotes(["NIFTY"], on_tick=_first_callback)
    adapter.subscribe_quotes(["NIFTY"], on_tick=_second_callback)

    assert adapter._ws.on_tick is _second_callback
    assert adapter._ws_bound_on_tick is _second_callback
    assert adapter._ws.subscribe_calls == [
        [("NIFTY", "NSE", "26000")],
        [("NIFTY", "NSE", "26000")],
    ]


# -- front-month future volume proxy (NSE cash-index has no `v`) --------------

from datetime import timedelta  # noqa: E402


def _fut_row(underlying: str, tsym: str, token: str, exp: date) -> dict:
    return {
        "instname": "FUTIDX",
        "symname": underlying,
        "tsym": tsym,
        "token": token,
        "exd": exp.strftime("%d-%b-%Y").upper(),
    }


def test_resolve_front_month_future_picks_nearest_unexpired_and_caches():
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    near = date.today() + timedelta(days=10)
    far = date.today() + timedelta(days=40)
    rest.search_scrip_response_by_exchange["NFO"] = [
        _fut_row("NIFTY", "NIFTY_FAR_F", "70002", far),
        _fut_row("NIFTY", "NIFTY_NEAR_F", "70001", near),
        {"instname": "OPTIDX", "symname": "NIFTY", "tsym": "NIFTYOPT", "token": "9", "exd":
            near.strftime("%d-%b-%Y").upper()},
    ]

    assert adapter._resolve_front_month_future("NIFTY") == ("NIFTY_NEAR_F", "NFO", "70001")

    # second call is cache-served — no extra search_scrip
    rest.calls.clear()
    assert adapter._resolve_front_month_future("NIFTY") == ("NIFTY_NEAR_F", "NFO", "70001")
    assert not any(c[0] == "search_scrip" for c in rest.calls)


def test_resolve_front_month_future_ignores_expired_rows():
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NFO"] = [
        _fut_row("NIFTY", "NIFTY_OLD_F", "1", date.today() - timedelta(days=5)),
        _fut_row("NIFTY", "NIFTY_LIVE_F", "2", date.today() + timedelta(days=20)),
    ]
    assert adapter._resolve_front_month_future("NIFTY") == ("NIFTY_LIVE_F", "NFO", "2")


def test_resolve_front_month_future_raises_when_none_listed():
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NFO"] = [
        {"instname": "OPTIDX", "symname": "NIFTY", "tsym": "X", "token": "1", "exd": "30-DEC-2099"},
    ]
    with pytest.raises(ShoonyaApiError):
        adapter._resolve_front_month_future("NIFTY")


def test_subscribe_quotes_wires_volume_proxy_for_known_underlying(monkeypatch):
    import app.modules.broker_adapter.shoonya.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "ShoonyaWSClient", _FakeWSClient)
    rest = _FakeRestClient()
    rest.search_scrip_response_by_exchange["NSE"] = [{"tsym": "Nifty 50", "token": "26000"}]
    rest.search_scrip_response_by_exchange["NFO"] = [
        _fut_row("NIFTY", "NIFTY_F", "70001", date.today() + timedelta(days=15)),
    ]
    adapter, _ = _adapter(rest)

    adapter.subscribe_quotes(["NIFTY"], on_tick=lambda t: None)

    assert adapter._ws.volume_proxy_calls == [
        (("NIFTY", "NSE", "26000"), ("NIFTY_F", "NFO", "70001")),
    ]


def test_subscribe_quotes_does_not_wire_volume_proxy_for_an_option_symbol(monkeypatch):
    import app.modules.broker_adapter.shoonya.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "ShoonyaWSClient", _FakeWSClient)
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    adapter._remember_token("NIFTY30JUL26C24000", "NFO", "12345")

    adapter.subscribe_quotes(["NIFTY30JUL26C24000"], on_tick=lambda t: None)

    assert adapter._ws.volume_proxy_calls == []


def test_subscribe_quotes_volume_proxy_failure_does_not_block_subscription(monkeypatch):
    import app.modules.broker_adapter.shoonya.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "ShoonyaWSClient", _FakeWSClient)
    rest = _FakeRestClient()
    rest.search_scrip_response_by_exchange["NSE"] = [{"tsym": "Nifty 50", "token": "26000"}]
    # no NFO futures rows -> _resolve_front_month_future raises
    adapter, _ = _adapter(rest)

    adapter.subscribe_quotes(["NIFTY"], on_tick=lambda t: None)

    assert adapter._ws.subscribe_calls == [[("NIFTY", "NSE", "26000")]]
    assert adapter._ws.volume_proxy_calls == []


def test_get_price_history_splices_front_month_future_volume_by_bucket():
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NSE"] = [{"tsym": "Nifty 50", "token": "26000"}]
    rest.search_scrip_response_by_exchange["NFO"] = [
        _fut_row("NIFTY", "NIFTY_F", "70001", date.today() + timedelta(days=15)),
    ]
    rest.get_time_price_series_response_by_token["26000"] = [
        {"ssboe": "1785831900", "into": "1", "inth": "2", "intl": "1", "intc": "24446.80",
         "intv": "0"},
        {"ssboe": "1785831960", "into": "1", "inth": "2", "intl": "1", "intc": "24450.10",
         "intv": "0"},
    ]
    rest.get_time_price_series_response_by_token["70001"] = [
        {"ssboe": "1785831900", "into": "1", "inth": "2", "intl": "1", "intc": "24600.0",
         "intv": "1500"},
        # second bucket missing on the future series -> falls back to index intv (0)
    ]
    start = datetime(2026, 8, 4, 13, 0, tzinfo=UTC)
    end = datetime(2026, 8, 4, 14, 0, tzinfo=UTC)

    candles = adapter.get_price_history("NIFTY", start, end, timeframe_seconds=60)

    assert [(c.close, c.volume) for c in candles] == [(24446.80, 1500), (24450.10, 0)]


def test_get_price_history_splice_failure_keeps_zero_volume():
    rest = _FakeRestClient()
    adapter, _ = _adapter(rest)
    rest.search_scrip_response_by_exchange["NSE"] = [{"tsym": "Nifty 50", "token": "26000"}]
    # no NFO rows -> front-month future resolution raises -> splice skipped
    rest.get_time_price_series_response = [
        {"ssboe": "1785831900", "into": "1", "inth": "2", "intl": "1", "intc": "24446.80",
         "intv": "0"},
    ]
    start = datetime(2026, 8, 4, 13, 0, tzinfo=UTC)
    end = datetime(2026, 8, 4, 14, 0, tzinfo=UTC)

    candles = adapter.get_price_history("NIFTY", start, end, timeframe_seconds=60)

    assert [(c.close, c.volume) for c in candles] == [(24446.80, 0)]


# --- warm_token_cache (restart token-cache warm-up) ---------------------------


def _nse_index_rows() -> list[dict]:
    return [
        {"tsym": "Nifty 50", "token": "26000", "instname": "UNDIND"},
        {"tsym": "Nifty Bank", "token": "26009", "instname": "UNDIND"},
    ]


def test_warm_token_cache_remembers_options_and_resolves_both_underlyings():
    rest = _FakeRestClient()
    rest.search_scrip_response_by_exchange["NSE"] = _nse_index_rows()
    adapter, rest = _adapter(rest)

    adapter.warm_token_cache(
        [("NIFTY28AUG25C24000", "111"), ("BANKNIFTY28AUG25P52000", "222")]
    )

    assert adapter._resolve_token("NIFTY28AUG25C24000") == ("NFO", "111")
    assert adapter._resolve_token("BANKNIFTY28AUG25P52000") == ("NFO", "222")
    assert adapter._resolve_token("NIFTY") == ("NSE", "26000")
    assert adapter._resolve_token("BANKNIFTY") == ("NSE", "26009")
    nse_searches = [c for c in rest.calls if c[0] == "search_scrip" and c[1][1] == "NSE"]
    assert len(nse_searches) == 2


def test_warm_token_cache_survives_underlying_resolution_failure():
    rest = _FakeRestClient()
    rest.search_scrip_response_by_exchange["NSE"] = []  # no exact tsym match -> raises internally
    adapter, _ = _adapter(rest)

    adapter.warm_token_cache([("NIFTY28AUG25C24000", "111")])  # must not raise

    assert adapter._resolve_token("NIFTY28AUG25C24000") == ("NFO", "111")
    with pytest.raises(ShoonyaApiError):
        adapter._resolve_token("NIFTY")


def test_warm_token_cache_skips_blank_option_tokens():
    rest = _FakeRestClient()
    rest.search_scrip_response_by_exchange["NSE"] = _nse_index_rows()
    adapter, _ = _adapter(rest)

    adapter.warm_token_cache([("NIFTY28AUG25C24000", "")])

    with pytest.raises(ShoonyaApiError):
        adapter._resolve_token("NIFTY28AUG25C24000")
