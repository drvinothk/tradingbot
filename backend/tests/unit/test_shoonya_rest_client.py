from __future__ import annotations

import httpx
import pytest

from app.core.rate_limiter import TokenBucket
from app.modules.broker_adapter.shoonya.rest_client import ShoonyaApiError, ShoonyaRestClient


def _client(handler) -> ShoonyaRestClient:
    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return ShoonyaRestClient(
        "https://api.shoonya.test/NorenWClientAPI", "tok-123", http_client=http_client
    )


def test_post_sends_jkey_and_authorization_header():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
        return httpx.Response(200, json={"stat": "Ok"})

    client = _client(handler)
    client._post("SomeEndpoint", {"uid": "FA1"})

    assert captured["headers"]["authorization"] == "Bearer tok-123"
    assert captured["body"]["jKey"] == "tok-123"


def test_post_sends_jdata_as_unencoded_raw_json():
    """Live-corrected regression test: a real account's first live call
    against the old dict-based `data={...}` shape got back
    `Invalid Input : jData is not valid json object` — httpx percent-encodes
    a dict's values, but Shoonya's server does a naive `jData=`-prefix
    string split, not proper form-decoding. This asserts the body is the
    literal, unencoded JSON text (no `%7B`/`%22` escaping), not just that a
    `jKey` field happens to be parseable.
    """
    import json

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.content.decode()
        assert "%7B" not in raw and "%22" not in raw, "jData must not be percent-encoded"
        jdata_part = raw.split("&jKey=")[0]
        assert jdata_part.startswith("jData=")
        captured["jdata"] = json.loads(jdata_part[len("jData=") :])
        return httpx.Response(200, json={"stat": "Ok"})

    client = _client(handler)
    client._post("SomeEndpoint", {"uid": "FA1", "exch": "NFO"})

    assert captured["jdata"] == {"uid": "FA1", "exch": "NFO"}


def test_get_option_chain_url_encodes_tsym():
    """Confirms this method still applies the reference NorenApi.py's
    `quote_plus` encoding to `tsym` — harmless defensive behavior, but not
    what actually fixed the live "Invalid Trading Symbol" rejection: a real
    "Nifty 50" query got rejected even quote_plus-encoded ("Nifty+50" was
    rejected too). The real fix was in ShoonyaBrokerAdapter.get_option_chain
    (a real futures contract symbol, never any form of the index name) —
    see its own docstring.
    """
    import json

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.content.decode()
        jdata_part = raw.split("&jKey=")[0][len("jData=") :]
        captured["jdata"] = json.loads(jdata_part)
        return httpx.Response(200, json={"stat": "Ok", "values": []})

    client = _client(handler)
    client.get_option_chain("FA1", "NFO", "Nifty 50", 0.0)

    assert captured["jdata"]["tsym"] == "Nifty+50"


def test_post_raises_on_not_ok_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"stat": "Not_Ok", "emsg": "Invalid Input"})

    client = _client(handler)
    with pytest.raises(ShoonyaApiError, match="Invalid Input"):
        client._post("PlaceOrder", {"uid": "FA1"})


def test_post_raises_on_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    client = _client(handler)
    with pytest.raises(ShoonyaApiError):
        client._post("PlaceOrder", {"uid": "FA1"})


def test_search_scrip_returns_values_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"stat": "Ok", "values": [{"tsym": "NIFTY"}]})

    client = _client(handler)
    result = client.search_scrip("FA1", "NFO", "NIFTY")
    assert result == [{"tsym": "NIFTY"}]


def test_order_book_returns_list_response_directly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"norenordno": "1"}, {"norenordno": "2"}])

    client = _client(handler)
    result = client.order_book("FA1")
    assert len(result) == 2


def test_place_order_sends_payload_and_parses_ack():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"stat": "Ok", "norenordno": "999"})

    client = _client(handler)
    result = client.place_order({"uid": "FA1", "tsym": "NIFTY30JUL2624000CE"})
    assert result["norenordno"] == "999"


def test_rate_limiter_timeout_raises_before_any_http_call():
    called = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["count"] += 1
        return httpx.Response(200, json={"stat": "Ok"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    exhausted_bucket = TokenBucket(capacity=1, refill_rate_per_second=0.0001)
    exhausted_bucket.try_acquire(cost=1.0)  # drain the only token

    client = ShoonyaRestClient(
        "https://api.shoonya.test/NorenWClientAPI",
        "tok-123",
        http_client=http_client,
        rate_limiter=exhausted_bucket,
        rate_limit_timeout=0.05,
    )
    from app.core.rate_limiter import RateLimitExceeded

    with pytest.raises(RateLimitExceeded):
        client.order_book("FA1")
    assert called["count"] == 0
