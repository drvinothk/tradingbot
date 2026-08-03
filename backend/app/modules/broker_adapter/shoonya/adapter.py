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

# Live-confirmed via a diagnostic log of real search_scrip("NSE", "NIFTY")
# results: Shoonya's NSE tsym for the index isn't the bare "NIFTY"/
# "BANKNIFTY" name used everywhere else in this codebase — it's a distinct
# display-style string. The same search for "NIFTY" also returned "Nifty
# Bank", "Nifty Next 50", "Nifty Fin Service", and a dozen unrelated
# NIFTYxxx-EQ ETF tickers, so a substring/startswith match would have been
# genuinely ambiguous — an explicit mapping, scoped to the same two known
# underlyings this whole adapter already limits itself to, is the only safe
# fix.
_UNDERLYING_INDEX_TSYM: dict[str, str] = {
    "NIFTY": "Nifty 50",
    "BANKNIFTY": "Nifty Bank",
}

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
        # Resolves the underlying's own (NSE) token — both as a side effect
        # (caches it for a later subscribe_quotes/get_quote("NIFTY") call)
        # and, live-corrected below, to fetch its current price for strprc.
        underlying_exchange, underlying_token = self._resolve_underlying_token(underlying)
        # Live-corrected (three wrong guesses first — "NIFTY", "Nifty 50",
        # and quote_plus-encoded "Nifty+50" were all rejected as "Invalid
        # Trading Symbol"): GetOptionChain's `tsym` isn't an index name in
        # any form — Shoonya's own docs define it as "Trading symbol of any
        # of the option or future. Option chain for that underlying will be
        # returned," e.g. a real contract like `NIFTY28AUG25F`. Any live,
        # currently-listed futures contract on this underlying works as the
        # anchor — the returned chain is filtered by `expiry` below
        # regardless of which contract's own expiry we anchored on.
        anchor_tsym = self._resolve_futures_anchor_tsym(underlying)
        # Live-corrected: `strprc=0.0` was rejected outright ("Invalid
        # strprc") — Shoonya's docs call it the "mid price" to center the
        # chain on (near ATM), not an optional/zero-default field. Uses the
        # underlying's own current LTP, the same "lp" field normalizer.
        # parse_tick already reads from an identical GetQuotes response
        # shape.
        underlying_quote = self._rest.get_quotes(self._uid, underlying_exchange, underlying_token)
        strike_price = float(underlying_quote.get("lp", 0.0))
        rows = self._rest.get_option_chain(
            self._uid, exchange, anchor_tsym, strike_price=strike_price
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

    def _resolve_underlying_token(self, underlying: str) -> tuple[str, str]:
        """**Live-corrected twice**: the underlying index itself (`NIFTY`/
        `BANKNIFTY`) only exists as a tradable scrip on `NSE` (the cash/
        index segment) — `NFO` exclusively lists derivative contracts *on*
        it (option/future symbols like `NIFTY28AUG25FUT`), never a bare
        `NIFTY` tsym; searching `NFO` for it (the original assumption)
        never finds a match. Round 2: even on `NSE`, the index's own tsym
        isn't the bare underlying name — it's Shoonya's own display-style
        string ("Nifty 50"/"Nifty Bank"), confirmed via a live diagnostic
        log (see `_UNDERLYING_INDEX_TSYM`'s own docstring for the full
        candidate list that made a fuzzy match too risky to use instead).
        """
        try:
            return self._resolve_token(underlying)
        except ShoonyaApiError:
            index_tsym = _UNDERLYING_INDEX_TSYM.get(underlying.upper(), underlying).upper()
            rows = self._rest.search_scrip(self._uid, "NSE", underlying)
            for row in rows:
                if str(row.get("tsym", "")).upper() == index_tsym:
                    token = str(row.get("token", ""))
                    self._remember_token(underlying, "NSE", token)
                    return "NSE", token
            raise

    def _resolve_futures_anchor_tsym(self, underlying: str) -> str:
        """Finds any live, currently-listed NFO futures contract on this
        underlying to use as `GetOptionChain`'s `tsym` anchor — see
        `get_option_chain`'s own docstring for why a real contract symbol
        is required at all. `instname` starting with `FUT` (`FUTIDX`/
        `FUTSTK`) is the same Noren convention `normalizer.
        parse_instrument_master_row` already uses to identify option rows
        (`OPTIDX`/`OPTSTK`); `symname` is the row's own underlying
        reference, more reliable than pattern-matching `tsym` strings.
        Picks the nearest expiry so this stays valid as far into the
        future as any currently-listed contract does.
        """
        rows = self._rest.search_scrip(self._uid, "NFO", underlying)
        futures = [
            row
            for row in rows
            if str(row.get("instname", "")).upper().startswith("FUT")
            and str(row.get("symname", "")).upper() == underlying.upper()
        ]
        if not futures:
            raise ShoonyaApiError(
                "resolve_futures_anchor",
                f"no NFO futures contract found for underlying {underlying!r}",
            )

        def _expiry_key(row: dict) -> date:
            try:
                return normalizer.parse_shoonya_date(str(row.get("exd", "")))
            except normalizer.NormalizationError:
                return date.max

        futures.sort(key=_expiry_key)
        return str(futures[0]["tsym"])

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
