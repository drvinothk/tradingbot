"""Single shared WebSocket connection to Alice Blue's ANT V3 market-data
feed. Deliberately mirrors `broker_adapter/shoonya/ws_client.py`'s already
hard-won shape almost line-for-line, **not** a coincidence — Alice Blue's
own WebSocket doc page (confirmed live 2026-08-21) shows the identical
Noren-OMS wire protocol Shoonya uses: `t:"tk"`/`"tf"` touchline
(full-then-partial) ticks, `#`-joined `EXCH|token` subscription keys, and a
heartbeat frame. Real, independent confirmation Alice Blue runs on the same
underlying platform family (reinforced by `alice_blue_scrip_master.py`'s own
finding: Alice's NSE index tokens, 26000/26009 for NIFTY 50/NIFTY BANK,
are byte-identical to Shoonya's).

Starting from Shoonya's *fixed* version (not its first, buggy draft) means
this client is built with two live-incident-driven fixes already applied,
not rediscovered the hard way a second time:

1. **`_live_ws` publish-on-connect, not send-with-explicit-`ws`.** The
   original Shoonya client only ever wrote to the socket when a caller
   passed an explicit `ws=` (only the background thread's own resubscribe/
   heartbeat calls did) — `subscribe()`/`unsubscribe()` from any other
   thread silently no-op'd on an already-open connection, dropping the
   request forever with no error. Fixed here from the start: `_run`
   publishes the live `Connection` to `self._live_ws`, `_send` always
   writes to whatever that currently is.
2. **Partial-touchline merge.** Noren's own protocol convention: `"tk"`
   (sent once, right after subscribing) is a complete snapshot; every
   `"tf"` after that carries only the fields that changed. Every message is
   merged onto a per-key running snapshot (`_last_known_by_key`) before
   parsing, so a partial update that doesn't happen to touch `lp` still
   parses correctly instead of raising and being dropped.

**Two things NOT carried over from Shoonya, both genuinely different here,
not oversights:**

- **Auth frame shape is Alice's own** — but Alice Blue's own WebSocket doc
  page turned out to be wrong about it in two real, live-confirmed ways
  (2026-08-21), neither guessable from the doc alone:
  1. The doc showed the bare `ws1.aliceblueonline.com/NorenWS` host with
     no trailing slash — that URL 301-redirects at the plain-HTTP level
     (confirmed: `curl`/`httpx` against it returns `Location: .../NorenWS/`),
     and `websockets.sync.client.connect` does not follow redirects during
     the handshake at all, so every connect attempt failed before
     authentication was ever reached. Fixed in `AliceBlueSettings.ws_host`'s
     own default (trailing slash added).
  2. Even with the trailing slash, every auth attempt using `uid`/`actid` =
     the bare `client_id` and `susertoken` = double-SHA-256 of
     `userSession` was rejected (`{"t":"ck","s":"NOT_OK"}`) — the doc's own
     ack-shape example (`{"t":"cf","k":"OK"}`) was *also* wrong; the real
     ack is the classic Noren `{"t":"ck","s":...}` shape. Root cause, found
     by reading the doc's "pre-connection requirements" section more
     carefully rather than guessing further: a separate `POST
     .../profile/createWsSess` call (`alice_blue_auth.create_ws_session`)
     must register the WS session server-side *before* every connect
     attempt, and `uid`/`actid` must be `f"{client_id}_API"`, not the bare
     `client_id` — see `create_ws_session`'s own docstring and
     `alice_blue.py`'s for exactly where each fix lives. Both fixes
     together, live-confirmed: real ticks for NIFTY 50 and NIFTY BANK
     streamed successfully the same day.
- **Depth (market-depth/5-level) messages are not implemented.** Alice
  Blue's own doc page only showed a touchline (LTP-only) example — no
  confirmed `df`/`dk` field shape to normalize against, unlike Shoonya's
  own (already-confirmed) `bp1..bp5`/`sp1..sp5`. `on_depth` is accepted for
  interface compatibility but never invoked; flagged here rather than
  guessing a shape from Shoonya's, since the two brokers' depth message
  fields are not confirmed to match even though touchline does.

**Live-verified 2026-08-21** — real ticks streamed for NIFTY 50 (token
26000) and NIFTY BANK (token 26009) via a standalone diagnostic script,
both the full `"tk"` snapshot and subsequent `"tf"` partial updates parsing
correctly through the existing merge logic above.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from websockets.sync.client import connect
from websockets.sync.connection import Connection

from app.modules.broker_adapter.base.broker_port import TickCallback
from app.modules.market_data.providers.alice_blue_normalizer import (
    NormalizationError,
    parse_tick,
)

logger = logging.getLogger("app.market_data.alice_blue_ws")

_RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10, 15, 30)
# Alice Blue's own doc: "Send heartbeat once in every 50 seconds."
_HEARTBEAT_INTERVAL_SECONDS = 50.0

# 2026-09-01: separate, longer-capped backoff for "we already know there's no
# live session" (`session_is_live` returns False) — see `ws_client.py`'s
# identical constant for the full incident writeup (a stale/expired Alice
# Blue session, the normal state until a human logs in, meant this loop
# retried a doomed handshake — *and* the `_ensure_ws_session` REST call ahead
# of it — every 30s indefinitely). This client is additionally the live
# failover backup for Shoonya (`MARKET_DATA_FAILOVER_ENABLED=true` on the
# deployed box) — confirmed via live logs, this exact loop is what produced
# ~190 failed attempts over one ~90 minute morning while Alice Blue's cached
# session was stale.
_SESSION_NOT_READY_BACKOFF_SECONDS = (5, 15, 30, 60, 120, 300)


@dataclass(frozen=True)
class _SubscriptionEntry:
    contract_symbol: str
    exchange: str
    token: str

    @property
    def key(self) -> str:
        return f"{self.exchange}|{self.token}"


def _double_sha256(value: str) -> str:
    """`susertoken` field per Alice Blue's own WebSocket doc:
    `sha256_encryption(sha256_encryption(session_id))` — hashed twice.
    """
    once = hashlib.sha256(value.encode()).hexdigest()
    return hashlib.sha256(once.encode()).hexdigest()


class AliceBlueWSClient:
    def __init__(
        self,
        ws_host: str,
        *,
        uid: str,
        actid: str,
        user_session: str,
        on_tick: TickCallback,
        source: str = "API",
        ensure_ws_session: Callable[[], None] | None = None,
        session_is_live: Callable[[], bool] | None = None,
    ) -> None:
        """`uid`/`actid` are taken exactly as given — see `alice_blue.py`'s
        own docstring for why the `f"{client_id}_API"` formatting lives at
        the call site, not here (this class stays protocol-only, same
        separation `ShoonyaWSClient` keeps from its own adapter layer).

        `ensure_ws_session`, when given, is called once before *every*
        connect attempt (first connect and every reconnect alike) — see
        `alice_blue_auth.create_ws_session`'s own docstring for why this
        call is required at all: live-confirmed 2026-08-21, every connect
        attempt without it first was rejected with `NOT_OK`.

        `session_is_live`, when given, is checked at the top of every `_run`
        loop iteration *before* `ensure_ws_session`/the connect attempt —
        `None` (the default) preserves prior behavior exactly. See
        `_SESSION_NOT_READY_BACKOFF_SECONDS`'s own comment.
        """
        self._ws_host = ws_host
        self._uid = uid
        self._actid = actid
        self._user_session = user_session
        self._on_tick = on_tick
        self._source = source
        self._ensure_ws_session = ensure_ws_session
        self._session_is_live = session_is_live

        self._entries_by_key: dict[str, _SubscriptionEntry] = {}
        self._last_known_by_key: dict[str, dict] = {}
        self._lock = threading.Lock()

        self._live_ws: Connection | None = None
        self._send_lock = threading.Lock()

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = threading.Event()

        # Health-monitoring only -- not read by any hot path. `reconnect_count`
        # increments once per *re*-connect (not the first connect), so a
        # multi-hour soak's own health endpoint can distinguish "connected
        # once and stayed up" from "flapping."
        self._ever_connected = False
        self.reconnect_count = 0
        self.last_tick_at: float | None = None
        self.tick_count = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._connected.wait(timeout=5.0)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def has_subscriptions(self) -> bool:
        with self._lock:
            return bool(self._entries_by_key)

    def subscribe(self, entries: list[tuple[str, str, str]]) -> None:
        """`entries` is `(contract_symbol, exchange, broker_token)` triples."""
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
                self._last_known_by_key.pop(entry.key, None)
        if to_remove:
            self._send_unsubscribe(to_remove)

    # -- connection lifecycle -------------------------------------------------

    def _run(self) -> None:
        backoff_index = 0
        not_ready_index = 0
        waiting_for_session = False
        while not self._stop.is_set():
            if self._session_is_live is not None and not self._session_looks_live():
                if not waiting_for_session:
                    logger.warning(
                        "Alice Blue WebSocket: no valid session yet — deferring connect "
                        "attempts until a fresh login completes"
                    )
                    waiting_for_session = True
                delay = _SESSION_NOT_READY_BACKOFF_SECONDS[
                    min(not_ready_index, len(_SESSION_NOT_READY_BACKOFF_SECONDS) - 1)
                ]
                not_ready_index += 1
                self._stop.wait(delay)
                continue
            if waiting_for_session:
                logger.warning("Alice Blue WebSocket: session now available — resuming connect")
                waiting_for_session = False
            not_ready_index = 0

            try:
                if self._ensure_ws_session is not None:
                    self._ensure_ws_session()
                with connect(self._ws_host, open_timeout=10) as ws:
                    self._authenticate(ws)
                    with self._send_lock:
                        self._live_ws = ws
                    self._resubscribe_all()
                    if self._ever_connected:
                        self.reconnect_count += 1
                    self._ever_connected = True
                    self._connected.set()
                    backoff_index = 0
                    self._receive_loop(ws)
            except Exception:
                logger.exception("Alice Blue WebSocket connection dropped; reconnecting")
            finally:
                with self._send_lock:
                    self._live_ws = None
            self._connected.clear()
            if self._stop.is_set():
                return
            delay = _RECONNECT_BACKOFF_SECONDS[
                min(backoff_index, len(_RECONNECT_BACKOFF_SECONDS) - 1)
            ]
            backoff_index += 1
            self._stop.wait(delay)

    def _session_looks_live(self) -> bool:
        assert self._session_is_live is not None
        try:
            return self._session_is_live()
        except Exception:
            logger.exception(
                "Alice Blue WebSocket: session_is_live check raised — treating as not live"
            )
            return False

    def _authenticate(self, ws: Connection) -> None:
        logger.warning(
            "Alice Blue WebSocket auth attempt: uid=%r actid=%r source=%r",
            self._uid,
            self._actid,
            self._source,
        )
        ws.send(
            json.dumps(
                {
                    "susertoken": _double_sha256(self._user_session),
                    "t": "c",
                    "actid": self._actid,
                    "uid": self._uid,
                    "source": self._source,
                }
            )
        )
        ack_raw = ws.recv(timeout=10)
        ack = json.loads(ack_raw)
        # Live-confirmed 2026-08-21: the real ack is the classic Noren
        # `{"t": "ck", "s": "OK"/"NOT_OK"}` shape, not the `{"t": "cf",
        # "k": "OK"}` this codebase's docs research had assumed (Alice
        # Blue's own WebSocket doc page's ack example turned out to not
        # match the real server response at all).
        if ack.get("t") != "ck" or ack.get("s") != "OK":
            raise ConnectionError(f"Alice Blue WebSocket auth rejected: {ack!r}")

    def _resubscribe_all(self) -> None:
        with self._lock:
            entries = list(self._entries_by_key.values())
        if entries:
            self._send_subscribe(entries)

    def _send_subscribe(self, entries: list[_SubscriptionEntry]) -> None:
        if not entries:
            return
        keys = "#".join(entry.key for entry in entries)
        self._send({"k": keys, "t": "t"})

    def _send_unsubscribe(self, entries: list[_SubscriptionEntry]) -> None:
        keys = "#".join(entry.key for entry in entries)
        # "u" (unsubscribe touchline) is inferred from Shoonya's identical
        # Noren-family convention, not directly confirmed in Alice Blue's
        # own doc excerpt (which only showed subscribe) -- flagged, not
        # silently assumed correct. If unsubscribe turns out to be a no-op
        # on a live account, this is the first thing to re-check.
        self._send({"k": keys, "t": "u"})

    def _send(self, message: dict) -> None:
        with self._send_lock:
            target = self._live_ws
            if target is None:
                return
            try:
                target.send(json.dumps(message))
            except Exception:
                logger.exception("Alice Blue WebSocket send failed for %r", message)

    def _receive_loop(self, ws: Connection) -> None:
        last_heartbeat = time.monotonic()
        while not self._stop.is_set():
            try:
                raw = ws.recv(timeout=1.0)
            except TimeoutError:
                if time.monotonic() - last_heartbeat >= _HEARTBEAT_INTERVAL_SECONDS:
                    self._send({"k": "", "t": "h"})
                    last_heartbeat = time.monotonic()
                continue

            self._handle_message(raw)

    def _handle_message(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            logger.warning("Alice Blue WebSocket sent non-JSON frame: %r", raw)
            return

        msg_type = message.get("t")
        key = f"{message.get('e', '')}|{message.get('tk', '')}"
        with self._lock:
            entry = self._entries_by_key.get(key)
            if entry is None:
                return
            merged = {**self._last_known_by_key.get(key, {}), **message}
            self._last_known_by_key[key] = merged

        if msg_type not in ("tk", "tf"):
            return
        try:
            tick = parse_tick(merged, entry.contract_symbol)
        except NormalizationError:
            logger.exception("Failed to normalize Alice Blue WebSocket message: %r", merged)
            return
        self.tick_count += 1
        self.last_tick_at = time.time()
        self._on_tick(tick)
