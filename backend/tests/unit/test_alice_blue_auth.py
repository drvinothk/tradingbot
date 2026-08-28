"""`alice_blue_auth.probe_ws_session` — the read-only `createWsSess` liveness
check behind `GET /aliceblue/status`. httpx is exercised via a
`MockTransport`-backed client injected through the `http_client=` kwarg, the
same pattern `test_shoonya_auth.py` uses.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import AliceBlueSettings
from app.modules.market_data.providers.alice_blue_auth import (
    AliceBlueSession,
    probe_ws_session,
)

SETTINGS = AliceBlueSettings(
    app_code="AC", api_secret=SecretStr("SEC"), redirect_url="https://x/cb"
)
SESSION = AliceBlueSession(client_id="288866", user_session="tok-abc")


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("code", [401, 403])
def test_probe_reports_dead_on_auth_rejection(code):
    result = probe_ws_session(
        SETTINGS, SESSION, http_client=_client(lambda _r: httpx.Response(code, json={}))
    )
    assert result == "dead"


def test_probe_reports_alive_on_2xx():
    result = probe_ws_session(
        SETTINGS,
        SESSION,
        http_client=_client(
            lambda _r: httpx.Response(200, json={"status": "Ok", "result": [{"Status": "OK"}]})
        ),
    )
    assert result == "alive"


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_probe_reports_unknown_on_other_non_2xx(code):
    result = probe_ws_session(
        SETTINGS, SESSION, http_client=_client(lambda _r: httpx.Response(code, text="nope"))
    )
    assert result == "unknown"


def test_probe_reports_unknown_on_transport_error():
    def _boom(_request):
        raise httpx.ConnectError("no route to host")

    result = probe_ws_session(SETTINGS, SESSION, http_client=_client(_boom))
    assert result == "unknown"


def test_probe_sends_bearer_and_body():
    seen: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    probe_ws_session(SETTINGS, SESSION, http_client=_client(_handler))

    assert seen["auth"] == "Bearer tok-abc"
    assert seen["url"].endswith("/open-api/od/v1/profile/createWsSess")
