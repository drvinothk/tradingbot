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

**2026-08-11: ROOT CAUSE FOUND, FIXED, LIVE-CONFIRMED.** Root cause
identified by Shoonya's own support team via a personalized reply
(addressed to this account's actual client ID) after months of `NOT_OK`
on every attempt: their recent OAuth migration changed the WS auth
payload shape. The connect message type is now `"t": "a"` (not `"t":
"c"`), the token field is `"accesstoken"` (not `susertoken`), and the
success ack is `{"t": "ak", "s": "OK"}` (not `{"t": "ck", "s": "OK"}`).
REST host/WS host are unchanged (`NorenWClientAPI`/`NorenWSAPI` — Shoonya
support's reply confirmed the same paths already configured here, not a
change).

**Live-confirmed the same day**, via `ShoonyaBrokerAdapter.
diagnose_ws_auth`'s one-shot connect+auth (re-added per the recipe this
file's own history had already anticipated for exactly this moment): real
account, real response —
`{"connected": true, "auth_ok": true, "ack": {"t": "ak", "s": "OK", "uid":
"FA44103"}}`. The WebSocket connection itself accepts the new auth frame
for the first time ever. **Not yet confirmed**: real tick streaming past
the auth handshake (`_receive_loop`, real `subscribe`/`tk`/`tf` messages)
— the diagnostic only exercises `_authenticate` in isolation, the same
scope `ShoonyaWSClient._authenticate` itself has. That's the next concrete
verification step, ideally during real market hours since a closed market
may not stream ticks regardless of auth succeeding.

**Prior investigation (now superseded, kept for context)**: every other
candidate cause under the *old* payload shape was ruled out first —
`susertoken` (matching the reference `NorenApi.py` implementation),
`uid`/`actid` both correct, REST and WS on the same whitelisted IP, both
`NorenWSAPI`/`NorenWSTP` hosts and three URL forms tried, `source`
variants (`API`/`WEB`/`MOB`) all identical — every attempt still got
`NOT_OK` at the identical point. That exhaustive process is what
established this was a genuine protocol-level mismatch rather than a
configuration mistake on this client's side, which is what prompted
escalating to Shoonya support directly rather than continuing to guess.

**2026-08-12: a second, real, client-side bug found and fixed — this is
why auth succeeding still produced zero ticks.** Live-tested during real
market hours via `diagnose_ws_ticks` against a real, correctly-tokened
NIFTY contract: `subscribe_quotes` raised nothing, but `ticks_received`
came back `0`. Root cause: `_send` only ever wrote to the socket when a
caller passed an explicit `ws=` — and only `_run`'s own background
thread, via `_resubscribe_all`/the heartbeat, ever did. `subscribe()`/
`unsubscribe()`, called from any other thread (a real strategy's
ingestion thread, or this diagnostic), went through the same `_send`
with no `ws`, which silently did nothing beyond updating
`_entries_by_key` — correct only on the theory that a *future*
reconnect's `_resubscribe_all` would pick it up. On an already-connected
client (the normal case: connect once via `subscribe_quotes`, then every
later call just extends the same subscription) there is no future
reconnect, so the subscribe request was dropped forever with no error
anywhere. Fixed by having `_run` publish the live `Connection` object to
`self._live_ws` (guarded by `_send_lock`) once connected, and having
`_send` always write to whatever `_live_ws` currently is, rather than
requiring the caller to hand one in — `_resubscribe_all`/heartbeat now
go through the same path instead of a separate explicit-`ws` one.
Between reconnects `_live_ws` is `None` and `_send` still silently
no-ops, same as before (recovered by `_resubscribe_all` next connect).
**Not yet re-verified live** — this fix hasn't been redeployed/retested
against a real account yet; that's the next concrete step once deployed.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from websockets.sync.client import connect
from websockets.sync.connection import Connection

from app.modules.broker_adapter.base.broker_port import DepthCallback, TickCallback
from app.modules.broker_adapter.shoonya.normalizer import (
    NormalizationError,
    parse_depth,
    parse_tick,
)

OrderUpdateCallback = Callable[[dict], None]

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
        on_order_update: OrderUpdateCallback | None = None,
        source: str = "API",
    ) -> None:
        self._ws_host = ws_host
        self._uid = uid
        self._actid = actid
        self._access_token = access_token
        self._on_tick = on_tick
        self._on_depth = on_depth
        self._on_order_update = on_order_update
        self._source = source

        self._entries_by_key: dict[str, _SubscriptionEntry] = {}
        # Noren's own touchline-feed protocol: "tk"/"dk" (sent once, right
        # after subscribing) is a *complete* snapshot; every "tf"/"df"
        # after that is a *partial* update carrying only the fields that
        # actually changed since the last push -- see `_handle_message`'s
        # own docstring for the live evidence and full reasoning. Keyed the
        # same as `_entries_by_key`.
        self._last_known_by_key: dict[str, dict] = {}
        self._lock = threading.Lock()

        # The live connection object, set only while `_run`'s `with connect(...)`
        # block holds one — see `_send`'s own docstring for why this exists:
        # a bare `_entries_by_key` update from `subscribe()` alone never
        # reaches the wire on an already-open connection.
        self._live_ws: Connection | None = None
        self._send_lock = threading.Lock()

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

    def has_subscriptions(self) -> bool:
        """Lets a caller that only owns *some* of this shared connection's
        subscriptions (e.g. a diagnostic call) check whether anything else
        is still relying on it before deciding to `stop()` the whole thing
        — this connection is shared per-process (see this module's own
        docstring), never one-per-caller, so tearing it down unconditionally
        after one caller's own `unsubscribe` would silently kill every other
        subscriber's stream too.
        """
        with self._lock:
            return bool(self._entries_by_key)

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

    def set_callbacks(
        self, on_tick: TickCallback, on_depth: DepthCallback | None = None
    ) -> None:
        """Re-point the tick/depth callbacks on an already-running client.

        `_handle_message`'s receive loop reads `self._on_tick`/`self._on_depth`
        live on every frame, so a plain assignment (done here under `_lock`
        for memory-visibility, though CPython attribute stores are atomic
        anyway) takes effect from the next frame on — no reconnect needed.

        Used by `ShoonyaBrokerAdapter.subscribe_quotes` when the process's
        shared `BrokerPortMarketDataAdapter` gets rebuilt (a Shoonya
        reconnect runs `market_data.registry.reset_for_reconnect` ->
        `set_market_data_provider(None)`), which hands this still-alive client
        a fresh — but behaviourally identical — dispatcher bound method.
        Last-writer-wins is correct there: the previous shim instance is
        being discarded, and its `get_latest_tick` cache going cold is
        already covered by `PositionManager._live_tick`'s `broker.get_quote`
        fallback.
        """
        with self._lock:
            self._on_tick = on_tick
            self._on_depth = on_depth

    def unsubscribe(self, contract_symbols: list[str]) -> None:
        with self._lock:
            to_remove = [
                entry
                for entry in self._entries_by_key.values()
                if entry.contract_symbol in contract_symbols
            ]
            for entry in to_remove:
                del self._entries_by_key[entry.key]
                # A later resubscribe (same or a different contract sharing
                # the exchange|token key, however unlikely) must never
                # inherit a stale merged snapshot from this subscription.
                self._last_known_by_key.pop(entry.key, None)
        if to_remove:
            self._send_unsubscribe(to_remove)

    # -- connection lifecycle -------------------------------------------------

    def _run(self) -> None:
        backoff_index = 0
        while not self._stop.is_set():
            try:
                with connect(self._ws_host, open_timeout=10) as ws:
                    self._authenticate(ws)
                    with self._send_lock:
                        self._live_ws = ws
                    self._resubscribe_all()
                    self._connected.set()
                    backoff_index = 0
                    self._receive_loop(ws)
            except Exception:
                logger.exception("Shoonya WebSocket connection dropped; reconnecting")
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

    def _authenticate(self, ws: Connection) -> None:
        # 2026-08-11: payload shape per Shoonya support's own reply (see
        # module docstring) — "t": "a" / "accesstoken", replacing the old
        # "t": "c" / "susertoken" pair that never once got past NOT_OK.
        # Deliberately logs uid/actid/source (account identifiers, not
        # secrets) but never accesstoken.
        # `.warning`, not `.info`: a real per-connection auth attempt is
        # worth always seeing, not routine chatter.
        logger.warning(
            "Shoonya WebSocket auth attempt: uid=%r actid=%r source=%r",
            self._uid,
            self._actid,
            self._source,
        )
        ws.send(
            json.dumps(
                {
                    "t": "a",
                    "uid": self._uid,
                    "actid": self._actid,
                    "accesstoken": self._access_token,
                    "source": self._source,
                }
            )
        )
        ack_raw = ws.recv(timeout=10)
        ack = json.loads(ack_raw)
        if ack.get("t") != "ak" or ack.get("s") != "OK":
            raise ConnectionError(f"Shoonya WebSocket auth rejected: {ack!r}")

    def _resubscribe_all(self) -> None:
        with self._lock:
            entries = list(self._entries_by_key.values())
        if entries:
            self._send_subscribe(entries)

    def _send_subscribe(self, entries: list[_SubscriptionEntry]) -> None:
        if not entries:
            return
        keys = "#".join(entry.key for entry in entries)
        self._send({"t": "t", "k": keys})
        if self._on_depth is not None:
            self._send({"t": "d", "k": keys})

    def _send_unsubscribe(self, entries: list[_SubscriptionEntry]) -> None:
        keys = "#".join(entry.key for entry in entries)
        self._send({"t": "u", "k": keys})

    def _send(self, message: dict) -> None:
        """2026-08-12: **real bug fixed here** — the old signature only ever
        wrote to the socket when a caller passed an explicit `ws=` (only
        `_run`'s own thread, via `_resubscribe_all`/heartbeat, ever did).
        `subscribe()`/`unsubscribe()`, called from any *other* thread (a
        real strategy's ingestion thread, this module's own diagnostic),
        went through this same method with no `ws` — silently doing
        nothing beyond updating `_entries_by_key`, on the theory that a
        *future* reconnect's `_resubscribe_all` would send it. On an
        already-connected client (the common case — `subscribe_quotes`
        connects once, then every later call just adds to the existing
        subscription) there is no future reconnect, so the request was
        dropped forever with no error. Live-confirmed 2026-08-12 via
        `diagnose_ws_ticks` against a real, correctly-tokened NIFTY
        contract during market hours: `ticks_received: 0` even though
        auth succeeded and `subscribe_quotes` raised nothing. Fixed by
        always sending on whichever connection `_run` currently holds
        (`self._live_ws`, set/cleared only by `_run` itself) — still
        silently no-ops between reconnects (recovered by
        `_resubscribe_all` on the next successful connect, unchanged),
        but no longer no-ops on an already-open connection just because
        the caller isn't the background thread.
        """
        with self._send_lock:
            target = self._live_ws
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
                    self._send({"t": "h"})
                    last_heartbeat = time.monotonic()
                continue

            self._handle_message(raw)

    def _handle_message(self, raw: str | bytes) -> None:
        """2026-08-12: **real bug fixed here.** Live-observed on a real
        account during market hours: `"tf"` (touchline feed) messages
        arrive as genuine partial updates, e.g.
        `{"t": "tf", "e": "NSE", "tk": "26000", "ft": "...", "toi": "..."}`
        — no `lp` at all, because only open interest changed since the
        last push. `parse_tick` requires `lp` unconditionally (correctly,
        for a REST `GetQuotes` response, which is always a complete
        snapshot), so every such partial update was raising
        `NormalizationError` and being dropped — silently losing real
        ticks (and spamming the log) any time an update didn't happen to
        touch price. Noren's own protocol convention is that `"tk"`/`"dk"`
        (sent once, right after subscribing) is the complete snapshot and
        every following `"tf"`/`"df"` carries only the fields that
        actually changed — so each message is now merged onto
        `_last_known_by_key`'s running snapshot for that token before
        parsing, giving `parse_tick`/`parse_depth` a complete picture even
        from a partial wire message. A token with no snapshot at all yet
        (partial update arrives before any full one — possible on a race
        at subscribe time) still correctly fails to parse: there's
        genuinely nothing to report yet.
        """
        try:
            message = json.loads(raw)
        except ValueError:
            logger.warning("Shoonya WebSocket sent non-JSON frame: %r", raw)
            return

        msg_type = message.get("t")

        # 2026-08-20: order-update push, added alongside the existing tick/
        # depth handling above. **Message type confirmed from primary
        # source the same night** -- the official `NorenRestApiPy` package
        # (PyPI, the actual library every Noren-broker wrapper, Shoonya's
        # `ShoonyaApi-py` included, depends on) has its own WS dispatch
        # doing exactly `if res['t'] == 'om': self.__order_update_callback
        # (res)`, passing the raw message straight through with no
        # reshaping -- matches this handler's own shape and supports
        # reusing `parse_order_result`'s field names (`norenordno`/
        # `status`/`fillshares`/`avgprc`) on the theory an `om` row shares
        # the same OMS data model as `OrderBook`/`SingleOrdHist`. Order
        # updates arrive automatically on this same connection once
        # authenticated, no separate subscribe call needed, confirmed the
        # same way. **Still not live-verified against a real account** --
        # the message *type* is now primary-source-confirmed, not just
        # inferred, but the exact field-for-field shape of a live `om` row
        # hasn't been directly observed yet; that's what the WARNING logs
        # just below and in `adapter.py`'s own callback exist to capture
        # the next time a real order resolves asynchronously. Deliberately
        # does not touch `_entries_by_key`/the tick-subscription-keyed
        # lookup below: an order-update message is an account-level event,
        # not tied to any subscribed instrument token, so it must be
        # handled before that lookup would otherwise silently drop it.
        # This is a best-effort fast path only --
        # `execution_engine.paper.service.reconcile_pending_live_orders`'s
        # own unconditional REST poll is the actual safety net if this
        # never fires or the field shape turns out to be wrong.
        if msg_type == "om":
            # .warning, not .info -- logged on *every* raw "om" frame
            # received, separate from adapter.py's own "successfully cached"
            # log --
            # this line proves the message *type* assumption is correct
            # even if parsing later fails, the two signals this whole
            # unconfirmed mechanism needs to be diagnosable tomorrow.
            logger.warning("Shoonya WS order-update frame received: %r", message)
            if self._on_order_update is not None:
                try:
                    self._on_order_update(message)
                except Exception:
                    logger.exception("Shoonya WS order-update callback failed for %r", message)
            return

        key = f"{message.get('e', '')}|{message.get('tk', '')}"
        with self._lock:
            entry = self._entries_by_key.get(key)
            if entry is None:
                return
            merged = {**self._last_known_by_key.get(key, {}), **message}
            self._last_known_by_key[key] = merged

        try:
            if msg_type in ("tk", "tf"):
                self._on_tick(parse_tick(merged, entry.contract_symbol))
            elif msg_type in ("dk", "df") and self._on_depth is not None:
                self._on_depth(parse_depth(merged, entry.contract_symbol))
        except NormalizationError:
            logger.exception("Failed to normalize Shoonya WebSocket message: %r", merged)
