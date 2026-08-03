"""`ShoonyaBrokerAdapter` — the real `BrokerPort` implementation Phase 5
slots in behind `broker_adapter.composition.get_broker()` with zero changes
required upstream (every module already only ever talks to `BrokerPort`).

**Construction is two-step, unlike `MockBrokerAdapter`.** Shoonya's OAuth
login (`auth.py`) needs a real browser + a human completing a login on
Shoonya's own site — nothing a backend method can do synchronously. So
`ShoonyaBrokerAdapter` is never constructed until `api.v1.shoonya.
oauth_callback` has already completed `auth.exchange_code_for_token` and
has a real `OAuthSession` in hand; `authenticate()` on the finished
instance just returns the `AuthResult` it was built with; it does not
attempt to (and cannot) run the OAuth flow itself.

**Only ever syncs NIFTY/BANKNIFTY** — deliberately, not a literal "every
tradable instrument on the exchange." `get_instrument_master` is
implemented via `SearchScrip` against this system's own known-underlyings
list (matching `mock_universe.py`'s hardcoded `NIFTY`/`BANKNIFTY` scope),
not a bulk scrip-master file download — this system never trades anything
else, so syncing thousands of irrelevant stock F&O contracts would be pure
waste, not fidelity to the interface's literal wording.

**Error-to-mode-transition mapping is not fully wired yet.** Phase 5's spec
calls for invalid-credentials/IP-mismatch/TOTP-drift/mid-session-expiry/
WS-drop scenarios to reach specific mode transitions and alerts. This
adapter raises a small, specific exception taxonomy for each
(`ShoonyaAuthError`, `ShoonyaSessionExpiredError`, `ShoonyaApiError`) so a
caller *can* catch and map them — deliberately not calling
`transition_mode`/`enter_kill_switch` directly here, since this module has
no DB session/TradingSession context (same boundary discipline
`core/clock.py`'s health checks already follow: "pure check functions —
they report, they don't act"). Wiring these into the Scheduler's existing
health-check loop is the next concrete step, not yet done — see the build
plan's Phase 5 section.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, date, datetime

from app.config.settings import ShoonyaSettings
from app.modules.broker_adapter.base.broker_port import BrokerPort, DepthCallback, TickCallback
from app.modules.broker_adapter.base.contracts import (
    AuthResult,
    DepthSnapshot,
    InstrumentInfo,
    MarginInfo,
    OptionChainSnapshot,
    OrderRequest,
    OrderResult,
    Position,
    Tick,
)
from app.modules.broker_adapter.shoonya import normalizer
from app.modules.broker_adapter.shoonya.rest_client import (
    ShoonyaApiError,
    ShoonyaRestClient,
    ShoonyaSessionExpiredError,
)
from app.modules.broker_adapter.shoonya.ws_client import ShoonyaWSClient

logger = logging.getLogger("app.broker_adapter.shoonya")

# This system only ever trades these two underlyings (see mock_universe.py) —
# get_instrument_master narrows to them rather than pulling every tradable
# NFO contract, per this module's own docstring.
KNOWN_UNDERLYINGS: tuple[str, ...] = ("NIFTY", "BANKNIFTY")

# Re-exported for callers that only import from adapter.py — the actual
# class lives in rest_client.py now (session-expiry classification happens
# right where the raw Not_Ok response is parsed, not one layer up).
__all__ = ["ShoonyaBrokerAdapter", "ShoonyaSessionExpiredError"]


class ShoonyaBrokerAdapter(BrokerPort):
    def __init__(
        self,
        settings: ShoonyaSettings,
        auth_result: AuthResult,
        *,
        rest_client: ShoonyaRestClient | None = None,
    ) -> None:
        self._settings = settings
        self._auth_result = auth_result
        self._uid = settings.user_id
        self._actid = auth_result.account_id

        self._rest = rest_client or ShoonyaRestClient(
            settings.api_host, auth_result.session_token
        )

        # contract_symbol -> (exchange, broker_token) — populated as
        # get_instrument_master/get_option_chain results come in, since
        # order placement and WS subscription both need the broker token,
        # not just the tradable symbol string.
        self._token_by_symbol: dict[str, tuple[str, str]] = {}
        self._token_lock = threading.Lock()

        self._ws: ShoonyaWSClient | None = None
        self._ws_on_tick: TickCallback | None = None
        self._ws_on_depth: DepthCallback | None = None

        # idempotency_key -> OrderResult, per BrokerPort.place_order's own
        # contract — a repeated call for the same key must return the
        # original result, not resubmit. Shoonya itself has no concept of
        # our idempotency_key beyond it riding along in `remarks`
        # (normalizer.to_place_order_payload), so this guarantee is ours
        # to keep, same as MockBrokerAdapter's identical dict.
        self._orders_by_idempotency_key: dict[str, OrderResult] = {}

    def close(self) -> None:
        if self._ws is not None:
            self._ws.stop()
        self._rest.close()

    def _remember_token(self, symbol: str, exchange: str, token: str) -> None:
        if token:
            with self._token_lock:
                self._token_by_symbol[symbol] = (exchange, token)

    def _resolve_token(self, contract_symbol: str) -> tuple[str, str]:
        with self._token_lock:
            resolved = self._token_by_symbol.get(contract_symbol)
        if resolved is None:
            raise ShoonyaApiError(
                "resolve_token",
                f"no cached broker token for {contract_symbol!r} — call "
                "get_instrument_master/get_option_chain for it first",
            )
        return resolved

    # -- BrokerPort: session --------------------------------------------------

    def authenticate(self) -> AuthResult:
        return self._auth_result

    # -- BrokerPort: instrument / chain data ----------------------------------

    def get_instrument_master(self, exchange: str) -> list[InstrumentInfo]:
        infos: list[InstrumentInfo] = []
        for underlying in KNOWN_UNDERLYINGS:
            rows = self._rest.search_scrip(self._uid, exchange, underlying)
            for row in rows:
                info = normalizer.parse_instrument_master_row(row, exchange)
                self._remember_token(info.symbol, exchange, info.broker_token)
                infos.append(info)
        return infos

    def get_option_chain(self, underlying: str, expiry: date) -> OptionChainSnapshot:
        exchange = "NFO"
        underlying_exchange, underlying_token = self._resolve_underlying_token(underlying, exchange)
        rows = self._rest.get_option_chain(
            self._uid, underlying_exchange, underlying, strike_price=0.0
        )

        entries = []
        for row in rows:
            row_expiry = row.get("exd")
            if row_expiry and normalizer.parse_shoonya_date(str(row_expiry)) != expiry:
                continue
            symbol = str(row.get("tsym", ""))
            token = str(row.get("token", ""))
            self._remember_token(symbol, exchange, token)
            entries.append(normalizer.parse_option_chain_entry(row, symbol))

        return OptionChainSnapshot(
            underlying=underlying,
            expiry=expiry,
            ts=_utcnow(),
            entries=tuple(entries),
        )

    def _resolve_underlying_token(self, underlying: str, exchange: str) -> tuple[str, str]:
        try:
            return self._resolve_token(underlying)
        except ShoonyaApiError:
            rows = self._rest.search_scrip(self._uid, exchange, underlying)
            for row in rows:
                if str(row.get("tsym", "")).upper() == underlying.upper():
                    token = str(row.get("token", ""))
                    self._remember_token(underlying, exchange, token)
                    return exchange, token
            raise

    # -- BrokerPort: quotes / depth -------------------------------------------

    def get_quote(self, contract_symbol: str) -> Tick:
        exchange, token = self._resolve_token(contract_symbol)
        raw = self._rest.get_quotes(self._uid, exchange, token)
        return normalizer.parse_tick(raw, contract_symbol)

    def get_depth(self, contract_symbol: str) -> DepthSnapshot:
        exchange, token = self._resolve_token(contract_symbol)
        raw = self._rest.get_quotes(self._uid, exchange, token)
        return normalizer.parse_depth(raw, contract_symbol)

    def subscribe_quotes(
        self,
        contract_symbols: list[str],
        on_tick: TickCallback,
        on_depth: DepthCallback | None = None,
    ) -> None:
        """One shared `ShoonyaWSClient` for the process, extended with each
        call — same "one shared connection, not one per instrument" shape
        `market_data/registry.py` already established for
        `MarketDataIngestionService`, and the same single-callback-slot
        contract `MockBrokerAdapter` implements (Phase 4's QC found that
        constructing a *second* streaming thing per instrument silently
        drops every other one's ticks; this codebase now only ever builds
        one, shared, everywhere).
        """
        self._ws_on_tick = on_tick
        self._ws_on_depth = on_depth
        if self._ws is None:
            self._ws = ShoonyaWSClient(
                self._settings.ws_host,
                uid=self._uid,
                actid=self._actid,
                access_token=self._auth_result.session_token,
                on_tick=on_tick,
                on_depth=on_depth,
            )
            self._ws.start()

        entries = [(symbol, *self._resolve_token(symbol)) for symbol in contract_symbols]
        self._ws.subscribe(entries)

    def unsubscribe_quotes(self, contract_symbols: list[str]) -> None:
        if self._ws is not None:
            self._ws.unsubscribe(contract_symbols)

    # -- BrokerPort: orders ----------------------------------------------------

    def place_order(self, request: OrderRequest) -> OrderResult:
        if request.idempotency_key in self._orders_by_idempotency_key:
            return self._orders_by_idempotency_key[request.idempotency_key]

        payload = normalizer.to_place_order_payload(request, uid=self._uid, actid=self._actid)
        raw = self._rest.place_order(payload)
        result = normalizer.parse_order_result(raw, idempotency_key=request.idempotency_key)

        # PlaceOrder's own immediate response only ever carries an Ok/Not_Ok
        # ack plus the new order id, not a real fill status — one
        # follow-up get_order_status call gets the actual post-fill state
        # rather than reporting every order as perpetually PENDING.
        if result.status.value == "pending":
            try:
                result = self.get_order_status(result.broker_order_id)
            except ShoonyaApiError:
                logger.exception(
                    "Failed to fetch order status right after PlaceOrder for %s",
                    result.broker_order_id,
                )

        self._orders_by_idempotency_key[request.idempotency_key] = result
        return result

    def modify_order(self, broker_order_id: str, **changes: object) -> OrderResult:
        payload = {
            "uid": self._uid,
            "norenordno": broker_order_id,
            **{k: str(v) for k, v in changes.items()},
        }
        raw = self._rest.modify_order(payload)
        return normalizer.parse_order_result(raw, idempotency_key=broker_order_id)

    def cancel_order(self, broker_order_id: str) -> OrderResult:
        raw = self._rest.cancel_order(self._uid, broker_order_id)
        return normalizer.parse_order_result(raw, idempotency_key=broker_order_id)

    def get_order_status(self, broker_order_id: str) -> OrderResult:
        rows = self._rest.single_order_history(self._uid, broker_order_id)
        if not rows:
            raise ShoonyaApiError(
                "SingleOrdHist", f"no history for order {broker_order_id!r}"
            )
        # Noren returns history oldest-first; the latest row is the order's
        # current state.
        return normalizer.parse_order_result(rows[-1], idempotency_key=broker_order_id)

    def get_positions(self) -> list[Position]:
        rows = self._rest.position_book(self._uid, self._actid)
        return [normalizer.parse_position(row) for row in rows]

    def get_margin(self) -> MarginInfo:
        raw = self._rest.get_limits(self._uid, self._actid)
        return normalizer.parse_margin(raw)


def _utcnow() -> datetime:
    return datetime.now(UTC)
