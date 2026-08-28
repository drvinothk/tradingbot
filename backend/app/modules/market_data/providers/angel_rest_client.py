"""Thin synchronous REST wrapper over Angel One's `loginByPassword` — the
only REST call this system needs to make to Angel directly (the scrip master
file is a plain unauthenticated download, handled by `scrip_master.py`; the
live tick stream itself is SmartStream WebSocket, handled by
`angel_ws_client.py`). Hand-rolled rather than the `smartapi-python` SDK
here — this is a single, simple JSON REST call, easy to verify and easy to
fake in tests (unlike the binary WS protocol, where the SDK is used
specifically — see `angel_ws_client.py`'s own docstring for that split).

Endpoint/headers/body shape are from the user-supplied Angel One SmartAPI doc
extraction (2026-08), not yet independently verified against a live account.
"""

from __future__ import annotations

import logging

import httpx

from app.modules.broker_adapter.base.errors import (
    BrokerAuthError,
    BrokerConnectivityError,
    BrokerRateLimitedError,
)

logger = logging.getLogger("app.market_data.angel_one")


class AngelOneLoginError(BrokerAuthError):
    """`loginByPassword` rejected the request — bad credentials, TOTP drift,
    or a genuinely malformed request. Carries Angel's own error message
    (`message`/`errorcode` in the response body) so callers can log the real
    reason rather than a generic failure.
    """

    def __init__(self, message: str, error_code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code


# Angel One's own documented interval codes for the historical-candle
# endpoint (ONE_MINUTE / THREE_MINUTE / FIVE_MINUTE / TEN_MINUTE /
# FIFTEEN_MINUTE / THIRTY_MINUTE / ONE_HOUR / ONE_DAY) — picks the closest
# supported bucket at or below the requested timeframe, same "whole-unit
# granularity, no seconds on the wire" pragmatism as Shoonya's own TPSeries
# wrapper.
_CANDLE_INTERVALS: tuple[tuple[int, str], ...] = (
    (60, "ONE_MINUTE"),
    (180, "THREE_MINUTE"),
    (300, "FIVE_MINUTE"),
    (600, "TEN_MINUTE"),
    (900, "FIFTEEN_MINUTE"),
    (1800, "THIRTY_MINUTE"),
    (3600, "ONE_HOUR"),
    (86400, "ONE_DAY"),
)


def _interval_code_for(timeframe_seconds: int) -> str:
    code = _CANDLE_INTERVALS[0][1]
    for seconds, candidate in _CANDLE_INTERVALS:
        if seconds <= timeframe_seconds:
            code = candidate
        else:
            break
    return code


class AngelOneRestClient:
    def __init__(
        self,
        rest_host: str,
        *,
        api_key: str,
        mac_address: str,
        http_client: httpx.Client | None = None,
        auth_proxy: str = "",
    ) -> None:
        """`auth_proxy`, when set, routes every call this client makes
        (`loginByPassword` *and* `getCandleData` — both hit `apiconnect.
        angelone.in`, the same gateway live-confirmed to time out from the
        OCI VM's IP while responding instantly to the identical request
        from an unrelated residential IP; there's no reason to expect
        `getCandleData` behaves differently, so it goes through the same
        proxy rather than being left broken). Never applies to the
        WebSocket (a different host, `smartapisocket.angelone.in`, and
        deliberately excluded per `AngelOneSettings.auth_proxy`'s own
        docstring) or to the scrip-master download (a third host, already
        confirmed reachable directly). Ignored entirely when an explicit
        `http_client` is supplied (tests, or a caller managing its own).
        """
        self._rest_host = rest_host.rstrip("/")
        self._api_key = api_key
        self._mac_address = mac_address
        self._client = http_client or httpx.Client(timeout=15.0, proxy=auth_proxy or None)
        self._owns_client = http_client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> AngelOneRestClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def login_by_password(self, client_code: str, password: str, totp: str) -> dict:
        """`POST /rest/auth/angelbroking/user/v1/loginByPassword`. Returns
        the parsed `data` object (`jwtToken`/`refreshToken`/`feedToken`) on
        success. **Flagged, not assumed**: Angel's broader docs elsewhere
        also reference `X-ClientLocalIP`/`X-ClientPublicIP` headers this
        extraction didn't include — if login rejects with a header-related
        error live, add those two here first.
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-PrivateKey": self._api_key,
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-MACAddress": self._mac_address,
        }
        body = {
            "clientcode": client_code,
            "password": password,
            "totp": totp,
            "state": "trading_bot_login",
        }

        try:
            response = self._client.post(
                f"{self._rest_host}/rest/auth/angelbroking/user/v1/loginByPassword",
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            # Deliberately BrokerConnectivityError, not BrokerAuthError, even
            # for a proxy failure specifically (httpx.ProxyError is a
            # TransportError, a subclass of HTTPError, already caught here):
            # a proxy being unreachable is "retry next cycle," never
            # "credentials are dead, force a fresh login" — the exact
            # distinction BrokerAuthError's own docstring in
            # broker_adapter/base/errors.py draws, and the reason
            # PositionManager._handle_broker_auth_error treats BrokerAuthError
            # as a mode-transition trigger. Misclassifying a transient proxy
            # blip as an auth failure would risk a spurious degraded_mode
            # transition on a live session over what's really just "the
            # relay hiccuped" — a real correctness bug in a
            # real-money code path, not just an inaccurate label.
            raise BrokerConnectivityError(f"Angel One login request failed: {exc}") from exc

        if response.status_code >= 400:
            raise AngelOneLoginError(
                f"Angel One login HTTP {response.status_code}: {response.text[:500]}"
            )

        try:
            parsed = response.json()
        except ValueError as exc:
            raise BrokerConnectivityError(
                f"Angel One login returned non-JSON response: {response.text[:500]}"
            ) from exc

        if not parsed.get("status"):
            raise AngelOneLoginError(
                str(parsed.get("message", "Angel One login rejected")),
                error_code=str(parsed.get("errorcode", "")),
            )

        data = parsed.get("data")
        if not isinstance(data, dict) or not data.get("feedToken"):
            raise AngelOneLoginError(
                f"Angel One login response missing expected data/feedToken: {parsed!r}"
            )
        return data

    def get_candle_data(
        self,
        jwt_token: str,
        exchange: str,
        symbol_token: str,
        from_dt: str,
        to_dt: str,
        timeframe_seconds: int,
    ) -> list[list]:
        """`POST /rest/secure/angelbroking/historical/v1/getCandleData` —
        **not part of the user-supplied doc extraction**; based on Angel
        One's publicly documented Historical Candle Data API from general
        knowledge, not independently re-verified against a live account or
        the current official docs. Flagged the same way every other
        researched-not-live-verified claim in this codebase is (see
        `docs/lessons-learned.md`) — confirm endpoint path, header set, and
        response row shape (`[timestamp, open, high, low, close, volume]`
        assumed here) against a real account before trusting this in
        production. `from_dt`/`to_dt` are `"YYYY-MM-DD HH:MM"` strings per
        Angel's documented convention.
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {jwt_token}",
            "X-PrivateKey": self._api_key,
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-MACAddress": self._mac_address,
        }
        body = {
            "exchange": exchange,
            "symboltoken": symbol_token,
            "interval": _interval_code_for(timeframe_seconds),
            "fromdate": from_dt,
            "todate": to_dt,
        }
        try:
            response = self._client.post(
                f"{self._rest_host}/rest/secure/angelbroking/historical/v1/getCandleData",
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise BrokerConnectivityError(f"Angel One getCandleData failed: {exc}") from exc

        if response.status_code >= 400:
            # Live-confirmed 2026-08-06: a 403 specifically for exceeding
            # the rate limit, distinct from every other 4xx/5xx this
            # endpoint can return (bad request, server error, etc.) — only
            # this specific case gets the dedicated rate-limit backoff
            # treatment upstream (see BrokerRateLimitedError's own
            # docstring); anything else stays a normal retry-next-cycle
            # BrokerConnectivityError.
            if response.status_code == 403 and "exceeding access rate" in response.text.lower():
                raise BrokerRateLimitedError(
                    f"Angel One getCandleData rate-limited (HTTP 403): {response.text[:500]}"
                )
            raise BrokerConnectivityError(
                f"Angel One getCandleData HTTP {response.status_code}: {response.text[:500]}"
            )
        try:
            parsed = response.json()
        except ValueError as exc:
            raise BrokerConnectivityError(
                f"Angel One getCandleData returned non-JSON response: {response.text[:500]}"
            ) from exc

        if not parsed.get("status"):
            message = str(parsed.get("message", parsed))
            # Live-confirmed 2026-08-06: a stale/expired token comes back as
            # a "soft" failure here (status: false, HTTP 200), not an HTTP
            # 401 -- exactly the "Invalid Token" message observed live after
            # a token outlived its server-side validity. BrokerAuthError
            # (not BrokerConnectivityError) so the caller invalidates the
            # cached token and the next call forces a fresh login, instead
            # of retrying the same dead token forever (the actual root
            # cause of that night's rate-limit exhaustion in the first
            # place).
            if message.strip().lower() == "invalid token":
                raise BrokerAuthError(f"Angel One getCandleData rejected: {message!r}")
            raise BrokerConnectivityError(f"Angel One getCandleData rejected: {message!r}")
        data = parsed.get("data")
        return list(data) if isinstance(data, list) else []
