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
