"""OAuth-style browser-redirect login, per `ShoonyaSettings`' own docstring
(`app/config/settings.py`) — Phase 0's verified flow, not the classic
direct-TOTP `QuickAuth` login most generic Noren-OMS forks use. Two steps:

1. `build_authorize_url` — the URL `api.v1.shoonya.login_redirect` sends the
   user's own browser to. They log in on Shoonya's own site (User ID +
   password + OTP/TOTP) and get redirected back to `redirect_url` with a
   `code` query param.
2. `exchange_code_for_token` — `api.v1.shoonya.oauth_callback` POSTs that
   `code` (plus a checksum) to `GenAcsTok` for the actual access token.

Neither step is exercised end-to-end without a real Shoonya account and a
human completing the browser login — see this package's own `README`-shaped
caveat in `normalizer.py` for the same "researched, not live-verified"
caveat applied to the token-exchange response shape.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.config.settings import ShoonyaSettings
from app.modules.broker_adapter.base.contracts import AuthResult
from app.modules.broker_adapter.base.errors import BrokerAuthError


class ShoonyaAuthError(BrokerAuthError):
    """Raised on anything from a malformed `GenAcsTok` response to a
    non-2xx HTTP status — covers the "invalid credentials"/"IP mismatch"/
    "TOTP drift" login-time scenarios Phase 5's spec calls for. A
    `BrokerAuthError` subclass so broker-agnostic callers can catch it
    without importing anything Shoonya-specific.
    """


@dataclass(frozen=True)
class OAuthSession:
    """What `exchange_code_for_token` returns beyond the broker-agnostic
    `AuthResult` — the refresh token isn't part of `AuthResult` (that DTO
    is broker-agnostic and no other adapter has a refresh concept), so it's
    threaded separately for `ShoonyaBrokerAdapter` to hold privately.

    `raw_login_capabilities` is bracket-order research Phase A (2026-08-21,
    read-only, no order placed anywhere) — `exarr`/`prarr` (enabled
    exchanges/products for this account) are documented by Shoonya-Dev's
    own README as fields of the classic direct-login response; whether
    `GenAcsTok` (this codebase's OAuth flow) also returns them is
    unconfirmed. Captured here, verbatim, if present — `None` if absent,
    which is itself useful evidence (means the separate `UserDetails` probe
    in `adapter.get_product_capabilities` is the only route for this flow).
    """

    auth_result: AuthResult
    refresh_token: str | None
    raw_login_capabilities: dict | None = None


def build_authorize_url(settings: ShoonyaSettings) -> str:
    """The user opens this URL in their own browser (never fetched
    server-side — the whole point of the OAuth redirect is that Shoonya's
    login page, including TOTP entry, only ever sees the user's own
    browser, never this backend).
    """
    params = {"client_id": settings.client_id, "redirect_uri": settings.redirect_url}
    return f"{settings.oauth_authorize_url}?{urlencode(params)}"


def _token_exchange_checksum(client_id: str, secret_code: str, code: str) -> str:
    """`SHA256(client_id + secret_code + code)`, per `ShoonyaSettings`'
    docstring — string concatenation in that exact order, hex-digest
    output (Noren's own checksum convention elsewhere in this codebase's
    research, e.g. `appkey = SHA256(userid|api_secret)`, is consistently
    "concatenate then hex-digest," not HMAC).
    """
    return hashlib.sha256(f"{client_id}{secret_code}{code}".encode()).hexdigest()


def exchange_code_for_token(
    settings: ShoonyaSettings, code: str, *, http_client: httpx.Client | None = None
) -> OAuthSession:
    """POSTs to `{api_host}/GenAcsTok`. Raises `ShoonyaAuthError` on any
    non-2xx response or a response missing the expected token field —
    never returns a partially-valid `OAuthSession`.

    **Live-corrected twice (first real account, prior assumptions were
    wrong):** every Noren-OMS endpoint — including, it turns out,
    `GenAcsTok` — wants its payload as a single `jData=<json-string>` form
    field, never a plain JSON body (round 1: `json=payload` got back
    `"jData or jKey is Missing"`). Round 2: httpx's dict-based `data={...}`
    percent-encodes the JSON string's `{`/`"`/`:` characters, but Shoonya's
    server does its own naive `jData=`-prefix string split rather than
    proper form-decoding — it got back `"jData is not valid json object"`
    for the percent-encoded body. The reference `NorenApi.py` implementations
    confirm this: they build the body via raw string concatenation
    (`'jData=' + json.dumps(values)`) and POST that string directly, never
    a dict — so the fix is `content=`, not `data=`, sending the exact bytes
    unencoded. No `jKey` here, matching the reference `login`/
    `forgot_password` calls (also pre-session, before any token exists).
    Inner field names (`client_id`/`code`/`checksum`) are unchanged since
    nothing live has contradicted them yet.
    """
    checksum = _token_exchange_checksum(
        settings.client_id, settings.secret_code.get_secret_value(), code
    )
    payload = {"client_id": settings.client_id, "code": code, "checksum": checksum}

    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=15.0)
    try:
        response = client.post(
            f"{settings.api_host}/GenAcsTok", content=f"jData={json.dumps(payload)}"
        )
    except httpx.HTTPError as exc:
        raise ShoonyaAuthError(f"GenAcsTok request failed: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    if response.status_code >= 400:
        raise ShoonyaAuthError(
            f"GenAcsTok returned HTTP {response.status_code}: {response.text[:500]}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise ShoonyaAuthError(f"GenAcsTok returned non-JSON body: {response.text[:500]}") from exc

    try:
        access_token = str(
            body.get("susertoken") or body.get("access_token") or body["token"]
        )
        account_id = str(body.get("actid") or body.get("account_id") or settings.user_id)
    except (KeyError, TypeError) as exc:
        raise ShoonyaAuthError(
            f"GenAcsTok response missing expected token field: {body!r}"
        ) from exc

    refresh_token = body.get("refresh_token")
    raw_login_capabilities = (
        {"exarr": body.get("exarr"), "prarr": body.get("prarr")}
        if "exarr" in body or "prarr" in body
        else None
    )
    return OAuthSession(
        auth_result=AuthResult(
            session_token=access_token,
            account_id=account_id,
            expires_at=None,
        ),
        refresh_token=str(refresh_token) if refresh_token else None,
        raw_login_capabilities=raw_login_capabilities,
    )
