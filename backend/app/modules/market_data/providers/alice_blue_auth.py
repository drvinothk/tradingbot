"""OAuth-style browser-redirect login for Alice Blue's ANT V3 API — see
`AliceBlueSettings`' own docstring (`app/config/settings.py`) for the full,
confirmed-live-2026-08-21 flow. Two steps, same shape as
`broker_adapter/shoonya/auth.py`:

1. `build_authorize_url` — the URL `api.v1.alice_blue.login_redirect` sends
   the user's own browser to. They log in on Alice Blue's own site and get
   redirected back to `redirect_url` with `authCode`/`userId` query params.
2. `exchange_for_session` — `api.v1.alice_blue.oauth_callback` computes the
   SHA-256 checksum and POSTs it to `getUserDetails` for the actual session
   token (`userSession`).

Market-data-only: this module has no order/account-mutating calls, and
nothing here (or anywhere else in `alice_blue_*`) ever will — see
`AliceBlueMarketDataProvider`'s own docstring.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlencode

import httpx

from app.config.settings import AliceBlueSettings
from app.modules.broker_adapter.base.errors import BrokerAuthError

logger = logging.getLogger("app.market_data.alice_blue_auth")


class AliceBlueAuthError(BrokerAuthError):
    """Covers a malformed `getUserDetails` response or a non-2xx HTTP
    status — a `BrokerAuthError` subclass so broker-agnostic callers (if any
    ever exist for market data) can catch it without importing anything
    Alice-Blue-specific, same reasoning as `ShoonyaAuthError`.
    """


@dataclass(frozen=True)
class AliceBlueSession:
    client_id: str
    user_session: str


def build_authorize_url(settings: AliceBlueSettings) -> str:
    """The user opens this in their own browser (never fetched server-side —
    Alice Blue's login page, including password/OTP entry, must only ever
    see the user's own browser). Confirmed live 2026-08-21: Alice's docs
    show only `appcode` in the query string, no `redirect_uri` param — the
    redirect target is apparently tied to the App Code at registration time
    in the portal, not passed per-request.
    """
    params = {"appcode": settings.app_code}
    return f"{settings.authorize_base_url}?{urlencode(params)}"


def _checksum(user_id: str, auth_code: str, api_secret: str) -> str:
    """SHA-256(userId + authCode + apiSecret), hex digest — confirmed
    verbatim from Alice Blue's own Authentication doc page 2026-08-21, same
    "concatenate then hex-digest" convention `ShoonyaSettings`' own
    `GenAcsTok` checksum uses.
    """
    return hashlib.sha256(f"{user_id}{auth_code}{api_secret}".encode()).hexdigest()


def exchange_for_session(
    settings: AliceBlueSettings,
    auth_code: str,
    user_id: str,
    *,
    http_client: httpx.Client | None = None,
) -> AliceBlueSession:
    """POSTs to `{api_host}/open-api/od/v1/vendor/getUserDetails`. Confirmed
    live 2026-08-21 from Alice Blue's own docs: the request body is
    `{"checkSum": "<hash>"}` only — `userId`/`authCode` are not repeated in
    the body, presumably because the checksum itself is enough context for
    Alice's server (it already knows which pending auth flow this authCode
    belongs to). Raises `AliceBlueAuthError` on any non-2xx response or a
    response missing `userSession`/`clientId` — never returns a
    partially-valid session.
    """
    checksum = _checksum(user_id, auth_code, settings.api_secret.get_secret_value())
    payload = {"checkSum": checksum}

    owns_client = http_client is None
    client = http_client or httpx.Client(
        timeout=15.0,
        proxy=settings.auth_proxy or None,
    )
    try:
        response = client.post(
            f"{settings.api_host}/open-api/od/v1/vendor/getUserDetails", json=payload
        )
    except httpx.HTTPError as exc:
        raise AliceBlueAuthError(f"getUserDetails request failed: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    if response.status_code >= 400:
        raise AliceBlueAuthError(
            f"getUserDetails returned HTTP {response.status_code}: {response.text[:500]}"
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise AliceBlueAuthError(
            f"getUserDetails returned non-JSON body: {response.text[:500]}"
        ) from exc

    if str(body.get("stat", "")).lower() != "ok":
        raise AliceBlueAuthError(f"getUserDetails did not return stat=Ok: {body!r}")

    try:
        user_session = str(body["userSession"])
        client_id = str(body.get("clientId") or user_id)
    except (KeyError, TypeError) as exc:
        raise AliceBlueAuthError(
            f"getUserDetails response missing expected fields: {body!r}"
        ) from exc

    return AliceBlueSession(client_id=client_id, user_session=user_session)


def create_ws_session(
    settings: AliceBlueSettings,
    session: AliceBlueSession,
    *,
    http_client: httpx.Client | None = None,
) -> None:
    """**Live-confirmed 2026-08-21, and the actual root cause of a real WS
    auth failure** — `POST {api_host}/open-api/od/v1/profile/createWsSess`
    must be called (`Authorization: Bearer <userSession>`, body
    `{"source": "API", "userId": client_id}`) to register/activate a WS
    session server-side *before* the WebSocket connect frame is sent.
    Alice Blue's own WebSocket doc page mentions this as a "pre-connection
    requirement" but doesn't explain what it does or why it's needed — the
    WS connect frame's `susertoken` (double-SHA-256 of `userSession`) was
    rejected with `{"t":"ck","s":"NOT_OK"}` on every attempt without this
    call first, and started succeeding immediately once this was added,
    live-verified via three real subscribed ticks (NIFTY 50/NIFTY BANK).
    A second, independent live-confirmed fix, found the same session: `uid`/
    `actid` in the connect frame must be `f"{client_id}_API"`, not the bare
    `client_id` — see `alice_blue.py`'s own docstring for where that's
    applied. The response body (`{"status":"Ok","message":"Success",
    "result":[{"Status":"OK"}]}`) carries no new token to extract — this
    call is pure side effect, never raises on a non-2xx/malformed body
    (best-effort: if this genuinely needs to succeed for the WS connect
    to work, that failure surfaces naturally as the connect's own auth
    rejection instead, which is more informative than a stack trace here).
    """
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=15.0, proxy=settings.auth_proxy or None)
    try:
        client.post(
            f"{settings.api_host}/open-api/od/v1/profile/createWsSess",
            json={"source": "API", "userId": session.client_id},
            headers={"Authorization": f"Bearer {session.user_session}"},
        )
    except httpx.HTTPError:
        logger.warning("createWsSess call failed — WS connect may be rejected", exc_info=True)
    finally:
        if owns_client:
            client.close()


WsProbeResult = Literal["alive", "dead", "unknown"]


def probe_ws_session(
    settings: AliceBlueSettings,
    session: AliceBlueSession,
    *,
    http_client: httpx.Client | None = None,
) -> WsProbeResult:
    """Read-only liveness check for `GET /aliceblue/status` — does the cached
    `user_session` still register a WS session server-side?

    Same `POST …/profile/createWsSess` as `create_ws_session`, but this one
    *inspects the status code* (which `create_ws_session` deliberately
    discards, since for its real caller a failure surfaces as the WS connect's
    own auth rejection). `401`/`403` → `"dead"` (this is the response actually
    observed for an expired Alice Blue token); any `2xx` → `"alive"`; anything
    else — `5xx`, `429`, a non-HTTP transport error, a timeout — → `"unknown"`
    (a transient blip must not flip the UI to "not connected"). Never raises.

    Deliberately does **not** clear the session or touch the WS reconnect
    loop — the loop self-heals the moment a valid token exists, which is
    wanted for failback; this is purely an honest mirror.

    Timeout is 5s (not `create_ws_session`'s 15s) because this sits on a
    status endpoint the frontend polls.
    """
    owns_client = http_client is None
    client = http_client or httpx.Client(timeout=5.0, proxy=settings.auth_proxy or None)
    try:
        response = client.post(
            f"{settings.api_host}/open-api/od/v1/profile/createWsSess",
            json={"source": "API", "userId": session.client_id},
            headers={"Authorization": f"Bearer {session.user_session}"},
        )
    except httpx.HTTPError:
        logger.warning("createWsSess liveness probe failed (transport) — treating as unknown")
        return "unknown"
    finally:
        if owns_client:
            client.close()

    if response.status_code in (401, 403):
        return "dead"
    if 200 <= response.status_code < 300:
        return "alive"
    logger.warning(
        "createWsSess liveness probe returned HTTP %d — treating as unknown", response.status_code
    )
    return "unknown"
