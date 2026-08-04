"""Thin synchronous REST wrapper over Shoonya's Noren-OMS HTTP surface —
each method here is one endpoint, nothing more; response parsing into
broker-agnostic DTOs happens one layer up in `adapter.py` via
`normalizer.py`. Every outbound call goes through `core.rate_limiter`'s
`TokenBucket` first, per this system's "safety net against a runaway retry
loop hammering the broker" design (see that module's own docstring).

**Auth transport is a hedge, not a verified fact.** Every authenticated
call sends the access token two ways at once: as `jKey` in the POST body
(the classic Noren convention every direct-login fork agrees on) *and* as
an `Authorization: Bearer` header (what the OAuth-specific forks researched
for Phase 5 hint at via an `injectOAuthHeader` step). Harmless if the
server only honors one of the two — cheaper than guessing wrong and
burning a support ticket to find out. First thing to simplify once a real
account confirms which one actually matters.
"""

from __future__ import annotations

import json
import logging
import urllib.parse

import httpx

from app.core.rate_limiter import RateLimitExceeded, TokenBucket, make_broker_call_limiter
from app.modules.broker_adapter.base.errors import BrokerAuthError, BrokerConnectivityError

logger = logging.getLogger("app.broker_adapter.shoonya")

# Substrings observed (via research, not a live session — same caveat as
# the rest of this package) in Shoonya's `emsg` for a token that died
# mid-session. Checked case-insensitively against every Not_Ok response so
# a dead session surfaces as `ShoonyaSessionExpiredError` (a `BrokerAuthError`
# broker-agnostic callers like `PositionManager` already know to react to),
# not a generic `ShoonyaApiError` indistinguishable from any other failure.
_SESSION_EXPIRED_MARKERS = ("session expired", "invalid session", "invalid token")


class ShoonyaApiError(BrokerConnectivityError):
    """A Shoonya call returned `stat: Not_Ok` (or an HTTP error status) —
    carries `emsg` (Shoonya's own error message) so callers can pattern-match
    known scenarios without re-parsing JSON.
    """

    def __init__(self, endpoint: str, message: str, raw: object = None) -> None:
        super().__init__(f"{endpoint}: {message}")
        self.endpoint = endpoint
        self.message = message
        self.raw = raw


class ShoonyaSessionExpiredError(ShoonyaApiError, BrokerAuthError):
    """The access token this session was using is no longer valid — the
    only recovery is a fresh OAuth browser login (no silent refresh in
    this design; Phase 5's spec treats mid-session expiry as an explicit
    scenario, not something to paper over). Inherits from both
    `ShoonyaApiError` (existing Shoonya-specific catch sites keep working)
    and `BrokerAuthError` (broker-agnostic callers react to it too).
    """


class ShoonyaRestClient:
    def __init__(
        self,
        api_host: str,
        access_token: str,
        *,
        http_client: httpx.Client | None = None,
        rate_limiter: TokenBucket | None = None,
        rate_limit_timeout: float = 10.0,
    ) -> None:
        self._api_host = api_host.rstrip("/")
        self._access_token = access_token
        self._client = http_client or httpx.Client(timeout=15.0)
        self._owns_client = http_client is None
        self._rate_limiter = rate_limiter or make_broker_call_limiter()
        self._rate_limit_timeout = rate_limit_timeout

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ShoonyaRestClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _post(self, endpoint: str, jdata: dict) -> dict | list:
        """**Live-corrected**: httpx's dict-based `data={...}` percent-encodes
        the `jData` JSON string, but Shoonya's server does a naive
        `jData=`-prefix string split rather than proper form-decoding (see
        `auth.exchange_code_for_token`'s docstring for the live error that
        confirmed this for `GenAcsTok` — the same convention applies to
        every Noren endpoint, this one included). Sends the raw
        `jData=...&jKey=...` string via `content=`, unencoded, matching every
        reference `NorenApi.py` implementation.
        """
        if not self._rate_limiter.acquire_blocking(timeout=self._rate_limit_timeout):
            raise RateLimitExceeded(f"broker call limiter timed out waiting to call {endpoint}")

        body = f"jData={json.dumps(jdata)}&jKey={self._access_token}"
        headers = {"Authorization": f"Bearer {self._access_token}"}

        try:
            response = self._client.post(
                f"{self._api_host}/{endpoint}", content=body, headers=headers
            )
        except httpx.HTTPError as exc:
            raise ShoonyaApiError(endpoint, f"request failed: {exc}") from exc

        if response.status_code >= 400:
            raise ShoonyaApiError(
                endpoint, f"HTTP {response.status_code}: {response.text[:500]}"
            )

        try:
            parsed = response.json()
        except ValueError as exc:
            raise ShoonyaApiError(
                endpoint, f"non-JSON response: {response.text[:500]}"
            ) from exc

        # Noren convention: a single-object response carries stat:Not_Ok on
        # failure; a list response (OrderBook, PositionBook, SearchScrip's
        # `values`) has no top-level stat at all and is never itself an error.
        if isinstance(parsed, dict) and parsed.get("stat") == "Not_Ok":
            emsg = str(parsed.get("emsg", "unknown error"))
            error_cls = (
                ShoonyaSessionExpiredError
                if any(marker in emsg.lower() for marker in _SESSION_EXPIRED_MARKERS)
                else ShoonyaApiError
            )
            raise error_cls(endpoint, emsg, raw=parsed)

        return parsed

    # -- instrument / market data --------------------------------------------

    def search_scrip(self, uid: str, exchange: str, search_text: str) -> list[dict]:
        result = self._post(
            "SearchScrip", {"uid": uid, "exch": exchange, "stext": search_text}
        )
        if isinstance(result, dict):
            return list(result.get("values", []))
        return list(result)

    def get_quotes(self, uid: str, exchange: str, token: str) -> dict:
        result = self._post("GetQuotes", {"uid": uid, "exch": exchange, "token": token})
        if not isinstance(result, dict):
            raise ShoonyaApiError("GetQuotes", f"unexpected list response: {result!r}")
        return result

    def get_time_price_series(
        self,
        uid: str,
        exchange: str,
        token: str,
        start_time: int,
        end_time: int,
        interval_minutes: int = 1,
    ) -> list[dict]:
        """`TPSeries` — real broker-side OHLCV candles for a token, rather
        than aggregating our own bars from a tick stream. Each returned row
        carries `into`/`inth`/`intl`/`intc` (OHLC), `intv` (volume),
        `intvwap` (broker-computed VWAP) and `oi`, per the official
        ShoonyaApi-py reference. `st`/`et` are epoch seconds; `intrv` is a
        minute count as a string ("1", "3", "5", "15", ...).

        Whether this endpoint supports NSE *index* tokens (NIFTY/BANKNIFTY
        spot) is unconfirmed — there's a documented, unresolved report of
        Shoonya returning no data for index historical queries while stock/
        derivative tokens work fine, so callers must handle an empty list.
        """
        result = self._post(
            "TPSeries",
            {
                "uid": uid,
                "exch": exchange,
                "token": token,
                "st": str(start_time),
                "et": str(end_time),
                "intrv": str(interval_minutes),
            },
        )
        if isinstance(result, dict):
            return list(result.get("values", []))
        return list(result)

    def get_option_chain(
        self, uid: str, exchange: str, tradingsymbol: str, strike_price: float, count: int = 10
    ) -> list[dict]:
        """`cnt` is strikes-each-side-of-anchor, not a total — matches
        `mock_universe.py`'s own `strike_range` convention (default 10
        comfortably covers every strategy's widest ATM±7 analysis window).

        `tsym` is `quote_plus`-encoded before being embedded in the `jData`
        JSON string, matching the reference `NorenApi.py` implementation —
        harmless defensive encoding for any future caller whose symbol
        contains special characters. **Not the actual live fix**: a real
        `"Nifty 50" is Invalid Trading Symbol"` rejection (the index
        underlying's own display-style tsym, which contains a space) still
        happened with this encoding applied (`"Nifty+50"` was rejected too)
        — `GetOptionChain` needed a real NFO option contract symbol as
        `tsym` all along, never any form of the index name. See
        `ShoonyaBrokerAdapter.get_option_chain`'s own docstring for the fix
        that actually resolved it (`_resolve_option_anchor_tsym`) — live
        diagnostic logging also confirmed each returned row carries only
        structural contract data (`token`/`tsym`/`strprc`/`optt`/...),
        never quote fields, and no `exd` at all — see that method's
        docstring for why the anchor itself must already be the exact
        requested expiry rather than filtering rows after the fact.
        """
        result = self._post(
            "GetOptionChain",
            {
                "uid": uid,
                "exch": exchange,
                "tsym": urllib.parse.quote_plus(tradingsymbol),
                "strprc": str(strike_price),
                "cnt": str(count),
            },
        )
        if isinstance(result, dict):
            return list(result.get("values", []))
        return list(result)

    # -- orders ---------------------------------------------------------------

    def place_order(self, payload: dict) -> dict:
        result = self._post("PlaceOrder", payload)
        if not isinstance(result, dict):
            raise ShoonyaApiError("PlaceOrder", f"unexpected list response: {result!r}")
        return result

    def modify_order(self, payload: dict) -> dict:
        result = self._post("ModifyOrder", payload)
        if not isinstance(result, dict):
            raise ShoonyaApiError("ModifyOrder", f"unexpected list response: {result!r}")
        return result

    def cancel_order(self, uid: str, broker_order_id: str) -> dict:
        result = self._post("CancelOrder", {"uid": uid, "norenordno": broker_order_id})
        if not isinstance(result, dict):
            raise ShoonyaApiError("CancelOrder", f"unexpected list response: {result!r}")
        return result

    def order_book(self, uid: str) -> list[dict]:
        result = self._post("OrderBook", {"uid": uid})
        return list(result) if isinstance(result, list) else []

    def single_order_history(self, uid: str, broker_order_id: str) -> list[dict]:
        result = self._post("SingleOrdHist", {"uid": uid, "norenordno": broker_order_id})
        return list(result) if isinstance(result, list) else []

    def position_book(self, uid: str, actid: str) -> list[dict]:
        result = self._post("PositionBook", {"uid": uid, "actid": actid})
        return list(result) if isinstance(result, list) else []

    def get_limits(self, uid: str, actid: str) -> dict:
        result = self._post("Limits", {"uid": uid, "actid": actid})
        if not isinstance(result, dict):
            raise ShoonyaApiError("Limits", f"unexpected list response: {result!r}")
        return result
