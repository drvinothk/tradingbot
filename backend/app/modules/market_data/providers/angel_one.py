"""`AngelOneMarketDataProvider` — the real `BaseMarketDataProvider`
implementation this system's live ticks come from. Shoonya stays purely an
execution broker (`BrokerPort`/`get_execution_broker()`, untouched by this
module) — see `market_data/provider_composition.py`'s own docstring for the
full "why a second port, not a bigger `BrokerPort`" reasoning.

Endpoint/payload details for the REST login are from the user-supplied
Angel One SmartAPI doc extraction (2026-08); the historical-candle REST call
is this module's own researched-not-live-verified addition (see
`angel_rest_client.get_candle_data`'s own docstring). Binary WS tick
unpacking is delegated to the official `smartapi-python` SDK, not
hand-rolled (see `angel_ws_client.py`'s own docstring for why).
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

import pyotp

from app.config.settings import AngelOneSettings
from app.core.clock import IST
from app.modules.broker_adapter.base.contracts import DepthLevel, DepthSnapshot, PriceCandle, Tick
from app.modules.broker_adapter.base.errors import BrokerAuthError
from app.modules.market_data.providers.angel_rest_client import AngelOneRestClient
from app.modules.market_data.providers.angel_ws_client import (
    EXCHANGE_TYPE_NSE_CM,
    EXCHANGE_TYPE_NSE_FO,
    AngelWSClient,
    RawAngelDepth,
    RawAngelTick,
)
from app.modules.market_data.providers.base import (
    BaseMarketDataProvider,
    DepthCallback,
    TickCallback,
)
from app.modules.market_data.scrip_master import ScripMasterService

logger = logging.getLogger("app.market_data.angel_one")

# SmartStream's own documented ceiling — this system's real usage (2-3
# underlyings, a handful of open-position option contracts) is nowhere near
# it; a defensive log-loud-don't-silently-truncate guard, not active
# chunking logic, since there's no realistic path to needing one yet.
_MAX_TOKENS_PER_SESSION = 1000


def _exchange_segment_to_type(segment: str) -> int:
    return EXCHANGE_TYPE_NSE_CM if segment.upper() == "NSE" else EXCHANGE_TYPE_NSE_FO


class AngelOneMarketDataProvider(BaseMarketDataProvider):
    def __init__(
        self,
        settings: AngelOneSettings,
        scrip_master: ScripMasterService,
        *,
        rest_client: AngelOneRestClient | None = None,
    ) -> None:
        self._settings = settings
        self._scrip_master = scrip_master
        self._rest = rest_client or AngelOneRestClient(
            settings.rest_host,
            api_key=settings.api_key,
            mac_address=settings.resolved_mac_address(),
            auth_proxy=settings.auth_proxy,
        )

        self._jwt_token: str | None = None
        self._feed_token: str | None = None

        self._ws: AngelWSClient | None = None
        # Per-symbol, not a single shared slot -- see this class's own
        # subscribe_ticks docstring for why (live bug, 2026-08-13).
        self._on_tick_by_symbol: dict[str, TickCallback] = {}
        self._on_depth_by_symbol: dict[str, DepthCallback] = {}

        self._lock = threading.Lock()
        self._token_by_symbol: dict[str, str] = {}
        self._symbol_by_token: dict[str, str] = {}
        self._latest_ticks: dict[str, Tick] = {}

    # -- BaseMarketDataProvider: connection lifecycle -----------------------

    def connect(self) -> None:
        if self._feed_token is not None:
            return  # already authenticated — idempotent per the interface's own contract
        totp = pyotp.TOTP(self._settings.totp_secret.get_secret_value()).now()
        data = self._rest.login_by_password(
            self._settings.client_code,
            self._settings.password.get_secret_value(),
            totp,
        )
        self._jwt_token = data["jwtToken"]
        self._feed_token = data["feedToken"]

    def disconnect(self) -> None:
        if self._ws is not None:
            self._ws.stop()
            self._ws = None
        self._jwt_token = None
        self._feed_token = None

    def _force_fresh_login(self) -> tuple[str, str]:
        """Bypasses `connect()`'s own idempotency check on purpose — that
        method's contract (per `BaseMarketDataProvider`'s own docstring) is
        "idempotent, must not re-authenticate if already connected," which
        is correct for every other caller. This is the one narrow, explicit
        exception: `AngelWSClient`'s reconnect loop, after enough
        consecutive failures that the currently-held tokens are worth
        discarding rather than trusting again (see that class's own
        `_TOKEN_REFRESH_AFTER_CONSECUTIVE_FAILURES`). `pyotp.TOTP(...).now()`
        inside `connect()` generates a fresh code at call time, so calling
        this later than the original login needs no extra handling for TOTP
        validity.
        """
        self._feed_token = None
        self.connect()
        assert self._jwt_token is not None
        assert self._feed_token is not None
        return self._jwt_token, self._feed_token

    def close(self) -> None:
        self.disconnect()
        self._rest.close()

    # -- BaseMarketDataProvider: ticks ---------------------------------------

    def subscribe_ticks(
        self,
        symbols: list[str],
        on_tick: TickCallback,
        on_depth: DepthCallback | None = None,
    ) -> None:
        """Per-symbol callback dispatch, not a single shared slot -- a
        single `self._on_tick_external`/`self._on_depth_external` attribute
        (overwritten on every call) used to mean `PositionManager`
        subscribing an option contract's price on this same shared provider
        would silently overwrite `MarketDataIngestionService`'s own
        underlying callback, breaking `quote_ticks`/`price_bars` persistence
        for everything, system-wide, the moment any position opened --
        live-confirmed 2026-08-13 (see broker_port_shim.py's identical fix,
        same root cause, for the full incident writeup).

        **2026-08-17**: per-symbol keying alone wasn't the full fix --
        `PositionManager` also subscribes the *underlying* itself (not just
        option contracts), colliding on the same symbol/caller pair
        ingestion already owns. Registration is now first-registrant-wins
        (`setdefault`) rather than unconditional assignment, matching
        `broker_port_shim.py`'s identical fix -- see that module's
        docstring for the full incident writeup.
        """
        if self._feed_token is None:
            self.connect()
        with self._lock:
            for symbol in symbols:
                self._on_tick_by_symbol.setdefault(symbol, on_tick)
                if on_depth is not None:
                    self._on_depth_by_symbol.setdefault(symbol, on_depth)

        if self._ws is None:
            self._ws = AngelWSClient(
                auth_token=self._jwt_token or "",
                api_key=self._settings.api_key,
                client_code=self._settings.client_code,
                feed_token=self._feed_token or "",
                on_tick=self._handle_raw_tick,
                on_depth=self._handle_raw_depth,
                token_refresh_callback=self._force_fresh_login,
                # 2026-08-11, live-confirmed: Angel's WS gateway rejects a
                # session whose streaming IP doesn't match the one its
                # token was issued from -- the WS connection must go
                # through the same proxy REST login uses, not connect
                # directly, whenever one is configured. See
                # AngelWSClient's own module docstring for the full finding.
                proxy_url=self._settings.auth_proxy,
            )
            self._ws.start()

        entries: list[tuple[str, int]] = []
        with self._lock:
            for symbol in symbols:
                token = self._scrip_master.get_angel_token(symbol)
                if token is None:
                    logger.warning(
                        "No Angel One token mapped for %r; skipping subscribe "
                        "(scrip master hasn't matched it yet, or it isn't a "
                        "tracked underlying)",
                        symbol,
                    )
                    continue
                segment = self._scrip_master.get_angel_exchange_segment(symbol) or "NFO"
                self._token_by_symbol[symbol] = token
                self._symbol_by_token[token] = symbol
                entries.append((token, _exchange_segment_to_type(segment)))

            total_subscribed = len(self._symbol_by_token)

        if total_subscribed > _MAX_TOKENS_PER_SESSION:
            logger.error(
                "Angel One subscription count (%d) exceeds SmartStream's "
                "documented %d-token ceiling — some symbols may not stream",
                total_subscribed,
                _MAX_TOKENS_PER_SESSION,
            )

        self._ws.subscribe(entries)

    def unsubscribe_ticks(self, symbols: list[str]) -> None:
        entries: list[tuple[str, int]] = []
        with self._lock:
            for symbol in symbols:
                self._on_tick_by_symbol.pop(symbol, None)
                self._on_depth_by_symbol.pop(symbol, None)
                token = self._token_by_symbol.pop(symbol, None)
                if token is None:
                    continue
                self._symbol_by_token.pop(token, None)
                self._latest_ticks.pop(symbol, None)
                segment = self._scrip_master.get_angel_exchange_segment(symbol) or "NFO"
                entries.append((token, _exchange_segment_to_type(segment)))
        if self._ws is not None:
            self._ws.unsubscribe(entries)

    def get_latest_tick(self, symbol: str) -> Tick | None:
        with self._lock:
            return self._latest_ticks.get(symbol)

    def _handle_raw_tick(self, raw_tick: RawAngelTick) -> None:
        with self._lock:
            symbol = self._symbol_by_token.get(raw_tick.token)
        if symbol is None:
            # Covers a token arriving before this process's own local cache
            # caught up (e.g. right after a restart) — falls back to the
            # scrip master's DB-backed reverse lookup rather than dropping
            # it outright.
            symbol = self._scrip_master.get_symbol_for_angel_token(raw_tick.token)
        if symbol is None:
            return

        tick = Tick(
            contract_symbol=symbol,
            ltp=raw_tick.ltp,
            bid=raw_tick.bid,
            ask=raw_tick.ask,
            volume=raw_tick.volume,
            oi=raw_tick.oi,
            ts=raw_tick.ts,
        )
        with self._lock:
            self._latest_ticks[symbol] = tick
            callback = self._on_tick_by_symbol.get(symbol)
        if callback is not None:
            callback(tick)

    def _handle_raw_depth(self, raw_depth: RawAngelDepth) -> None:
        with self._lock:
            symbol = self._symbol_by_token.get(raw_depth.token)
        if symbol is None:
            symbol = self._scrip_master.get_symbol_for_angel_token(raw_depth.token)
        if symbol is None:
            return
        with self._lock:
            callback = self._on_depth_by_symbol.get(symbol)
        if callback is None:
            return

        depth = DepthSnapshot(
            contract_symbol=symbol,
            bid_levels=tuple(
                DepthLevel(price=lvl.price, qty=lvl.qty, orders=lvl.orders)
                for lvl in raw_depth.bid_levels
            ),
            ask_levels=tuple(
                DepthLevel(price=lvl.price, qty=lvl.qty, orders=lvl.orders)
                for lvl in raw_depth.ask_levels
            ),
            ts=raw_depth.ts,
        )
        callback(depth)

    # -- BaseMarketDataProvider: history --------------------------------------

    def get_price_history(
        self, underlying: str, start: datetime, end: datetime, timeframe_seconds: int = 60
    ) -> list[PriceCandle]:
        if self._feed_token is None:
            self.connect()
        token = self._scrip_master.get_angel_token(underlying)
        if token is None:
            logger.warning(
                "No Angel One token mapped for underlying %r; returning no history", underlying
            )
            return []
        segment = self._scrip_master.get_angel_exchange_segment(underlying) or "NSE"

        try:
            # `start`/`end` arrive UTC-aware (every caller in this codebase
            # passes `datetime.now(UTC)`-derived values) — `strftime` alone
            # only formats the datetime's own wall-clock fields, it does not
            # convert, so without `.astimezone(IST)` first this silently sent
            # Angel's IST-expecting `fromdate`/`todate` the raw UTC clock
            # digits: a real, ~5.5-hour-shifted query window on every single
            # call, live since this method was first written. Caught
            # 2026-08-10 investigating why `price_bars` for NIFTY had had no
            # new row since 2026-08-05 despite the REST-poll fallback
            # reporting success (HTTP 200) — the shifted window was landing
            # on stretches with no genuine candle data to return, so
            # `_poll_once` saw a real, non-erroring, empty response and
            # silently persisted nothing, cycle after cycle.
            rows = self._rest.get_candle_data(
                self._jwt_token or "",
                segment,
                token,
                start.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
                end.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
                timeframe_seconds,
            )
        except BrokerAuthError:
            # The cached token is dead server-side (live-confirmed 2026-08-06:
            # a "soft" Invalid Token rejection, not an HTTP-level failure) --
            # clear it so connect()'s own idempotency check (`if self.
            # _feed_token is not None: return`) stops short-circuiting and
            # the *next* call attempts a genuine fresh login, instead of
            # retrying this same dead token indefinitely (what silently
            # burned the REST rate-limit budget overnight in the first
            # place). This call's own caller still sees the failure and
            # retries next cycle, same as any other error here.
            self._jwt_token = None
            self._feed_token = None
            raise
        candles: list[PriceCandle] = []
        for row in rows:
            try:
                bucket_start = datetime.fromisoformat(str(row[0])).astimezone(UTC)
                candles.append(
                    PriceCandle(
                        bucket_start=bucket_start,
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=int(row[5]) if len(row) > 5 else 0,
                    )
                )
            except (IndexError, ValueError, TypeError):
                logger.warning("Skipping unparseable Angel One candle row: %r", row)
                continue
        return candles
