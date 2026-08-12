from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
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
        self.order_book_response: list[dict] = []
        # When set, place_order raises this instead of returning
        # place_order_response — used to simulate an ack-timeout/dropped
        # connection (with __cause__ set to an httpx.HTTPError) vs a clean
        # stat:Not_Ok rejection (no httpx cause).
        self.place_order_raises: Exception | None = None

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
        self._record("get_option_chain", uid, exchange, tradingsymbol, strike_price)
        return self.get_option_chain_response

    def get_time_price_series(
        self, uid, exchange, token, start_time, end_time, interval_minutes=1
    ):
        self._record(
            "get_time_price_series", uid, exchange, token, start_time, end_time, interval_minutes
        )
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

    infos = adapter.get_instrument_master("NFO")

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

    infos = adapter.get_instrument_master("NFO")

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
    adapter.get_instrument_master("NFO")

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
