"""`AliceBlueMarketDataProvider` — market-data-only `BaseMarketDataProvider`
implementation, added 2026-08-21 per explicit user decision. Shoonya stays
the execution broker, completely untouched by this module (see
`market_data/provider_composition.py`'s own docstring for the "why a
second port" reasoning this class is one more instance of).

**Structural boundary is enforced by construction, not convention**: this
class (like every `BaseMarketDataProvider`) has no order/account-mutating
method on its interface at all — `connect`/`disconnect`/`subscribe_ticks`/
`unsubscribe_ticks`/`get_latest_tick`/`get_price_history` only. There is no
code path anywhere in `alice_blue_*` that could place an order even if
asked to.

**Auth is fundamentally different from every other provider here**: Angel
One's `connect()` can log in itself (direct password+TOTP); Alice Blue's
can't — it's a browser-redirect OAuth flow that only completes via a human
clicking through `api.v1.alice_blue.oauth_callback` (see that module's own
docstring, and `AliceBlueSettings`' for why no TOTP/autologin alternative
exists per Alice Blue's own docs, confirmed 2026-08-21). `connect()` here
just reads whatever `alice_blue_session.get_alice_blue_session()` currently
holds and raises `BrokerAuthError` if nothing's there yet — it can never
trigger a fresh login on its own.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from app.config.settings import AliceBlueSettings
from app.modules.broker_adapter.base.contracts import PriceCandle, Tick
from app.modules.broker_adapter.base.errors import BrokerAuthError
from app.modules.market_data.providers.alice_blue_auth import create_ws_session
from app.modules.market_data.providers.alice_blue_scrip_master import AliceBlueScripMasterService
from app.modules.market_data.providers.alice_blue_session import get_alice_blue_session
from app.modules.market_data.providers.alice_blue_ws_client import AliceBlueWSClient
from app.modules.market_data.providers.base import (
    BaseMarketDataProvider,
    DepthCallback,
    TickCallback,
)

logger = logging.getLogger("app.market_data.alice_blue")


class AliceBlueMarketDataProvider(BaseMarketDataProvider):
    def __init__(
        self,
        settings: AliceBlueSettings,
        scrip_master: AliceBlueScripMasterService,
    ) -> None:
        self._settings = settings
        self._scrip_master = scrip_master

        self._ws: AliceBlueWSClient | None = None
        # Per-symbol, not a single shared slot -- same live-bug-driven
        # reasoning as AngelOneMarketDataProvider.subscribe_ticks' own
        # docstring (2026-08-13/17 incidents): a single shared callback
        # would let PositionManager subscribing an option contract silently
        # overwrite MarketDataIngestionService's own underlying callback.
        # setdefault (first-registrant-wins), matching that same fix.
        self._on_tick_by_symbol: dict[str, TickCallback] = {}

        self._lock = threading.Lock()
        self._token_by_symbol: dict[str, str] = {}
        self._symbol_by_token: dict[str, str] = {}
        self._latest_ticks: dict[str, Tick] = {}

    # -- BaseMarketDataProvider: connection lifecycle -----------------------

    def connect(self) -> None:
        """Idempotent per the interface's own contract, but unlike every
        other provider here, "connect" can't itself produce a session --
        see this module's own docstring. Raises `BrokerAuthError` (not,
        say, a bare `RuntimeError`) so callers already built to treat that
        as "broker isn't ready yet, retry later" (PositionManager's own
        `_handle_broker_auth_error`) handle this the same way they'd handle
        Shoonya's.
        """
        if self._ws is not None:
            return
        session = get_alice_blue_session()
        if session is None:
            raise BrokerAuthError(
                "No Alice Blue session — a human must complete the browser login via "
                "/aliceblue/login-url first (see api.v1.alice_blue)."
            )

    def disconnect(self) -> None:
        if self._ws is not None:
            self._ws.stop()
            self._ws = None

    def is_ready(self) -> bool:
        """`FailoverMarketDataProvider`'s pre-trip readiness gate (2026-08-25)
        -- this is the one real override of `BaseMarketDataProvider.is_ready`'s
        `True` default in this codebase, since Alice Blue is the one provider
        whose auth can't self-recover (see this module's own docstring: only
        a human browser login via `api.v1.alice_blue.oauth_callback` can ever
        produce a session). Same underlying check `connect()`/
        `subscribe_ticks()` already make, and the identical one
        `diagnostic_session._validate_can_run` uses for the "Test Failback"
        button's own precondition.
        """
        return get_alice_blue_session() is not None

    def close(self) -> None:
        self.disconnect()
        self._scrip_master.close()

    # -- BaseMarketDataProvider: ticks ---------------------------------------

    def subscribe_ticks(
        self,
        symbols: list[str],
        on_tick: TickCallback,
        on_depth: DepthCallback | None = None,
    ) -> None:
        """`on_depth` is accepted for interface compatibility but never
        invoked -- see `alice_blue_ws_client.py`'s own docstring for why
        depth isn't implemented yet (no confirmed message shape).
        """
        session = get_alice_blue_session()
        if session is None:
            raise BrokerAuthError(
                "No Alice Blue session — a human must complete the browser login via "
                "/aliceblue/login-url first (see api.v1.alice_blue)."
            )

        with self._lock:
            for symbol in symbols:
                self._on_tick_by_symbol.setdefault(symbol, on_tick)

        if self._ws is None:
            settings = self._settings
            # Live-confirmed 2026-08-21 (see alice_blue_ws_client.py's own
            # docstring for the full incident writeup): the WS connect
            # frame's uid/actid must carry this literal "_API" suffix, not
            # the bare client_id -- every attempt without it was rejected
            # even once createWsSess and the correct ack shape were fixed.
            self._ws = AliceBlueWSClient(
                settings.ws_host,
                uid=f"{session.client_id}_API",
                actid=f"{session.client_id}_API",
                user_session=session.user_session,
                on_tick=self._handle_raw_tick,
                ensure_ws_session=lambda: create_ws_session(settings, session),
            )
            self._ws.start()

        entries: list[tuple[str, str, str]] = []
        with self._lock:
            for symbol in symbols:
                token = self._scrip_master.get_token(symbol)
                if token is None:
                    logger.warning(
                        "No Alice Blue token mapped for %r; skipping subscribe "
                        "(scrip master hasn't matched it yet, or it isn't a "
                        "tracked underlying)",
                        symbol,
                    )
                    continue
                exchange = self._scrip_master.get_exchange_segment(symbol) or "NFO"
                self._token_by_symbol[symbol] = token
                self._symbol_by_token[token] = symbol
                entries.append((symbol, exchange, token))

        self._ws.subscribe(entries)

    def unsubscribe_ticks(self, symbols: list[str]) -> None:
        with self._lock:
            for symbol in symbols:
                self._on_tick_by_symbol.pop(symbol, None)
                token = self._token_by_symbol.pop(symbol, None)
                if token is not None:
                    self._symbol_by_token.pop(token, None)
                self._latest_ticks.pop(symbol, None)
        if self._ws is not None:
            self._ws.unsubscribe(symbols)

    def get_latest_tick(self, symbol: str) -> Tick | None:
        with self._lock:
            return self._latest_ticks.get(symbol)

    def _handle_raw_tick(self, tick: Tick) -> None:
        symbol = tick.contract_symbol
        with self._lock:
            self._latest_ticks[symbol] = tick
            callback = self._on_tick_by_symbol.get(symbol)
        if callback is not None:
            callback(tick)

    # -- BaseMarketDataProvider: history --------------------------------------

    def get_price_history(
        self, underlying: str, start: datetime, end: datetime, timeframe_seconds: int = 60
    ) -> list[PriceCandle]:
        """**Not yet implemented** — Alice Blue's own Historical Data REST
        endpoint hasn't been confirmed against a live account yet (unlike
        the WS tick path, which is this session's actual goal). Returns
        `[]` rather than guessing an endpoint shape, same "flag, don't
        fabricate" discipline as every other unconfirmed REST call in this
        codebase. `MarketDataIngestionService`'s WS-health-watchdog ->
        REST-polling-fallback feature (which calls this) degrades to "no
        candles" for Alice Blue specifically until this is built — it does
        not affect Angel One/Shoonya/TrueData, and it does not affect
        Alice Blue's own WS tick path either, since ticks don't route
        through this method at all.
        """
        logger.warning(
            "AliceBlueMarketDataProvider.get_price_history not yet implemented "
            "(no confirmed REST endpoint) — returning no candles for %r",
            underlying,
        )
        return []
