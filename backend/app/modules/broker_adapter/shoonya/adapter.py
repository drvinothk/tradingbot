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
tradable instrument on the exchange" (matching `mock_universe.py`'s own
hardcoded `NIFTY`/`BANKNIFTY` scope). `get_instrument_master`'s NFO path
(2026-08-12) now reads Shoonya's own bulk scrip-master file first,
filtered down to just those two underlyings' index options at parse time
(see `shoonya.scrip_master`'s own docstring) — this system never trades
anything else, so keeping thousands of irrelevant stock F&O rows around
would be pure waste, not fidelity to the interface's literal wording.
`SearchScrip` (this method's *only* NFO source before today) is now a
fallback, kept for when the static file is unreachable.

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

import json
import logging
import threading
import time
from datetime import UTC, date, datetime

import httpx
from websockets.sync.client import connect as _ws_connect

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
    PriceCandle,
    Tick,
)
from app.modules.broker_adapter.base.errors import CriticalSafetyException
from app.modules.broker_adapter.shoonya import normalizer
from app.modules.broker_adapter.shoonya import scrip_master as shoonya_scrip_master
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

        # (underlying, requested expiry) -> option anchor tsym. Unlike the
        # old futures-anchor cache this replaced, no time-based staleness
        # check is needed: the key already pins an exact calendar date, so
        # a resolved tsym for that exact key never needs re-resolving
        # (contrast a "nearest expiry" cache, which has to reroll as time
        # passes). A different requested expiry is simply a different key.
        self._option_anchor_cache: dict[tuple[str, date], str] = {}

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
        """2026-08-12: for `exchange == "NFO"`, tries Shoonya's own official
        static scrip master file first (`shoonya.scrip_master`, a real,
        public, no-auth, daily-updated download) — live evidence this same
        session showed `SearchScrip` returning different, non-overlapping
        expiry subsets across separate calls for the same underlying, and
        an empty `broker_token` for every recently-synced option row. The
        static file has neither failure mode (confirmed by downloading and
        inspecting it directly). `SearchScrip` is demoted to a fallback —
        used only if the static file's download/parse fails or returns
        nothing — never removed, so an unreachable file degrades to
        exactly today's existing (imperfect but working) behavior, never
        worse. Every other exchange keeps the original `SearchScrip`-only
        path unchanged.
        """
        if exchange == "NFO":
            zip_bytes = None
            with httpx.Client(timeout=30.0) as client:
                zip_bytes = shoonya_scrip_master.download_nfo_scrip_master(client)
            if zip_bytes is not None:
                infos = shoonya_scrip_master.parse_nfo_scrip_master(zip_bytes)
                if infos:
                    for info in infos:
                        self._remember_token(info.symbol, exchange, info.broker_token)
                    return infos
                logger.warning(
                    "Shoonya scrip master file parsed to zero NIFTY/BANKNIFTY rows — "
                    "falling back to SearchScrip"
                )
            else:
                logger.warning("Shoonya scrip master download failed — falling back to SearchScrip")
        return self._get_instrument_master_via_search_scrip(exchange)

    def _get_instrument_master_via_search_scrip(self, exchange: str) -> list[InstrumentInfo]:
        infos: list[InstrumentInfo] = []
        for underlying in KNOWN_UNDERLYINGS:
            rows = self._rest.search_scrip(self._uid, exchange, underlying)
            for row in rows:
                # This system only ever trades index options, never futures
                # (the old futures-anchor-based GetOptionChain approach was
                # already replaced by an options-only anchor earlier today —
                # nothing reads a futures InstrumentInfo anymore). Searching
                # NFO for "NIFTY"/"BANKNIFTY" text live-confirmed to also
                # match futures contracts (`NIFTY25AUG26F`) and unrelated
                # decoys sharing the same substring (`NIFTYNXT5025AUG26F`,
                # an ETF) — treating every non-option row as a tradable
                # underlying (the pre-existing behavior below) synced those
                # in as spurious, permanently-orphaned `Instrument` rows
                # that pollute the frontend's instrument picker and can
                # never be selected usefully. Skipping FUT* rows here keeps
                # this method's real job (populating option data for the
                # two known underlyings) unaffected — real option rows
                # already attach to the pre-existing NIFTY/BANKNIFTY
                # `Instrument` rows via `sync_instrument_master`'s own
                # DB-lookup fallback, never via a row from this loop.
                if str(row.get("instname", "")).upper().startswith("FUT"):
                    continue
                # Live-found: a real NFO SearchScrip response for NIFTY
                # included at least one option row with no `strprc` field at
                # all (`NIFTY04AUG26C18500`, `weekly: "W1"`) — never
                # surfaced before because this method had only ever run
                # against the mock adapter's clean synthetic data in
                # production. One malformed row used to abort parsing every
                # row after it in the whole underlying's list (a `for` loop
                # building one flat `infos` list, no per-row isolation) —
                # skipping just the bad row is what actually lets a real
                # sync complete instead of silently syncing nothing.
                try:
                    info = normalizer.parse_instrument_master_row(row, exchange)
                except normalizer.NormalizationError:
                    logger.warning(
                        "Skipping unparseable instrument master row for %r: %r",
                        underlying,
                        row,
                    )
                    continue
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
        # returned," e.g. a real contract like `NIFTY28AUG25F`. **Live-corrected
        # again**: anchoring via the nearest-expiry *futures* contract (the
        # original fix) always returns the *monthly* chain regardless of what
        # `expiry` was requested — NFO futures are monthly-only, and
        # `GetOptionChain`'s response follows the anchor's own series, not
        # some separately-specified expiry (there is no separate expiry
        # parameter to send). Live-confirmed via diagnostic logging: both
        # NIFTY and BANKNIFTY chains anchored on their nearest futures
        # contract came back on the identical monthly date, even though NIFTY
        # still lists weekly options. The actual fix is anchoring on a real
        # *option* contract that already matches the requested expiry exactly
        # (see `_resolve_option_anchor_tsym`) — including refusing outright
        # (never silently substituting the monthly series) when no such
        # contract exists, e.g. a BANKNIFTY weekly, which NSE discontinued.
        anchor_tsym = self._resolve_option_anchor_tsym(underlying, expiry)
        logger.info(
            "GetOptionChain anchor for %s expiry %s: tsym=%s", underlying, expiry, anchor_tsym
        )
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

        # Live-confirmed via diagnostic logging: a GetOptionChain row is
        # purely structural (token/tsym/strprc/optt/...) — it never carries
        # `exd`, and never carries live quote fields (lp/bp1/sp1/v/oi)
        # either, unlike GetQuotes/WS touchline pushes that share those exact
        # field names. Since the anchor above is now already pinned to the
        # exact requested expiry, there's nothing left to filter rows by —
        # every row this call returns is already that expiry's chain. Real
        # pricing needs one GetQuotes call per contract token; a failure on
        # any single contract degrades that one entry to zero rather than
        # discarding the whole snapshot over one bad call.
        entries = []
        for row in rows:
            symbol = str(row.get("tsym", ""))
            token = str(row.get("token", ""))
            self._remember_token(symbol, exchange, token)
            quote: dict = {}
            if token:
                try:
                    quote = self._rest.get_quotes(self._uid, exchange, token)
                except ShoonyaApiError:
                    logger.warning(
                        "Failed to fetch live quote for %s (token=%s); using zeros",
                        symbol,
                        token,
                    )
            entries.append(normalizer.parse_option_chain_entry(row, symbol, quote))

        logger.info(
            "GetOptionChain %s expiry %s: %d entries, sample=%r",
            underlying,
            expiry,
            len(entries),
            entries[:3],
        )
        return OptionChainSnapshot(
            underlying=underlying,
            expiry=expiry,
            ts=_utcnow(),
            entries=tuple(entries),
        )

    def _resolve_underlying_token(self, underlying: str) -> tuple[str, str]:
        """**Live-corrected three times**: the underlying index itself
        (`NIFTY`/`BANKNIFTY`) only exists as a tradable scrip on `NSE` (the
        cash/index segment) — `NFO` exclusively lists derivative contracts
        *on* it (option/future symbols like `NIFTY28AUG25FUT`), never a bare
        `NIFTY` tsym; searching `NFO` for it (the original assumption)
        never finds a match. Round 2: even on `NSE`, the index's own tsym
        isn't the bare underlying name — it's Shoonya's own display-style
        string ("Nifty 50"/"Nifty Bank"), confirmed via a live diagnostic
        log (see `_UNDERLYING_INDEX_TSYM`'s own docstring for the full
        candidate list). Round 3: searching NSE with `search_text=
        "BANKNIFTY"` returns only an unrelated ETF ticker ("BANKNIFTY1-EQ")
        — "Nifty Bank" never appears, confirmed via the same diagnostic
        logging — Shoonya's fuzzy search apparently needs *some* textual
        overlap with the actual tsym, and "BANKNIFTY" doesn't share enough
        with "Nifty Bank". Both known display-style tsyms happen to share
        the "Nifty" prefix, so searching with the fixed anchor text
        `"NIFTY"` (never the underlying's own name) reliably surfaces both.
        """
        try:
            return self._resolve_token(underlying)
        except ShoonyaApiError:
            index_tsym = _UNDERLYING_INDEX_TSYM.get(underlying.upper(), underlying).upper()
            rows = self._rest.search_scrip(self._uid, "NSE", "NIFTY")
            for row in rows:
                if str(row.get("tsym", "")).upper() == index_tsym:
                    token = str(row.get("token", ""))
                    self._remember_token(underlying, "NSE", token)
                    return "NSE", token
            logger.warning(
                "No exact NSE tsym match for underlying %r (expected %r) among "
                "%d search_scrip results: %s",
                underlying,
                index_tsym,
                len(rows),
                [row.get("tsym") for row in rows][:20],
            )
            raise

    def get_price_history(
        self, underlying: str, start: datetime, end: datetime, timeframe_seconds: int = 60
    ) -> list[PriceCandle]:
        """`TPSeries` — live-confirmed against a real account (diagnostic
        logging, since removed) to return real OHLC for NSE *index* tokens
        (NIFTY/BANKNIFTY spot), contrary to a documented, unresolved
        community report that index historical queries return no data on
        Shoonya. `intv` (volume) is genuinely `0` on every index candle
        though — confirmed by comparing against the same call against the
        underlying's own front-month futures token, which reports real
        volume in the same window. That's a real broker-side data gap for
        the index feed, not a parsing bug — `parse_tpseries_row` reports it
        as `0`, not a fabricated or estimated value.

        `interval_minutes` only supports whole-minute granularity per
        Shoonya's own docs ("1","3","5","10","15","30","60",...) — divides
        `timeframe_seconds` by 60 rather than exposing seconds to the wire
        call, since this system's only bar timeframe today is 60s anyway
        (`IndicatorEngine`'s default).
        """
        underlying_exchange, underlying_token = self._resolve_underlying_token(underlying)
        interval_minutes = max(1, timeframe_seconds // 60)
        rows = self._rest.get_time_price_series(
            self._uid,
            underlying_exchange,
            underlying_token,
            int(start.timestamp()),
            int(end.timestamp()),
            interval_minutes=interval_minutes,
        )
        return [normalizer.parse_tpseries_row(row) for row in rows]

    def _resolve_option_anchor_tsym(self, underlying: str, expiry: date) -> str:
        """`GetOptionChain` needs a real, currently-listed contract symbol
        as its `tsym` anchor (see that method's own docstring for the "any
        form of the index name" rejections that established this). The
        anchor's own expiry is what the returned chain follows — there is
        no separate expiry parameter — so it must already be an *option*
        contract on the exact requested `expiry`, never a futures contract
        (NFO futures are monthly-only, so that anchor always returns the
        monthly chain no matter what was asked for — live-confirmed: NIFTY
        and BANKNIFTY chains anchored on their nearest futures contract came
        back on the identical monthly date, even though NIFTY still lists
        weekly options).

        Raises rather than falling back to a different expiry when nothing
        matches (e.g. a BANKNIFTY weekly, discontinued by NSE) — a strategy
        silently trading a different expiry than the one it asked for is a
        worse failure mode than an explicit error here.

        Cached by `(underlying, expiry)` — unlike a "nearest expiry" cache,
        an exact calendar date never goes stale, so once resolved it's
        reused for the life of the process (`get_option_chain` runs on
        every periodic freshness-gate refresh as well as at strategy start;
        a fresh `SearchScrip` call every time would be pure waste for data
        that can't change).
        """
        cache_key = (underlying, expiry)
        with self._token_lock:
            cached = self._option_anchor_cache.get(cache_key)
        if cached is not None:
            return cached

        rows = self._rest.search_scrip(self._uid, "NFO", underlying)
        available_expiries: set[date] = set()
        for row in rows:
            if not str(row.get("instname", "")).upper().startswith("OPT"):
                continue
            if str(row.get("symname", "")).upper() != underlying.upper():
                continue
            try:
                row_expiry = normalizer.parse_shoonya_date(str(row.get("exd", "")))
            except normalizer.NormalizationError:
                continue
            if row_expiry == expiry:
                tsym = str(row["tsym"])
                with self._token_lock:
                    self._option_anchor_cache[cache_key] = tsym
                return tsym
            available_expiries.add(row_expiry)

        # The mismatch is the whole point of this method's safety guarantee,
        # so the error should be self-diagnosing rather than sending an
        # operator back to guess-and-check against SearchScrip by hand —
        # list what expiries this underlying's chain actually has.
        raise ShoonyaApiError(
            "resolve_option_anchor",
            f"no NFO option contract found for underlying {underlying!r} expiry "
            f"{expiry} — refusing to anchor GetOptionChain on a different "
            "(e.g. monthly) series than what was requested. Available expiries "
            f"for {underlying!r}: {sorted(available_expiries)}",
        )

    def seed_option_anchor(self, underlying: str, expiry: date, tsym: str) -> None:
        """Lets a caller that already knows a good `(underlying, expiry)`
        -> anchor `tsym` mapping (e.g. from this system's own already-
        synced `option_contracts`) pre-populate the same cache
        `_resolve_option_anchor_tsym` otherwise only ever fills via a live
        `SearchScrip` call.

        2026-08-12: `SearchScrip` has shown itself to be genuinely
        unreliable — live-confirmed returning an *empty* result for a
        real, currently-listed underlying+expiry multiple times in the
        same session (see this module's own history / the build plan's
        "Known open items" for the full writeup), not just occasionally
        slow. For an exact calendar expiry, the anchor tsym can never
        change once known — there's no correctness reason to ever prefer
        a fresh, unreliable live call over data this system already knows
        is correct. This adapter deliberately has no DB access of its own
        (`BrokerPort`'s broker-agnostic boundary — see this module's own
        top-level docstring), so the DB lookup that produces the value
        passed in here lives in the caller, not here.
        """
        with self._token_lock:
            self._option_anchor_cache[(underlying, expiry)] = tsym

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
                source=self._settings.ws_auth_source,
            )
            self._ws.start()

        entries = [(symbol, *self._resolve_symbol_token(symbol)) for symbol in contract_symbols]
        self._ws.subscribe(entries)

    def _resolve_symbol_token(self, symbol: str) -> tuple[str, str]:
        """2026-08-12: real gap found and fixed. `MarketDataIngestionService`
        subscribes to an *underlying's* own tick (`"NIFTY"`, never an option
        contract) to build `price_bars`/EMA — and does so from
        `ensure_ingestion_running`, called at `start_strategy` time, *before*
        the strategy runner itself has ever called `get_option_chain` for
        that underlying in this process. `_resolve_token` alone has no
        fallback (correctly so for an option contract — there's no expiry/
        strike context to blind-search for one), so subscribing to an
        underlying before anything else has warmed its token would always
        raise `ShoonyaApiError` here, with nothing upstream (`subscribe_
        quotes`, `BrokerPortMarketDataAdapter.subscribe_ticks`,
        `ensure_ingestion_running`) catching it — the very first
        `POST /strategies/{id}/start` against a fresh Shoonya-backed
        `MARKET_DATA_PROVIDER` would 500. `get_price_history` never hit this
        because it already routes through `_resolve_underlying_token`
        (cache, then a live NSE search_scrip fallback) — this just applies
        the same routing here, for known underlyings only; an option
        contract symbol still goes through the plain, fallback-free
        `_resolve_token` unchanged.
        """
        if symbol.upper() in KNOWN_UNDERLYINGS:
            return self._resolve_underlying_token(symbol)
        return self._resolve_token(symbol)

    def unsubscribe_quotes(self, contract_symbols: list[str]) -> None:
        if self._ws is not None:
            self._ws.unsubscribe(contract_symbols)

    def diagnose_ws_auth(self) -> dict:
        """One-shot, synchronous connect+auth against `ws_host` — bypasses
        `ShoonyaWSClient`'s background reconnect loop so a live diagnostic
        call gets an immediate answer instead of grepping logs. 2026-08-11:
        re-added per the exact recipe this codebase's own project memory
        anticipated for "once Shoonya support finally replies" — this time
        to verify the new "t": "a"/"accesstoken" payload Shoonya support
        specified (see `ws_client.py`'s own docstring) actually works
        against a real account. Sends the identical payload
        `ShoonyaWSClient._authenticate` sends, so a pass/fail here predicts
        the real client's own behavior.
        """
        try:
            with _ws_connect(self._settings.ws_host, open_timeout=10) as ws:
                ws.send(
                    json.dumps(
                        {
                            "t": "a",
                            "uid": self._uid,
                            "actid": self._actid,
                            "accesstoken": self._auth_result.session_token,
                            "source": self._settings.ws_auth_source,
                        }
                    )
                )
                ack_raw = ws.recv(timeout=10)
                ack = json.loads(ack_raw)
                return {
                    "connected": True,
                    "auth_ok": ack.get("t") == "ak" and ack.get("s") == "OK",
                    "ack": ack,
                }
        except Exception as exc:
            return {"connected": False, "error": f"{type(exc).__name__}: {exc}"}

    def diagnose_ws_ticks(
        self,
        contract_symbols: list[str],
        duration_seconds: float = 15.0,
        *,
        warm_underlying: str | None = None,
        warm_expiry: date | None = None,
    ) -> dict:
        """2026-08-12, Phase 0 of verifying Shoonya WS end-to-end: unlike
        `diagnose_ws_auth` (auth handshake only), this exercises the *real*
        production `subscribe_quotes` -> `ShoonyaWSClient` path — the exact
        code any real caller would use — and reports whatever ticks
        actually arrive in `duration_seconds`. Auth succeeding (confirmed
        2026-08-11) says nothing about whether `subscribe`/`tk`/`tf`
        messages actually flow after that; this is what answers that.
        Cleans up after itself (unsubscribes, stops the WS client) so it
        leaves no persistent connection behind — this is a diagnostic, not
        a way to start real streaming for a session.

        `_resolve_token` needs a broker token already cached in this
        adapter instance's own in-memory `_token_by_symbol` — populated by
        `get_instrument_master`/`get_option_chain`, never read back from
        the DB. Real strategies always call `get_option_chain` before
        picking a strike, so this is always warm in real usage; a bare
        diagnostic call skips that step, so `warm_underlying`/
        `warm_expiry`, when given, call `get_option_chain` first to
        replicate it.
        """
        if warm_underlying is not None and warm_expiry is not None:
            try:
                self.get_option_chain(warm_underlying, warm_expiry)
            except Exception as exc:
                return {
                    "error": f"warm-up get_option_chain failed: {type(exc).__name__}: {exc}",
                    "ticks_received": 0,
                    "sample": [],
                }

        received: list[dict] = []
        lock = threading.Lock()

        def _collect(tick: Tick) -> None:
            with lock:
                received.append(
                    {
                        "contract_symbol": tick.contract_symbol,
                        "ltp": tick.ltp,
                        "bid": tick.bid,
                        "ask": tick.ask,
                        "volume": tick.volume,
                        "ts": tick.ts.isoformat(),
                    }
                )

        try:
            self.subscribe_quotes(contract_symbols, on_tick=_collect)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}", "ticks_received": 0, "sample": []}

        try:
            time.sleep(duration_seconds)
        finally:
            try:
                self.unsubscribe_quotes(contract_symbols)
            except Exception:
                logger.exception("Failed to unsubscribe after ws-tick diagnostic")
            # Only tear down the shared connection if nothing else is still
            # subscribed on it — this slot is process-wide, not owned by
            # this diagnostic call alone (see `ShoonyaWSClient.
            # has_subscriptions`'s own docstring for the incident class
            # this guards against).
            if self._ws is not None and not self._ws.has_subscriptions():
                self._ws.stop()
                self._ws = None

        with lock:
            return {"ticks_received": len(received), "sample": received[:5]}

    # -- BrokerPort: orders ----------------------------------------------------

    def place_order(self, request: OrderRequest) -> OrderResult:
        """Ops-Hardening Phase 5: the 1-lot hardcap below is deliberately
        redundant with Risk Service's own DB-configurable `per_trade_lot_cap`
        (`risk_limit_configs`) — defense in depth for the one check that
        directly bounds real-money blast radius, not trusting a single,
        operator-editable layer for it. Checked first, before the
        idempotency-key short-circuit, so a malformed retry can never skip
        it either.
        """
        if request.qty > request.lot_size:
            raise CriticalSafetyException(
                f"place_order blocked: qty={request.qty} exceeds the 1-lot hardcap "
                f"(lot_size={request.lot_size}) for {request.contract_symbol!r} -- "
                "refusing to place a real order above 1 lot."
            )

        if request.idempotency_key in self._orders_by_idempotency_key:
            return self._orders_by_idempotency_key[request.idempotency_key]

        payload = normalizer.to_place_order_payload(request, uid=self._uid, actid=self._actid)
        try:
            raw = self._rest.place_order(payload)
        except ShoonyaApiError as exc:
            # `rest_client._post` wraps every failure into `ShoonyaApiError`
            # uniformly, but `raise ... from exc` (its own code) preserves
            # *which* failure via `__cause__` — an `httpx.HTTPError` cause
            # means the request-response round trip itself never completed
            # (timeout, dropped connection), genuinely ambiguous whether the
            # broker ever received/processed it, unlike a clean `stat:
            # Not_Ok` rejection (no httpx cause — the broker definitely
            # answered). A caller that blindly retries after *this specific*
            # ambiguity risks placing a real duplicate order — the #1
            # failure mode this whole system exists to avoid — so check
            # order history by our own idempotency_key (echoed back
            # verbatim in `remarks`, see `to_place_order_payload`) before
            # ever concluding the placement failed.
            if isinstance(exc.__cause__, httpx.HTTPError):
                found = self._find_order_by_remarks(request.idempotency_key)
                if found is not None:
                    self._orders_by_idempotency_key[request.idempotency_key] = found
                    return found
            raise

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

    def _find_order_by_remarks(self, idempotency_key: str) -> OrderResult | None:
        """Order-ack-timeout fallback: `OrderBook` rows echo back whatever
        `remarks` was submitted with, so a genuinely-placed order can be
        found this way even when `PlaceOrder`'s own response never arrived.
        Returns `None` (not raises) on either "truly not found" or a failed
        lookup itself — either way, the caller's only sane fallback is to
        treat this as "couldn't confirm, re-raise the original ambiguity"
        rather than invent a result.

        Ops-Hardening Phase 5: matches `idempotency_key` as a *prefix* of
        `remarks`, not exact equality — `to_place_order_payload` now appends
        `|{tag}` after the idempotency_key when a real order carries a
        session tag (see that function's own docstring), so remarks is no
        longer always byte-identical to the bare key. `idempotency_key`'s
        own DB-level `unique=True` constraint (`orders`/`trade_intents`) is
        what keeps a prefix match from ever finding the wrong row.
        """
        try:
            rows = self._rest.order_book(self._uid)
        except ShoonyaApiError:
            logger.exception(
                "Order-history fallback lookup failed for idempotency_key=%r", idempotency_key
            )
            return None
        for row in rows:
            remarks = str(row.get("remarks", ""))
            if remarks == idempotency_key or remarks.startswith(f"{idempotency_key}|"):
                return normalizer.parse_order_result(row, idempotency_key=idempotency_key)
        return None

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
