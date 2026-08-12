"""Wraps Angel One's SmartStream WebSocket (`smartapi-python`'s
`SmartWebSocketV2`) behind this codebase's own connection-supervision
discipline. The only file in this system that imports `SmartApi` — a
process that never touches Angel One never pays for it, same isolation
`composition.py` already documents for Shoonya's `httpx`/`websockets`.

**Binary tick unpacking is fully delegated to the SDK.** SmartStream
transmits binary frames; a hand-rolled byte-offset parser here, with no live
account to verify against, risks silently corrupting a price feeding a real
stop-loss decision — categorically worse than a JSON parsing bug, which
fails loud. `SmartWebSocketV2._parse_binary_data` (read directly from the
installed package, `smartapi-python==1.5.5`,
`SmartApi/smartWebSocketV2.py`) does that work; this module only translates
its already-parsed dict into `RawAngelTick`.

**Confirmed directly from the installed SDK source** (not guessed): prices
in a SNAP_QUOTE (mode 3) packet — `last_traded_price` and the best-5 buy/
sell `price` fields — are raw integers requiring `/100` to get real rupees
(`struct.unpack(..., "q")` reads them as plain 8-byte ints, no decimal
handling in the SDK itself). `on_error`'s real call signature is
`(error_type: str, message: str)`, two plain strings — not `(wsapp, error)`
as the shape might suggest; the SDK's own base-class stub (`on_error(self)`,
zero args) is misleading, its actual internal call sites
(`self.on_error("Reconnect Error", str(e))` /
`self.on_error("Max retry attempt reached", "Connection closed")`) are what
this class's `_handle_error` actually has to match.

**The SDK's own internal retry is disabled** (`max_retry_attempt=0`) in
favor of this class's own capped-backoff reconnect loop
(`_RECONNECT_BACKOFF_SECONDS`), same shape as `ShoonyaWSClient._run` — the
SDK's own retry only attempts a fixed, small number of same-process retries
(inside `_on_error`'s own recursive `connect()` call) before giving up
permanently; this system needs to keep trying for the life of the process.

**2026-08-11 audit, requested by the team against Angel One's own WS 2.0
docs and cross-checked here directly against the installed SDK source
(`smartWebSocketV2.py`, the ground truth, not the docs page — which is a
JS-rendered SPA this session couldn't fetch)**: mandatory headers
(`auth_token`/`api_key`/`client_code`/`feed_token`), integer exchange-type
mappings (`NSE_CM=1`, `NSE_FO=2`), and integer subscription modes
(`SNAP_QUOTE=3`) all confirmed already correct in this file and
`angel_one.py` — no string-based mappings anywhere, and "multiple modes for
one token in one request" is structurally impossible here since this
system only ever sends `MODE_SNAP_QUOTE`, never a list. The one genuine gap
found: `_handle_data` had no exception handling at all — a raising
`parse_angel_tick`/`parse_angel_depth` or (more likely) the external
`on_tick`/`on_depth` callback could propagate up through the SDK's own
`_on_data` into `websocket-client`'s dispatch loop, where it can be
swallowed/misattributed as a connection failure and trigger this class's
own reconnect loop over what was actually a code bug, not a network issue.
Fixed with a try/except in `_handle_data` (see its own docstring) — real,
defensive hardening, but **not** the explanation for the WS failure this
system has actually observed live, repeatedly, since Phase 5: every
recorded failure fails at `subscribe()` itself (`socket is already
closed`), before a single tick would ever reach `_handle_data` — this fix
addresses a different failure mode than the one currently seen.

**2026-08-11 — root cause found and live-confirmed.** `ANGELONE_AUTH_PROXY`
(a Webshare proxy, live-confirmed via geo-IP lookup to be UK-based, not
India) routes every REST call (login, candle history) through a different
egress IP than this box's own — but this class's WS connection has always
connected *directly*, from this box's own IP, never through that proxy.
Angel's WS streaming gateway appears to tie a session's streaming
authorization to the IP that performed the REST login: a token minted via
the proxy's IP, then presented from a *different* IP over WS, gets silently
rejected — matching the exact observed symptom (handshake succeeds, then an
immediate close right at `subscribe()`) precisely. **Live-proven on the OCI
deployment**: the identical token, over the identical WS host, succeeds
completely — real subscribe ack, 32 real binary tick messages streamed —
the moment the WS connection is routed through the same proxy IP the login
used. A direct (no-proxy) REST login was also tested and still times out,
ruling out "the portal's IP allowlist was updated" as an alternative
explanation.

`SmartWebSocketV2.connect()` builds a plain `websocket.WebSocketApp` with no
proxy parameters at all, so `_connect_and_run` doesn't call it when a proxy
is configured — it replicates that method's own few lines directly (see
below) instead, but hands the `WebSocketApp` constructor the SDK instance's
own `_on_open`/`_on_data`/`_on_close`/`_on_error`/`_on_ping`/`_on_pong`
methods (not `sws.connect()` itself) as callbacks, so binary tick parsing,
control-message handling, and heartbeat logic all stay exactly as
SDK-owned as before — nothing about *what* gets parsed changes, only *which
IP the socket itself opens from*.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

logger = logging.getLogger("app.market_data.angel_one.ws")

_RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10, 15, 30)

# 2026-08-11: a WS connection that keeps failing to (re)connect might just be
# holding stale tokens the *WS* side has stopped honoring even though the
# REST side never noticed (the two aren't the same session concept). 3
# consecutive failures, not 1 -- a single dropped connection is normal
# network noise (real live behavior; see _RECONNECT_BACKOFF_SECONDS' own
# short first delays), and forcing a fresh REST login on every single retry
# would itself risk hammering the login endpoint. 3 is "a set number of
# immediate retries" per the instruction this was built from, not tuned
# against any live evidence -- there is none yet, since every recorded WS
# failure to date happens on a token that was *already* fresh (see this
# module's own audit note above), so this fires only after 3 fresh-login
# attempts have already run and still failed.
_TOKEN_REFRESH_AFTER_CONSECUTIVE_FAILURES = 3

# SmartWebSocketV2 class constants, mirrored here so callers don't need to
# import the SDK just to reference a mode/exchange-type code.
MODE_SNAP_QUOTE = 3
EXCHANGE_TYPE_NSE_CM = 1  # underlying index/cash segment
EXCHANGE_TYPE_NSE_FO = 2  # NFO options/futures


@dataclass(frozen=True)
class RawAngelTick:
    token: str
    ltp: float
    bid: float
    ask: float
    volume: int
    oi: int | None
    ts: datetime


@dataclass(frozen=True)
class RawAngelDepthLevel:
    price: float
    qty: int
    orders: int


@dataclass(frozen=True)
class RawAngelDepth:
    token: str
    bid_levels: tuple[RawAngelDepthLevel, ...]
    ask_levels: tuple[RawAngelDepthLevel, ...]
    ts: datetime


def _price(raw: dict, key: str) -> float:
    value = raw.get(key)
    return float(value) / 100.0 if value is not None else 0.0


def parse_angel_tick(raw: dict) -> RawAngelTick | None:
    """Translates the SDK's own parsed SNAP_QUOTE dict into `RawAngelTick`.
    Returns `None` for anything that isn't a genuine price tick (a control
    message the SDK already filtered out before calling `on_data`, or an
    unexpected shape) rather than raising — one malformed message must not
    kill the whole stream.
    """
    if "token" not in raw or "last_traded_price" not in raw:
        return None
    token = str(raw["token"])
    ltp = _price(raw, "last_traded_price")

    best_buy = raw.get("best_5_buy_data") or []
    best_sell = raw.get("best_5_sell_data") or []
    bid = float(best_buy[0]["price"]) / 100.0 if best_buy else ltp
    ask = float(best_sell[0]["price"]) / 100.0 if best_sell else ltp

    volume = int(raw.get("volume_trade_for_the_day", 0) or 0)
    oi_raw = raw.get("open_interest")
    oi = int(oi_raw) if oi_raw is not None else None

    # exchange_timestamp is epoch milliseconds per Angel's own docs — not
    # independently re-verified against a live tick; if timestamps come back
    # implausible, check whether this is actually epoch seconds instead.
    exchange_ts = raw.get("exchange_timestamp")
    ts = (
        datetime.fromtimestamp(int(exchange_ts) / 1000.0, tz=UTC)
        if exchange_ts
        else datetime.now(UTC)
    )
    return RawAngelTick(token=token, ltp=ltp, bid=bid, ask=ask, volume=volume, oi=oi, ts=ts)


def parse_angel_depth(raw: dict) -> RawAngelDepth | None:
    """SNAP_QUOTE mode already carries the SDK's own `best_5_buy_data`/
    `best_5_sell_data` (each `{"flag", "quantity", "price", "no of orders"}`,
    per the installed SDK's own field names — note the space in "no of
    orders", not a typo here) — reused directly rather than a second
    DEPTH-mode (4) subscription, which this system's option-contract-only
    depth need doesn't require the fuller 20-level book DEPTH mode provides.
    """
    if "token" not in raw:
        return None
    best_buy = raw.get("best_5_buy_data") or []
    best_sell = raw.get("best_5_sell_data") or []
    if not best_buy and not best_sell:
        return None

    def _levels(entries: list[dict]) -> tuple[RawAngelDepthLevel, ...]:
        return tuple(
            RawAngelDepthLevel(
                price=float(entry.get("price", 0)) / 100.0,
                qty=int(entry.get("quantity", 0)),
                orders=int(entry.get("no of orders", 0)),
            )
            for entry in entries
        )

    exchange_ts = raw.get("exchange_timestamp")
    ts = (
        datetime.fromtimestamp(int(exchange_ts) / 1000.0, tz=UTC)
        if exchange_ts
        else datetime.now(UTC)
    )
    return RawAngelDepth(
        token=str(raw["token"]),
        bid_levels=_levels(best_buy),
        ask_levels=_levels(best_sell),
        ts=ts,
    )


class AngelWSClient:
    """One shared connection for the process — same "single shared stream,
    not one per instrument" discipline `ShoonyaWSClient` already establishes;
    every `subscribe`/`unsubscribe` call multiplexes onto it.
    """

    def __init__(
        self,
        *,
        auth_token: str,
        api_key: str,
        client_code: str,
        feed_token: str,
        on_tick: Callable[[RawAngelTick], None],
        on_depth: Callable[[RawAngelDepth], None] | None = None,
        token_refresh_callback: Callable[[], tuple[str, str]] | None = None,
        proxy_url: str = "",
    ) -> None:
        self._auth_token = auth_token
        self._api_key = api_key
        self._client_code = client_code
        self._feed_token = feed_token
        self._on_tick = on_tick
        self._on_depth = on_depth
        # 2026-08-11, live-confirmed: Angel's WS gateway ties a session's
        # streaming authorization to the IP that performed the REST login
        # (see this module's own docstring for the full finding) -- when
        # ANGELONE_AUTH_PROXY is set, the WS connection must go through the
        # *same* proxy, not connect directly, or every subscribe gets
        # silently rejected. Parsed once here, not per-connection-attempt.
        parsed_proxy = urlparse(proxy_url) if proxy_url else None
        self._proxy_host = parsed_proxy.hostname if parsed_proxy else None
        self._proxy_port = parsed_proxy.port if parsed_proxy else None
        self._proxy_auth = (
            (parsed_proxy.username, parsed_proxy.password)
            if parsed_proxy and parsed_proxy.username
            else None
        )
        # Optional: forces a genuinely fresh REST login (bypassing
        # AngelOneMarketDataProvider.connect()'s own idempotency check) and
        # returns (auth_token, feed_token) -- called from this class's own
        # background thread only, after _TOKEN_REFRESH_AFTER_CONSECUTIVE_
        # FAILURES consecutive reconnect failures. None in tests/any caller
        # that doesn't want this behavior (e.g. a fake provider with no
        # real login to perform).
        self._token_refresh_callback = token_refresh_callback

        # (exchange_type, token) -> True, accumulated across subscribe calls
        # so a reconnect can resubscribe everything, same "queue regardless,
        # replay on (re)connect" shape as ShoonyaWSClient._resubscribe_all.
        self._subscriptions: dict[tuple[int, str], bool] = {}
        self._lock = threading.Lock()

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._sws: object | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._connected.wait(timeout=10.0)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            sws = self._sws
        if sws is not None:
            sws.close_connection()  # type: ignore[attr-defined]
        if self._thread is not None:
            self._thread.join(timeout=10.0)

    def subscribe(self, entries: list[tuple[str, int]]) -> None:
        """`entries` is `(angel_token, exchange_type)` pairs."""
        with self._lock:
            for token, exchange_type in entries:
                self._subscriptions[(exchange_type, token)] = True
            sws = self._sws
        if sws is not None:
            self._send_subscribe(sws, entries)

    def unsubscribe(self, entries: list[tuple[str, int]]) -> None:
        with self._lock:
            for token, exchange_type in entries:
                self._subscriptions.pop((exchange_type, token), None)
            sws = self._sws
        if sws is not None and entries:
            self._send_unsubscribe(sws, entries)

    @staticmethod
    def _grouped_token_list(entries: list[tuple[str, int]]) -> list[dict]:
        by_exchange: dict[int, list[str]] = {}
        for token, exchange_type in entries:
            by_exchange.setdefault(exchange_type, []).append(token)
        return [
            {"exchangeType": exchange_type, "tokens": tokens}
            for exchange_type, tokens in by_exchange.items()
        ]

    def _send_subscribe(self, sws: object, entries: list[tuple[str, int]]) -> None:
        if not entries:
            return
        try:
            sws.subscribe(  # type: ignore[attr-defined]
                "tradingbot1", MODE_SNAP_QUOTE, self._grouped_token_list(entries)
            )
        except Exception:
            logger.exception("Angel One WS subscribe failed for %r", entries)

    def _send_unsubscribe(self, sws: object, entries: list[tuple[str, int]]) -> None:
        try:
            sws.unsubscribe(  # type: ignore[attr-defined]
                "tradingbot1", MODE_SNAP_QUOTE, self._grouped_token_list(entries)
            )
        except Exception:
            logger.exception("Angel One WS unsubscribe failed for %r", entries)

    def _run(self) -> None:
        backoff_index = 0
        while not self._stop.is_set():
            try:
                self._connect_and_run()
                backoff_index = 0
            except Exception:
                logger.exception("Angel One WebSocket connection dropped; reconnecting")
            self._connected.clear()
            if self._stop.is_set():
                return
            delay = _RECONNECT_BACKOFF_SECONDS[
                min(backoff_index, len(_RECONNECT_BACKOFF_SECONDS) - 1)
            ]
            backoff_index += 1
            # `==`, not `>=` -- fires exactly once per failure streak (the
            # exact attempt count crossing the threshold), not on every
            # attempt thereafter, which would risk hammering the login
            # endpoint if the underlying problem isn't actually the tokens.
            if backoff_index == _TOKEN_REFRESH_AFTER_CONSECUTIVE_FAILURES:
                self._refresh_tokens()
            self._stop.wait(delay)

    def _refresh_tokens(self) -> None:
        if self._token_refresh_callback is None:
            return
        try:
            auth_token, feed_token = self._token_refresh_callback()
        except Exception:
            logger.exception(
                "Angel One WS token refresh failed after %d consecutive reconnect "
                "failures; next attempt retries with the existing (possibly stale) tokens",
                _TOKEN_REFRESH_AFTER_CONSECUTIVE_FAILURES,
            )
            return
        self._auth_token = auth_token
        self._feed_token = feed_token
        logger.warning(
            "Angel One WS: refreshed auth/feed tokens after %d consecutive reconnect "
            "failures",
            _TOKEN_REFRESH_AFTER_CONSECUTIVE_FAILURES,
        )

    def _connect_and_run(self) -> None:
        # Local import: the one place in this codebase that ever imports
        # SmartApi (see module docstring).
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        sws = SmartWebSocketV2(
            self._auth_token,
            self._api_key,
            self._client_code,
            self._feed_token,
            max_retry_attempt=0,
        )
        sws.on_open = self._handle_open
        sws.on_data = self._handle_data
        sws.on_close = self._handle_close
        sws.on_error = self._handle_error

        with self._lock:
            self._sws = sws
        try:
            if self._proxy_host is not None:
                self._connect_via_proxy(sws)
            else:
                sws.connect()  # blocks; on_open fires (and resubscribes) once handshake completes
        finally:
            with self._lock:
                if self._sws is sws:
                    self._sws = None

    def _connect_via_proxy(self, sws: object) -> None:
        """Reimplements `SmartWebSocketV2.connect()`'s own few lines
        directly instead of calling it — that method builds a plain
        `websocket.WebSocketApp` with no proxy parameters at all, so a
        proxy-routed connection can't go through it. Hands the
        `WebSocketApp` constructor `sws`'s own `_on_open`/`_on_data`/
        `_on_close`/`_on_error`/`_on_ping`/`_on_pong` (not `sws.connect()`
        itself) as callbacks, so binary tick parsing, control-message
        handling, and heartbeat logic all stay exactly as SDK-owned as the
        no-proxy path above — this only changes which IP the socket itself
        opens from. See module docstring for the live-confirmed finding
        that motivated this.
        """
        import ssl

        import websocket

        # Narrows for mypy -- the caller only reaches this method after
        # checking self._proxy_host is not None, but that check doesn't
        # cross the method boundary on its own.
        assert self._proxy_host is not None
        assert self._proxy_port is not None

        headers = {
            "Authorization": sws.auth_token,  # type: ignore[attr-defined]
            "x-api-key": sws.api_key,  # type: ignore[attr-defined]
            "x-client-code": sws.client_code,  # type: ignore[attr-defined]
            "x-feed-token": sws.feed_token,  # type: ignore[attr-defined]
        }
        wsapp = websocket.WebSocketApp(
            sws.ROOT_URI,  # type: ignore[attr-defined]
            header=headers,
            on_open=sws._on_open,  # type: ignore[attr-defined]  # noqa: SLF001
            on_error=sws._on_error,  # type: ignore[attr-defined]  # noqa: SLF001
            on_close=sws._on_close,  # type: ignore[attr-defined]  # noqa: SLF001
            on_data=sws._on_data,  # type: ignore[attr-defined]  # noqa: SLF001
            on_ping=sws._on_ping,  # type: ignore[attr-defined]  # noqa: SLF001
            on_pong=sws._on_pong,  # type: ignore[attr-defined]  # noqa: SLF001
        )
        sws.wsapp = wsapp  # type: ignore[attr-defined]  # mirrors what connect() itself sets
        wsapp.run_forever(
            sslopt={"cert_reqs": ssl.CERT_NONE},
            ping_interval=sws.HEART_BEAT_INTERVAL,  # type: ignore[attr-defined]
            http_proxy_host=self._proxy_host,
            http_proxy_port=self._proxy_port,
            # websocket-client's own stub types http_proxy_auth as required
            # (no `| None`) despite its real default being None (unauthenticated
            # proxy) -- a stub inaccuracy, not a real constraint.
            http_proxy_auth=self._proxy_auth,  # type: ignore[arg-type]
            proxy_type="http",
        )

    def _handle_open(self, wsapp: object) -> None:
        del wsapp
        logger.info("Angel One WebSocket connected")
        with self._lock:
            sws = self._sws
            entries = [(token, exchange_type) for (exchange_type, token) in self._subscriptions]
        if sws is not None:
            self._send_subscribe(sws, entries)
        self._connected.set()

    def _handle_data(self, wsapp: object, data: dict) -> None:
        """**2026-08-10 audit finding**: this had zero exception handling —
        an unhandled exception here (in `parse_angel_tick`/`parse_angel_depth`,
        or in the external `_on_tick`/`_on_depth` callback this hands off to,
        which reaches all the way into `AngelOneMarketDataProvider`'s own
        tick-cache update and a live DB write) propagates up through the
        SDK's own `_on_data` into `websocket-client`'s dispatch loop inside
        `run_forever()`. That library can swallow/misattribute a callback
        exception as a connection failure, which this class's own `_run`
        loop would then log as "connection dropped; reconnecting" — a real
        code bug masquerading as a network issue, with the actual traceback
        never surfacing. Caught by external audit, not by any live symptom
        seen so far: every live WS failure recorded to date fails at
        `subscribe()` (`socket is already closed`), before a single tick
        would ever reach this method — so this fix is real, defensive
        hardening for a *different* failure mode than the one currently
        observed, not a fix for that one.

        The raw-data line was `.warning()`, deliberately, while this method
        had never been confirmed to receive a single live tick — this
        deployment has no logging configuration anywhere (see
        `shoonya/ws_client.py`'s own `_authenticate` docstring for the
        identical reasoning), so anything below WARNING is invisible in the
        real logs. **2026-08-11: confirmed live** — the proxy-routing fix
        (see module docstring) got real ticks flowing, ~230/minute for a
        single index token, so that line is now `.debug()` (silent by
        default, same "no config anywhere" reasoning applied the other
        direction): 230+ WARNING lines/minute in steady-state production is
        noise, not diagnostics, and would have flooded real logs for the
        rest of every trading day.
        """
        del wsapp
        logger.debug("Angel One WS raw data received: %r", data)
        try:
            tick = parse_angel_tick(data)
            if tick is not None:
                self._on_tick(tick)
            if self._on_depth is not None:
                depth = parse_angel_depth(data)
                if depth is not None:
                    self._on_depth(depth)
        except Exception:
            logger.exception(
                "Angel One WS on_data callback raised — swallowed here so it can never "
                "propagate into the SDK's own dispatch loop and get misattributed as a "
                "connection drop; see this method's own docstring"
            )

    def _handle_close(self, wsapp: object) -> None:
        del wsapp
        logger.warning("Angel One WebSocket closed")

    def _handle_error(self, error_type: str, message: str) -> None:
        logger.warning("Angel One WebSocket error: %s - %s", error_type, message)
