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

import httpx

from app.core.rate_limiter import RateLimitExceeded, TokenBucket, make_broker_call_limiter

logger = logging.getLogger("app.broker_adapter.shoonya")


class ShoonyaApiError(Exception):
    """A Shoonya call returned `stat: Not_Ok` (or an HTTP error status) —
    carries `emsg` (Shoonya's own error message) so callers can pattern-match
    known scenarios (IP mismatch, session expiry) without re-parsing JSON.
    """

    def __init__(self, endpoint: str, message: str, raw: object = None) -> None:
        super().__init__(f"{endpoint}: {message}")
        self.endpoint = endpoint
        self.message = message
        self.raw = raw


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
        if not self._rate_limiter.acquire_blocking(timeout=self._rate_limit_timeout):
            raise RateLimitExceeded(f"broker call limiter timed out waiting to call {endpoint}")

        body = {
            "jData": json.dumps(jdata),
            "jKey": self._access_token,
        }
        headers = {"Authorization": f"Bearer {self._access_token}"}

        try:
            response = self._client.post(
                f"{self._api_host}/{endpoint}", data=body, headers=headers
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
            raise ShoonyaApiError(endpoint, str(parsed.get("emsg", "unknown error")), raw=parsed)

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

    def get_option_chain(
        self, uid: str, exchange: str, tradingsymbol: str, strike_price: float, count: int = 10
    ) -> list[dict]:
        """`cnt` is strikes-each-side-of-anchor, not a total — matches
        `mock_universe.py`'s own `strike_range` convention (default 10
        comfortably covers every strategy's widest ATM±7 analysis window).
        """
        result = self._post(
            "GetOptionChain",
            {
                "uid": uid,
                "exch": exchange,
                "tsym": tradingsymbol,
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
