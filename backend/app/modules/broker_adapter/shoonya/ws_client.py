"""Single shared WebSocket connection — Shoonya (like every Noren-OMS
broker) "only supports one connection per session" (see
`broker_port.BrokerPort.subscribe_quotes`'s own docstring, and
`market_data/registry.py`'s identical reasoning for why this codebase
always shares one ingestion service across every underlying rather than
one-per-instrument). `ShoonyaWSClient` is that one connection: every
`subscribe`/`unsubscribe` call multiplexes onto it, never opens a second.

Runs its own background thread calling `websockets.sync.client` (a
blocking, non-asyncio client — available since `websockets` 13, already
this project's pinned minimum) rather than an asyncio event loop in a
thread, which is simpler and matches `MockBrokerAdapter._stream_loop`'s own
plain-`threading.Thread` shape exactly.

**Live-tested against a real account, still unresolved**: the auth
handshake (`_authenticate`) consistently gets `{"t": "ck", "s": "NOT_OK"}`
back, even though every other candidate cause was checked and ruled out —
`susertoken` (not the OAuth `access_token`) is confirmed used, the message
fields match the reference `NorenApi.py` implementation exactly (including
`uid`/`actid` both being the same value, per that reference), REST and WS
both connect from the same whitelisted IP, and both `NorenWSAPI` and
`NorenWSTP` hosts were tried, as were three URL forms (bare, `?token=`,
`?access_token=`) — every attempt gets the identical rejection at the
identical point (the WebSocket connection itself always succeeds; only the
post-connect auth frame is ever rejected). This rules out URL/host/field/
token-type mistakes on this client's side. Next step is Shoonya's own API
support, not further guessing at the wire format.

**Two more narrow, still-untested variables**, added after an external
second opinion on the `NOT_OK`: (1) `source` was always hardcoded `"API"`
(the classic-QuickAuth convention) and never itself varied — now
`ShoonyaSettings.ws_auth_source`, overridable via env with no redeploy,
in case this session's OAuth-issued token registers its origin
differently than a direct API login would; (2) `_authenticate` now logs
the literal `uid`/`actid` values sent (never `susertoken`) so the next
live attempt can actually see, rather than assume, whether `actid` (which
flows through from `GenAcsTok`'s own response field, not a static config
value — see `auth.py`'s `exchange_code_for_token`) comes back carrying an
unexpected suffix.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass

from websockets.sync.client import connect
from websockets.sync.connection import Connection

from app.modules.broker_adapter.base.broker_port import DepthCallback, TickCallback
from app.modules.broker_adapter.shoonya.normalizer import (
    NormalizationError,
    parse_depth,
    parse_tick,
)

logger = logging.getLogger("app.broker_adapter.shoonya.ws")

# Reconnect backoff — capped, not exponential-forever, since a broker outage
# lasting hours shouldn't turn into a multi-hour sleep before the next retry.
_RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10, 15, 30)

# App-level keepalive Noren's own protocol convention expects on top of the
# WebSocket ping/pong frames `websockets.sync.client.connect` already sends
# automatically — cheap to include even if the server turns out not to need
# it; a missing one, if it turns out to be required, silently drops the
# connection with no error to debug from.
_HEARTBEAT_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True)
class _SubscriptionEntry:
    contract_symbol: str
    exchange: str
    token: str

    @property
    def key(self) -> str:
        return f"{self.exchange}|{self.token}"


class ShoonyaWSClient:
    def __init__(
        self,
        ws_host: str,
        *,
        uid: str,
        actid: str,
        access_token: str,
        on_tick: TickCallback,
        on_depth: DepthCallback | None = None,
        source: str = "API",
    ) -> None:
        self._ws_host = ws_host
        self._uid = uid
        self._actid = actid
        self._access_token = access_token
        self._on_tick = on_tick
        self._on_depth = on_depth
        self._source = source

        self._entries_by_key: dict[str, _SubscriptionEntry] = {}
        self._lock = threading.Lock()

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        # Give the connect+auth handshake a bounded moment to complete so a
        # caller that immediately calls `subscribe` doesn't race the
        # connection — matches `subscribe`'s own tolerance for "not yet
        # connected" (it queues into `_entries_by_key` regardless and the
        # next reconnect cycle sends it), this is purely to make the common
        # case (start, then subscribe) not silently wait a full cycle.
        self._connected.wait(timeout=5.0)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def subscribe(self, entries: list[tuple[str, str, str]]) -> None:
        """`entries` is `(contract_symbol, exchange, broker_token)` triples —
        the adapter resolves `broker_token` from its own instrument cache
        before calling this, since the WS client itself has no DB/instrument
        knowledge.
        """
        parsed = [_SubscriptionEntry(*entry) for entry in entries]
        with self._lock:
            for entry in parsed:
                self._entries_by_key[entry.key] = entry
        self._send_subscribe(parsed)

    def unsubscribe(self, contract_symbols: list[str]) -> None:
        with self._lock:
            to_remove = [
                entry
                for entry in self._entries_by_key.values()
                if entry.contract_symbol in contract_symbols
            ]
            for entry in to_remove:
                del self._entries_by_key[entry.key]
        if to_remove:
            self._send_unsubscribe(to_remove)

    # -- connection lifecycle -------------------------------------------------

    def _run(self) -> None:
        backoff_index = 0
        while not self._stop.is_set():
            try:
                with connect(self._ws_host, open_timeout=10) as ws:
                    self._authenticate(ws)
                    self._resubscribe_all(ws)
                    self._connected.set()
                    backoff_index = 0
                    self._receive_loop(ws)
            except Exception:
                logger.exception("Shoonya WebSocket connection dropped; reconnecting")
            self._connected.clear()
            if self._stop.is_set():
                return
            delay = _RECONNECT_BACKOFF_SECONDS[
                min(backoff_index, len(_RECONNECT_BACKOFF_SECONDS) - 1)
            ]
            backoff_index += 1
            self._stop.wait(delay)

    def _authenticate(self, ws: Connection) -> None:
        # Deliberately logs uid/actid/source (account identifiers, not
        # secrets) but never susertoken — this is the one place we can
        # actually see, per live attempt, whether `actid` came back from
        # `GenAcsTok` carrying an unexpected suffix (e.g. `_U`) rather than
        # guessing at it, per the still-open NOT_OK investigation.
        logger.info(
            "Shoonya WebSocket auth attempt: uid=%r actid=%r source=%r",
            self._uid,
            self._actid,
            self._source,
        )
        ws.send(
            json.dumps(
                {
                    "t": "c",
                    "uid": self._uid,
                    "actid": self._actid,
                    "susertoken": self._access_token,
                    "source": self._source,
                }
            )
        )
        ack_raw = ws.recv(timeout=10)
        ack = json.loads(ack_raw)
        if ack.get("t") != "ck" or ack.get("s") != "OK":
            raise ConnectionError(f"Shoonya WebSocket auth rejected: {ack!r}")

    def _resubscribe_all(self, ws: Connection) -> None:
        with self._lock:
            entries = list(self._entries_by_key.values())
        if entries:
            self._send_subscribe(entries, ws=ws)

    def _send_subscribe(
        self, entries: list[_SubscriptionEntry], *, ws: Connection | None = None
    ) -> None:
        if not entries:
            return
        keys = "#".join(entry.key for entry in entries)
        self._send({"t": "t", "k": keys}, ws=ws)
        if self._on_depth is not None:
            self._send({"t": "d", "k": keys}, ws=ws)

    def _send_unsubscribe(self, entries: list[_SubscriptionEntry]) -> None:
        keys = "#".join(entry.key for entry in entries)
        self._send({"t": "u", "k": keys})

    def _send(self, message: dict, *, ws: Connection | None = None) -> None:
        """No-ops rather than raising when there's no live connection to
        send on — a `subscribe` call between reconnect attempts is
        recovered by `_resubscribe_all` on the next successful connect, so
        losing this particular send isn't a correctness gap.
        """
        target = ws
        if target is None:
            return
        try:
            target.send(json.dumps(message))
        except Exception:
            logger.exception("Shoonya WebSocket send failed for %r", message)

    def _receive_loop(self, ws: Connection) -> None:
        last_heartbeat = time.monotonic()
        while not self._stop.is_set():
            try:
                raw = ws.recv(timeout=1.0)
            except TimeoutError:
                if time.monotonic() - last_heartbeat >= _HEARTBEAT_INTERVAL_SECONDS:
                    self._send({"t": "h"}, ws=ws)
                    last_heartbeat = time.monotonic()
                continue

            self._handle_message(raw)

    def _handle_message(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            logger.warning("Shoonya WebSocket sent non-JSON frame: %r", raw)
            return

        msg_type = message.get("t")
        with self._lock:
            entry = self._entries_by_key.get(f"{message.get('e', '')}|{message.get('tk', '')}")
        if entry is None:
            return

        try:
            if msg_type in ("tk", "tf"):
                self._on_tick(parse_tick(message, entry.contract_symbol))
            elif msg_type in ("dk", "df") and self._on_depth is not None:
                self._on_depth(parse_depth(message, entry.contract_symbol))
        except NormalizationError:
            logger.exception("Failed to normalize Shoonya WebSocket message: %r", message)
