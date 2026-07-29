from __future__ import annotations

import hashlib

import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import ShoonyaSettings
from app.modules.broker_adapter.shoonya import auth

SETTINGS = ShoonyaSettings(
    client_id="TESTCID",
    secret_code=SecretStr("TESTSECRET"),
    user_id="FA12345",
    redirect_url="http://127.0.0.1:5000/shoonya/callback",
    api_host="https://api.shoonya.test/NorenWClientAPI",
    oauth_authorize_url="https://api.shoonya.test/OAuthlogin/authorize/oauth",
)


def test_build_authorize_url_includes_client_id_and_redirect():
    url = auth.build_authorize_url(SETTINGS)
    assert url.startswith(SETTINGS.oauth_authorize_url)
    assert "client_id=TESTCID" in url
    assert "redirect_uri=" in url


def test_token_exchange_checksum_is_sha256_of_concatenation():
    expected = hashlib.sha256(b"TESTCIDTESTSECRETabc123").hexdigest()
    assert auth._token_exchange_checksum("TESTCID", "TESTSECRET", "abc123") == expected


def _client_with_response(json_body: dict, status_code: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=json_body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_exchange_code_for_token_success():
    client = _client_with_response(
        {"susertoken": "tok-123", "actid": "FA12345", "refresh_token": "refresh-1"}
    )
    session = auth.exchange_code_for_token(SETTINGS, "auth-code", http_client=client)
    assert session.auth_result.session_token == "tok-123"
    assert session.auth_result.account_id == "FA12345"
    assert session.refresh_token == "refresh-1"


def test_exchange_code_for_token_missing_field_raises():
    client = _client_with_response({"stat": "Not_Ok", "emsg": "Invalid Checksum"})
    with pytest.raises(auth.ShoonyaAuthError):
        auth.exchange_code_for_token(SETTINGS, "auth-code", http_client=client)


def test_exchange_code_for_token_http_error_raises():
    client = _client_with_response({"emsg": "server error"}, status_code=500)
    with pytest.raises(auth.ShoonyaAuthError):
        auth.exchange_code_for_token(SETTINGS, "auth-code", http_client=client)


def test_exchange_code_for_token_non_json_body_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(auth.ShoonyaAuthError):
        auth.exchange_code_for_token(SETTINGS, "auth-code", http_client=client)


def test_exchange_code_for_token_sends_expected_payload():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"susertoken": "tok", "actid": "FA12345"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    auth.exchange_code_for_token(SETTINGS, "auth-code", http_client=client)

    assert captured["body"]["client_id"] == "TESTCID"
    assert captured["body"]["code"] == "auth-code"
    assert captured["body"]["checksum"] == auth._token_exchange_checksum(
        "TESTCID", "TESTSECRET", "auth-code"
    )
