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
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger("app.market_data.angel_one.ws")

_RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10, 15, 30)

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
    ) -> None:
        self._auth_token = auth_token
        self._api_key = api_key
        self._client_code = client_code
        self._feed_token = feed_token
        self._on_tick = on_tick
        self._on_depth = on_depth

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
            self._stop.wait(delay)

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
            sws.connect()  # blocks; on_open fires (and resubscribes) once the handshake completes
        finally:
            with self._lock:
                if self._sws is sws:
                    self._sws = None

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
        del wsapp
        tick = parse_angel_tick(data)
        if tick is not None:
            self._on_tick(tick)
        if self._on_depth is not None:
            depth = parse_angel_depth(data)
            if depth is not None:
                self._on_depth(depth)

    def _handle_close(self, wsapp: object) -> None:
        del wsapp
        logger.warning("Angel One WebSocket closed")

    def _handle_error(self, error_type: str, message: str) -> None:
        logger.warning("Angel One WebSocket error: %s - %s", error_type, message)
