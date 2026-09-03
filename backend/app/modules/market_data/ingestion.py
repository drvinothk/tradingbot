"""Market Data Service: consumes a BrokerPort's tick/depth stream and
persists normalized rows. Each streaming callback opens its own short-lived
session via `session_scope()` rather than sharing one across threads —
SQLAlchemy sessions aren't safe to use concurrently from multiple threads,
and the mock adapter's (and later, the real WebSocket client's) callbacks
fire from a background thread, not the caller's.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db.session import SessionFactory, session_scope
from app.domain.execution.models import Position, PositionStatus
from app.domain.market.models import DepthSnapshot as DepthSnapshotRow
from app.domain.market.models import IndicatorSnapshot as IndicatorSnapshotRow
from app.domain.market.models import Instrument, OptionContract
from app.domain.market.models import OptionChainSnapshot as OptionChainSnapshotRow
from app.domain.market.models import PriceBar as PriceBarRow
from app.domain.market.models import QuoteTick as QuoteTickRow
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.base.contracts import (
    DepthSnapshot,
    OptionChainEntry,
    PriceCandle,
    Tick,
)
from app.modules.broker_adapter.base.contracts import (
    OptionType as BrokerOptionType,
)
from app.modules.broker_adapter.base.errors import BrokerError, BrokerRateLimitedError
from app.modules.market_data.indicators.bar_aggregator import Bar
from app.modules.market_data.indicators.engine import IndicatorEngine
from app.modules.market_data.providers.base import BaseMarketDataProvider
from app.modules.market_data.tick_plausibility import is_plausible_option_tick

logger = logging.getLogger("app.market_data")

# How long to wait after subscribing before deciding WS isn't delivering
# ticks and falling back to REST polling for this symbol — long enough to
# not misfire on a normal reconnect blip (`ShoonyaWSClient`'s own backoff
# caps at 30s between attempts), short enough that a real outage doesn't
# leave price_bars empty for minutes before anyone notices.
_WS_HEALTH_GRACE_SECONDS = 15.0

# Per-symbol override for the above -- VIX/PCR environment-metrics feed
# (2026-08-19). India VIX is a computed index, not a continuously-traded
# instrument: `scripts/shoonya_ws_quality_diagnostic.py`'s own live-observed
# data found gaps as low as ~2 ticks/60s during genuinely healthy streaming
# (vs. NIFTY/BANKNIFTY's 350+/min) -- the flat 15s default would keep VIX
# perpetually flagged unhealthy and permanently on REST fallback (itself
# untested for an index symbol, unlike the option-underlying candles this
# fallback path was actually built for). 60s matches that diagnostic's own
# empirically-derived threshold.
_WS_HEALTH_GRACE_SECONDS_BY_SYMBOL: dict[str, float] = {
    "INDIA VIX": 60.0,
}

# 2026-08-20 — real live incident: the "NIFTY" underlying's own WS tick
# stream silently carried a different real instrument's price (~120-180,
# option-premium-scale) for an extended period while every other signal
# (connection alive, ticks arriving on schedule, real-looking volume)
# looked completely healthy — corrupting price_bars/VWAP/EMA for every
# strategy sharing that instrument (they all do; there's one shared row
# per underlying). Root-caused as far as static analysis + live read-only
# re-verification could reach: ShoonyaBrokerAdapter._resolve_underlying_
# token's own resolution logic re-verified correct on a fresh process
# (same live account, same day) every time it was re-tested — meaning
# whatever mismatched the *live* process's cached token did so through a
# path not reproducible on demand (most likely a one-time race during the
# reconnect window), not a deterministic bug in the matching logic itself.
# Since the exact trigger couldn't be pinned down with certainty, this
# guards the *symptom class* directly rather than only the one suspected
# mechanism: a known underlying's own ingested price is checked against a
# minimum plausible floor (real index levels are always in the thousands;
# an option premium or a broken/garbage read never is) before it's ever
# allowed to reach quote_ticks/price_bars/indicator_snapshots, on both the
# WS-tick path (_on_tick) and the REST-fallback candle path (_poll_once) —
# deliberately both, since get_price_history routes through the exact same
# underlying-token cache and would carry an identical corruption if that
# cache were ever the actual cause. A rejected tick/candle is simply
# dropped (never persisted) rather than "corrected" — the existing
# freshness-gating machinery (market_data.freshness) already treats an
# instrument with no fresh data as STALE/DEAD and correctly refuses to
# trade on it; that's the right degraded behavior here too, not something
# this guard needs to reimplement. Deliberately a *floor* only, not a
# tight band — the goal is catching "wrong instrument entirely," not
# tracking real price movement, which this module has no business judging.
_MIN_PLAUSIBLE_PRICE_BY_SYMBOL: dict[str, float] = {
    "NIFTY": 5000.0,
    "BANKNIFTY": 10000.0,
}

# 2026-09-02 — the mirror-image live incident: a real NIFTY spot tick
# (~23,870) was returned by the broker under an *option* contract's symbol
# instead of the underlying's own (see tick_plausibility.py's module
# docstring for the full incident + why the check itself lives there,
# shared with `record_option_chain_snapshot` below and
# `execution_engine.paper.service.current_contract_price`). Unlike the
# underlying-side guard above, an option contract has no per-symbol entry
# here — there are hundreds of them — so this is a cap on how many distinct
# option symbols get a detailed log line, not a plausibility threshold
# itself (that lives in tick_plausibility.MAX_PLAUSIBLE_OPTION_PREMIUM).
_MAX_IMPLAUSIBLE_OPTION_SYMBOLS_LOGGED = 25

# REST-poll cadence — deliberately shorter than the 60s bar timeframe so a
# just-closed candle is picked up within roughly half a bar, not up to a
# full minute late, while staying far under Shoonya's 10/sec GetQuotes-class
# rate limit even with several underlyings polling concurrently.
_REST_POLL_INTERVAL_SECONDS = 25.0

# How far back to ask for on each poll — generous enough to tolerate one
# missed cycle (a slow response, a transient error) without gapping
# price_bars, cheap since TPSeries-class endpoints return a whole window in
# one call regardless of span.
_REST_POLL_LOOKBACK = timedelta(minutes=5)

# Dedicated backoff for a rate-limit rejection specifically -- see
# BrokerRateLimitedError's own docstring for why this needs to be much
# longer than _REST_POLL_INTERVAL_SECONDS.
_RATE_LIMIT_BACKOFF_SECONDS = 300.0

# How long a symbol may go with zero candles back from a REST poll before
# it's worth a warning -- time-based, not a consecutive-empty-cycle count,
# deliberately: a cycle count is only meaningful relative to one particular
# `rest_poll_interval_seconds`, and this codebase already expects that to
# vary (a slower/faster broker, a future TrueData provider, a hypothetical
# future caller driving its own wall-clock-aligned cadence instead of this
# class's own free-running loop). A fixed wall-clock threshold means
# "no real data in over 90s" reads the same regardless of what's polling or
# how often. 90s is a little over 3x the default 25s poll interval -- long
# enough that one or two genuinely-empty cycles (e.g. the first minute after
# market open) don't misfire, short enough to catch a real stall quickly.
_EMPTY_POLL_WARNING_THRESHOLD_SECONDS = 90.0

# 2026-08-20: how often (in REST poll cycles) a fallen-back symbol gets a
# WS-recovery probe -- see `_try_ws_recovery`'s own docstring for the full
# mechanism. 10 cycles at the default 25s poll interval is ~4 minutes;
# frequent enough that VWAP doesn't stay frozen for hours after a real WS
# recovery, infrequent enough not to churn subscribe/unsubscribe calls or
# pause REST polling (each probe blocks polling for up to one grace window)
# more than necessary.
_WS_RECOVERY_PROBE_EVERY_N_POLLS = 10

# How many already-persisted completed bars to replay through
# `IndicatorEngine.warm_start` when a fresh (post-restart) engine first sees
# an underlying -- see that method's own docstring for why this exists at
# all. 60 is well past both EMA9 (9) and EMA20 (20)'s own warmup sample
# counts, so both are guaranteed to warm together; it's also generous enough
# that the replayed EMA20 has genuinely converged rather than sitting right
# at its just-warmed SMA seed (EMA20's alpha=2/21 means a bar 60 replays back
# has decayed to ~(19/21)^60 ≈ 0.3% of its original weight -- effectively
# fully converged). Harmless if fewer than 60 bars exist yet (e.g. a restart
# soon after market open) -- `warm_start` just replays whatever's available,
# same "not enough history yet -> None" semantics `EMACalculator` already has.
_WARM_START_LOOKBACK_BARS = 60

_SymbolRef = tuple[str, uuid.UUID]  # ("instrument" | "option_contract", row id)


class MarketDataIngestionService:
    """Consumes ticks strictly through `BaseMarketDataProvider` — never a
    concrete provider, never `BrokerPort` — so the strategy/indicator layer
    downstream of this service is vendor-agnostic regardless of which live
    feed (Angel One, a Shoonya/mock shim) is actually wired in behind it.
    See `market_data/provider_composition.py`'s own docstring for the "why
    a second port, not a bigger BrokerPort" reasoning.

    `session_factory` defaults to the real app-wide `session_scope` (each
    streaming callback fires on a background thread, so it needs its own
    short-lived session rather than sharing a caller-provided one) — but is
    injectable so tests can point it at an isolated test database instead of
    monkeypatching module internals.

    `indicator_engine` is optional — when supplied, every tick for an
    *underlying* (never an option contract; see IndicatorEngine's own docs)
    also updates VWAP/EMA9/EMA20 and persists whatever changed in the same
    transaction as the tick itself.

    **REST-polling fallback**: `start()` schedules a one-shot health check
    (`_WS_HEALTH_GRACE_SECONDS` after subscribing) for each *underlying*
    symbol; if no WS tick has landed by then, that symbol switches to
    polling `BrokerPort.get_price_history` on a timer instead
    (`_start_rest_fallback`/`_poll_loop`) — built for the real, live case
    where Shoonya's WS auth never once succeeded even though REST works
    fine. One-way per symbol for the life of this instance; see
    `_start_rest_fallback`'s own docstring for why switching back isn't
    attempted. Option-contract symbols never fall back (no per-contract
    history call is made for this) — `price_bars`/EMA are underlying-only
    already, per this class's own existing behavior above.
    """

    def __init__(
        self,
        provider: BaseMarketDataProvider,
        session_factory: SessionFactory = session_scope,
        indicator_engine: IndicatorEngine | None = None,
        *,
        ws_health_grace_seconds: float = _WS_HEALTH_GRACE_SECONDS,
        ws_health_grace_seconds_by_symbol: dict[str, float] | None = None,
        rest_poll_interval_seconds: float = _REST_POLL_INTERVAL_SECONDS,
        rate_limit_backoff_seconds: float = _RATE_LIMIT_BACKOFF_SECONDS,
        min_plausible_price_by_symbol: dict[str, float] | None = None,
        ws_recovery_probe_every_n_polls: int = _WS_RECOVERY_PROBE_EVERY_N_POLLS,
        warm_start_lookback_bars: int = _WARM_START_LOOKBACK_BARS,
    ) -> None:
        self._provider = provider
        self._session_factory = session_factory
        self._indicator_engine = indicator_engine
        self._warm_start_lookback_bars = warm_start_lookback_bars
        self._symbol_map: dict[str, _SymbolRef] = {}

        # WS-health watchdog / REST-polling fallback — see module docstring.
        # `_last_tick_at` is only ever written by `_on_tick`; a symbol
        # missing from it after the grace window means WS genuinely
        # delivered nothing, not just a slow first tick.
        self._ws_health_grace_seconds = ws_health_grace_seconds
        self._ws_health_grace_seconds_by_symbol = (
            ws_health_grace_seconds_by_symbol
            if ws_health_grace_seconds_by_symbol is not None
            else _WS_HEALTH_GRACE_SECONDS_BY_SYMBOL
        )
        self._rest_poll_interval_seconds = rest_poll_interval_seconds
        self._rate_limit_backoff_seconds = rate_limit_backoff_seconds
        # See _MIN_PLAUSIBLE_PRICE_BY_SYMBOL's own module-level docstring.
        self._min_plausible_price_by_symbol = (
            min_plausible_price_by_symbol
            if min_plausible_price_by_symbol is not None
            else _MIN_PLAUSIBLE_PRICE_BY_SYMBOL
        )
        self._implausible_price_logged: set[str] = set()
        # Option contracts number in the hundreds across a trading day
        # (every strike x expiry touched), unlike the 2-symbol underlying
        # set _implausible_price_logged was sized for -- a persistent
        # option-side corruption bug hitting many strikes could still flood
        # logs at "once per symbol" granularity. Capped separately: once
        # _MAX_IMPLAUSIBLE_OPTION_SYMBOLS_LOGGED distinct option symbols
        # have each logged their one detailed warning, later ones are
        # counted but not logged in detail -- see
        # _warn_implausible_option_price_once.
        self._implausible_option_price_logged: set[str] = set()
        self._implausible_option_price_suppressed_count = 0
        self._last_tick_at: dict[str, datetime] = {}
        self._fallback_symbols: set[str] = set()
        self._ws_recovery_probe_every_n_polls = ws_recovery_probe_every_n_polls
        self._poll_threads: dict[str, threading.Thread] = {}
        self._last_polled_bucket: dict[str, datetime | None] = {}
        # Wall-clock time a REST poll last returned at least one candle for
        # this symbol, broker-agnostic (Angel One, Shoonya, or a future
        # TrueData provider all funnel through the same _poll_once) -- see
        # _EMPTY_POLL_WARNING_THRESHOLD_SECONDS. Absent for a symbol that
        # has never once had a successful poll since this instance started
        # (startup, or every poll so far has come back empty) -- handled as
        # "unknown, don't warn yet" in _poll_once, not treated as a stall.
        self._last_valid_data_time: dict[str, datetime] = {}

    def _grace_seconds_for(self, symbol: str) -> float:
        return self._ws_health_grace_seconds_by_symbol.get(symbol, self._ws_health_grace_seconds)

    def _is_plausible_price(self, symbol: str, price: float) -> bool:
        """See `_MIN_PLAUSIBLE_PRICE_BY_SYMBOL`'s own module-level docstring
        for the live incident this exists to catch. A symbol with no
        configured floor (every option contract, and any underlying not
        explicitly listed) is always plausible as far as this check is
        concerned — this is deliberately a narrow, known-underlyings-only
        guard, not a general price-sanity system.
        """
        floor = self._min_plausible_price_by_symbol.get(symbol)
        return floor is None or price >= floor

    def _warn_implausible_price_once(self, symbol: str, price: float, source: str) -> None:
        if symbol in self._implausible_price_logged:
            return
        self._implausible_price_logged.add(symbol)
        logger.error(
            "REJECTED implausible %s price %.4f for known underlying %r (below configured "
            "floor %.1f) -- some real broker data is being received under the wrong symbol "
            "(a token-resolution mismatch, not a market move; a real index never trades this "
            "low). Dropping rather than persisting -- price_bars/indicators for %r will go "
            "stale until this clears, which is the safe degraded behavior. Logged once per "
            "symbol per process to avoid flooding logs on every subsequent tick/poll.",
            source,
            price,
            symbol,
            self._min_plausible_price_by_symbol.get(symbol, 0.0),
            symbol,
        )

    def _warn_implausible_option_price_once(
        self, symbol: str, ltp: float, bid: float, ask: float, volume: int, source: str
    ) -> None:
        """Option-side counterpart to `_warn_implausible_price_once` — see
        that method and `tick_plausibility.py`'s module docstring for the
        incident this exists to catch. Capped at
        `_MAX_IMPLAUSIBLE_OPTION_SYMBOLS_LOGGED` distinct symbols (not just
        deduped per-symbol like the underlying side) since an option-side
        corruption bug could plausibly hit far more than the 2 symbols the
        underlying guard was sized for.
        """
        if symbol in self._implausible_option_price_logged:
            return
        if len(self._implausible_option_price_logged) >= _MAX_IMPLAUSIBLE_OPTION_SYMBOLS_LOGGED:
            self._implausible_option_price_suppressed_count += 1
            if self._implausible_option_price_suppressed_count == 1:
                logger.error(
                    "REACHED %d distinct option symbols with a REJECTED implausible price "
                    "(source=%s) -- further occurrences will be counted, not logged in detail, "
                    "to avoid flooding logs. This many distinct symbols affected suggests a "
                    "systemic issue, not an isolated bad tick -- worth investigating directly "
                    "rather than relying on per-symbol log lines.",
                    _MAX_IMPLAUSIBLE_OPTION_SYMBOLS_LOGGED,
                    source,
                )
            return
        self._implausible_option_price_logged.add(symbol)
        logger.error(
            "REJECTED implausible %s price for option contract %r: ltp=%.4f bid=%.4f ask=%.4f "
            "volume=%d -- looks like a leaked underlying/wrong-instrument tick, not a real "
            "option premium (see tick_plausibility.py). Dropping rather than persisting -- "
            "falls through to the existing REST-snapshot/broker.get_quote fallback chain "
            "instead. Logged once per option symbol per process (capped at %d distinct symbols) "
            "to avoid flooding logs.",
            source,
            symbol,
            ltp,
            bid,
            ask,
            volume,
            _MAX_IMPLAUSIBLE_OPTION_SYMBOLS_LOGGED,
        )

    def _build_symbol_map(self, contract_symbols: list[str]) -> dict[str, _SymbolRef]:
        symbol_map: dict[str, _SymbolRef] = {}
        with self._session_factory() as db:
            for instrument in db.query(Instrument).filter(Instrument.symbol.in_(contract_symbols)):
                symbol_map[instrument.symbol] = ("instrument", instrument.id)
            for contract in db.query(OptionContract).filter(
                OptionContract.symbol.in_(contract_symbols)
            ):
                symbol_map[contract.symbol] = ("option_contract", contract.id)
        return symbol_map

    def _warm_start_indicators(self, instrument_id: uuid.UUID) -> None:
        """Reads the last `_warm_start_lookback_bars` already-persisted
        `price_bars` rows for `instrument_id` and replays them through
        `IndicatorEngine.warm_start` -- see that method's own docstring for
        why. A no-op when `_indicator_engine` is absent (checked by the one
        caller, `start`, before this is invoked) or when nothing has ever
        been persisted for this instrument yet (a genuinely new instrument,
        or the very first bar of a trading day) -- `warm_start` handles an
        empty list the same as `EMACalculator` already handles "not enough
        history yet": nothing changes, live ticks warm up from zero exactly
        as they always have.
        """
        assert self._indicator_engine is not None
        timeframe = f"{self._indicator_engine.timeframe_seconds}s"
        # Bar objects must be built *inside* this block, from row attributes
        # read before the session closes -- reading them after (e.g. in a
        # comprehension outside the `with`) hits SQLAlchemy's own
        # DetachedInstanceError, the exact trap this codebase's
        # `record_option_chain_snapshot` docstring already documents once.
        with self._session_factory() as db:
            rows = (
                db.query(PriceBarRow)
                .filter(
                    PriceBarRow.instrument_id == instrument_id,
                    PriceBarRow.timeframe == timeframe,
                )
                .order_by(PriceBarRow.bucket_start.desc())
                .limit(self._warm_start_lookback_bars)
                .all()
            )
            bars = [
                Bar(
                    bucket_start=row.bucket_start,
                    open=float(row.open),
                    high=float(row.high),
                    low=float(row.low),
                    close=float(row.close),
                    volume=row.volume,
                )
                for row in reversed(rows)  # ascending -- oldest first, see warm_start's docstring
            ]
        if not bars:
            return
        self._indicator_engine.warm_start(instrument_id, bars)

    def start(self, contract_symbols: list[str]) -> None:
        self._symbol_map.update(self._build_symbol_map(contract_symbols))
        unknown = set(contract_symbols) - set(self._symbol_map)
        if unknown:
            logger.warning(
                "subscribe requested for %d symbol(s) not found in instruments/"
                "option_contracts — ticks for them will be silently dropped "
                "until the instrument master is synced: %s",
                len(unknown),
                sorted(unknown),
            )
        # Must run before subscribe_ticks below -- warm_start is a no-op once
        # a live tick has already reached this instrument (see its own
        # docstring), so warming late would silently do nothing.
        if self._indicator_engine is not None:
            for symbol in contract_symbols:
                ref = self._symbol_map.get(symbol)
                if ref is not None and ref[0] == "instrument":
                    self._warm_start_indicators(ref[1])
        self._provider.subscribe_ticks(
            contract_symbols, on_tick=self._on_tick, on_depth=self._on_depth
        )
        for symbol in contract_symbols:
            # Only underlyings ever fall back to REST (see
            # `_start_rest_fallback`'s own docstring) — no point scheduling
            # a health check for an option-contract symbol that could never
            # act on it. Re-subscribing an already-watched or already-
            # fallen-back symbol (idempotent `start`, per this class's own
            # contract) must not schedule a second, redundant watchdog.
            ref = self._symbol_map.get(symbol)
            if ref is None or ref[0] != "instrument":
                continue
            if symbol in self._fallback_symbols or symbol in self._last_tick_at:
                continue
            grace_seconds = self._grace_seconds_for(symbol)
            timer = threading.Timer(grace_seconds, self._check_ws_health, args=(symbol,))
            timer.daemon = True
            timer.start()

    def _check_ws_health(self, symbol: str) -> None:
        if symbol in self._fallback_symbols or symbol in self._last_tick_at:
            return  # already on REST, or WS came through in time
        logger.warning(
            "No WS tick received for %r within %.0fs of subscribing — "
            "falling back to REST polling for price_bars",
            symbol,
            self._grace_seconds_for(symbol),
        )
        self._start_rest_fallback(symbol)

    def _start_rest_fallback(self, symbol: str) -> None:
        """**No longer strictly one-way, as of 2026-08-20** — the original
        reasoning here ("this codebase has never once seen WS work, so
        there's nothing to test recovery against") is stale now that real,
        sustained Shoonya WS ticks are proven live for hours at a stretch.
        `_poll_loop` now periodically probes for WS recovery
        (`_try_ws_recovery`) and promotes the symbol back if it succeeds —
        see that method's own docstring for why a probe, not a plain
        concurrent re-subscribe, is what makes this safe. Still explicitly
        drops the WS subscription here before REST polling starts (so a
        stray WS tick can't race a REST-polled insert for the same
        `price_bars` bucket in the meantime) — the same "stopped must mean
        no more callbacks fire" discipline `unsubscribe_quotes` already
        promises elsewhere in this codebase.
        """
        ref = self._symbol_map.get(symbol)
        if ref is None or ref[0] != "instrument":
            return
        instrument_id = ref[1]
        self._fallback_symbols.add(symbol)
        # `_last_polled_bucket` is in-memory only and forgets everything
        # across a restart — without this seed, the first poll after any
        # restart would re-consider every already-persisted bar within
        # `_REST_POLL_LOOKBACK` as "new," hit `uq_price_bar_bucket`, and get
        # stuck: `_persist_candle`'s writes for a whole poll cycle share one
        # transaction, so a duplicate anywhere in the batch rolls all of it
        # back, `_last_polled_bucket` never advances past it either, and the
        # next cycle repeats the identical failure forever. Live-reproduced
        # repeatedly during 2026-08-05's restart-heavy session.
        self._last_polled_bucket[symbol] = self._latest_persisted_bucket(instrument_id)
        try:
            self._provider.unsubscribe_ticks([symbol])
        except Exception:
            logger.exception(
                "Failed to unsubscribe %r from WS before starting REST fallback "
                "— continuing anyway, since a stray WS tick landing on an "
                "instrument PriceBar insert would fail loud on the DB's own "
                "uq_price_bar_bucket constraint rather than corrupt data",
                symbol,
            )
        thread = threading.Thread(
            target=self._poll_loop, args=(symbol, instrument_id), daemon=True
        )
        self._poll_threads[symbol] = thread
        thread.start()

    def _latest_persisted_bucket(self, instrument_id: uuid.UUID) -> datetime | None:
        """The real, durable source of truth `_last_polled_bucket` should
        have been seeded from all along — `None` for an instrument that has
        genuinely never had a bar persisted, same as `_last_polled_bucket`'s
        own prior default for a fresh symbol.
        """
        timeframe_seconds = (
            self._indicator_engine.timeframe_seconds if self._indicator_engine is not None else 60
        )
        with self._session_factory() as db:
            return db.query(func.max(PriceBarRow.bucket_start)).filter(
                PriceBarRow.instrument_id == instrument_id,
                PriceBarRow.timeframe == f"{timeframe_seconds}s",
            ).scalar()

    def _poll_loop(self, symbol: str, instrument_id: uuid.UUID) -> None:
        poll_count = 0
        while symbol in self._fallback_symbols:
            poll_count += 1
            if poll_count % self._ws_recovery_probe_every_n_polls == 0 and self._try_ws_recovery(
                symbol
            ):
                return  # promoted back to WS -- this poll loop's job is done
            if symbol not in self._fallback_symbols:
                return
            wait_seconds = self._rest_poll_interval_seconds
            try:
                self._poll_once(symbol, instrument_id)
            except BrokerRateLimitedError:
                # A dedicated, much longer backoff than the normal retry
                # cadence -- live-confirmed 2026-08-06 that continuing to
                # poll at the usual ~25s interval after a rate-limit
                # rejection just keeps hammering an already-limited
                # endpoint, for no benefit (a relogin doesn't reset a
                # rate-limit counter, so there is nothing a faster retry
                # could accomplish here). Angel's own reset window isn't
                # documented anywhere this codebase has found -- this is a
                # conservative, not-guessed choice: long enough to
                # meaningfully stop hammering, short enough not to leave
                # price_bars dark for hours once the limit actually clears.
                logger.exception(
                    "REST poll rate-limited for %r; backing off %.0fs instead of the "
                    "normal %.0fs interval",
                    symbol,
                    self._rate_limit_backoff_seconds,
                    self._rest_poll_interval_seconds,
                )
                wait_seconds = self._rate_limit_backoff_seconds
            except Exception:
                logger.exception("REST poll failed for %r; will retry next cycle", symbol)
            # Checked in short slices, not one long sleep, so `stop()`
            # dropping this symbol from `_fallback_symbols` is noticed
            # promptly rather than up to a full interval (or the rate-limit
            # backoff) late.
            for _ in range(int(wait_seconds * 10)):
                if symbol not in self._fallback_symbols:
                    return
                threading.Event().wait(0.1)

    def _try_ws_recovery(self, symbol: str) -> bool:
        """2026-08-20: called from `_poll_loop` every
        `_ws_recovery_probe_every_n_polls` cycles. Deliberately pauses REST
        polling for the duration of the probe rather than re-subscribing WS
        *alongside* an active poll loop — `_on_tick` has no fallback-aware
        guard of its own (it processes any tick for a known symbol
        unconditionally), so a stray WS tick landing while `_poll_once` is
        also running for the same symbol could drive `IndicatorEngine`
        through both `on_tick` and `on_completed_bar` concurrently for the
        same instrument — a real risk to indicator state, not just a
        `uq_price_bar_bucket` bump. Pausing during the probe avoids that
        overlap entirely; this method runs synchronously inside
        `_poll_loop`'s own thread, so there's nothing else to coordinate
        with.

        Reuses `_grace_seconds_for` — the exact same window the *original*
        fallback decision was judged against — for symmetry: if that
        duration was long enough to conclude "WS isn't delivering," it's
        the right duration to conclude "WS is delivering again," too.
        Returns True (and leaves the symbol removed from
        `_fallback_symbols`) only if a real tick arrived within the
        window; on any failure to promote, re-establishes the REST
        subscription exactly as `_start_rest_fallback` originally did and
        returns False so `_poll_loop` continues polling unchanged.
        """
        self._last_tick_at.pop(symbol, None)
        probe_started = datetime.now(UTC)
        try:
            self._provider.subscribe_ticks(
                [symbol], on_tick=self._on_tick, on_depth=self._on_depth
            )
        except Exception:
            logger.exception(
                "WS recovery probe subscribe failed for %r; staying on REST polling", symbol
            )
            return False

        grace_seconds = self._grace_seconds_for(symbol)
        deadline = probe_started + timedelta(seconds=grace_seconds)
        while datetime.now(UTC) < deadline:
            if symbol not in self._fallback_symbols:
                return True  # something else already promoted it
            last_tick = self._last_tick_at.get(symbol)
            if last_tick is not None and last_tick >= probe_started:
                self._fallback_symbols.discard(symbol)
                logger.warning(
                    "WS recovered for %r after a recovery probe -- promoting back from "
                    "REST polling to WS-driven ticks (VWAP resumes)",
                    symbol,
                )
                return True
            threading.Event().wait(0.5)

        logger.info(
            "WS recovery probe for %r found no tick within %.0fs -- staying on REST polling",
            symbol,
            grace_seconds,
        )
        try:
            self._provider.unsubscribe_ticks([symbol])
        except Exception:
            logger.exception(
                "Failed to unsubscribe %r after a failed WS recovery probe -- continuing "
                "on REST regardless",
                symbol,
            )
        return False

    def _poll_once(self, symbol: str, instrument_id: uuid.UUID) -> None:
        timeframe_seconds = (
            self._indicator_engine.timeframe_seconds if self._indicator_engine is not None else 60
        )
        now = datetime.now(UTC)
        candles = self._provider.get_price_history(
            symbol, now - _REST_POLL_LOOKBACK, now, timeframe_seconds
        )

        if candles:
            self._last_valid_data_time[symbol] = now
        else:
            # `_last_valid_data_time.get(symbol)` is None on this instance's
            # very first poll for this symbol (nothing to measure a stall
            # against yet) and on every cycle up to this one having also
            # come back empty — in both cases there's no prior "last good"
            # moment to diff against, so this is correctly silent rather
            # than warning off a missing baseline.
            last_valid = self._last_valid_data_time.get(symbol)
            if last_valid is not None:
                stall_seconds = (now - last_valid).total_seconds()
                if stall_seconds > _EMPTY_POLL_WARNING_THRESHOLD_SECONDS:
                    logger.warning(
                        "REST poll for %r has returned zero candles for %.0fs (last real "
                        "data at %s) — broker may be silently throttling (a successful, "
                        "non-erroring response with no data) rather than erroring outright",
                        symbol,
                        stall_seconds,
                        last_valid.isoformat(),
                    )

        last_seen = self._last_polled_bucket.get(symbol)
        new_candles = [
            c
            for c in candles
            # A broker's "latest" candle can be the one still forming —
            # only a bucket that has fully closed is safe to persist as
            # a completed bar (see PriceCandle/get_price_history's own
            # docstrings on why this can't be inferred from the row alone).
            if c.bucket_start + timedelta(seconds=timeframe_seconds) <= now
            and (last_seen is None or c.bucket_start > last_seen)
        ]
        if not new_candles:
            return
        new_candles.sort(key=lambda c: c.bucket_start)

        # See _MIN_PLAUSIBLE_PRICE_BY_SYMBOL's own module-level docstring --
        # get_price_history routes through the exact same underlying-token
        # cache the WS path does, so a rejected candle here isn't
        # hypothetical, it's the same real corruption via a different
        # transport. _last_polled_bucket deliberately does NOT advance past
        # a rejected candle -- it stays eligible to be reconsidered (and
        # correctly persisted) on the very next poll if the underlying
        # cause ever clears, rather than being permanently skipped.
        plausible_candles = []
        for candle in new_candles:
            if self._is_plausible_price(symbol, candle.close):
                plausible_candles.append(candle)
            else:
                self._warn_implausible_price_once(symbol, candle.close, "REST candle")
        if not plausible_candles:
            return

        with self._session_factory() as db:
            for candle in plausible_candles:
                self._persist_candle(db, instrument_id, candle, timeframe_seconds)
        self._last_polled_bucket[symbol] = plausible_candles[-1].bucket_start

    def _persist_candle(
        self, db: Session, instrument_id: uuid.UUID, candle: PriceCandle, timeframe_seconds: int
    ) -> None:
        # Same shape as `_on_tick`'s bar-completion branch: a QuoteTick so
        # anything reading "latest tick" for this instrument still sees
        # something (its own freshness display just reads as
        # degraded/stale between ~25s polls rather than continuously live —
        # cosmetic, doesn't gate strategy evaluation, see
        # `classify_latest_tick`), an IndicatorSnapshot per EMA that
        # updated, and the PriceBar itself.
        db.add(
            QuoteTickRow(
                id=uuid.uuid4(),
                instrument_id=instrument_id,
                option_contract_id=None,
                ltp=candle.close,
                bid=candle.close,
                ask=candle.close,
                volume=candle.volume,
                oi=None,
                ts=candle.bucket_start + timedelta(seconds=timeframe_seconds),
            )
        )
        if self._indicator_engine is not None:
            updated = self._indicator_engine.on_completed_bar(instrument_id, candle)
            for indicator_name, value in updated.items():
                db.add(
                    IndicatorSnapshotRow(
                        id=uuid.uuid4(),
                        instrument_id=instrument_id,
                        indicator_name=indicator_name,
                        timeframe=f"{timeframe_seconds}s",
                        value=value,
                        ts=candle.bucket_start,
                    )
                )
        db.add(
            PriceBarRow(
                id=uuid.uuid4(),
                instrument_id=instrument_id,
                timeframe=f"{timeframe_seconds}s",
                bucket_start=candle.bucket_start,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
            )
        )

    @property
    def fallback_symbols(self) -> frozenset[str]:
        """Read-only view of which symbols are currently REST-polling
        instead of WS — used by `market_data.registry
        .reset_subscriptions_for_new_day` to know which symbols must NOT
        be touched by a daily resubscribe (see `forget_symbol`'s own
        docstring for why).
        """
        return frozenset(self._fallback_symbols)

    def forget_symbol(self, symbol: str) -> None:
        """Clears WS-health tracking (`_last_tick_at`) for one symbol so
        the next `start()` call re-arms its watchdog timer, as if this
        symbol had never been subscribed before. Deliberately leaves
        `_fallback_symbols`/`_poll_threads` untouched — a symbol already on
        REST fallback must stay there (see `_start_rest_fallback`'s own
        "one-way ... for the life of this instance" contract); calling
        `start()` again for it would re-send a WS subscribe that could race
        a REST-polled insert for the same `price_bars` bucket, exactly what
        that fallback's own unsubscribe-on-switch was built to prevent.

        **Live bug fixed 2026-08-19**: `MarketDataScheduler`'s daily
        PRE_MARKET transition disconnects and reconnects the market-data
        provider — a genuinely new WS session server-side — but nothing
        told this service its `_last_tick_at`/`registry._subscribed_symbols`
        bookkeeping was now stale. `registry.ensure_ingestion_running` saw
        every underlying as "already subscribed" from the *previous* day's
        now-dead connection and never re-sent the real subscribe request on
        the new one; even a fixed `_subscribed_symbols` alone wouldn't have
        been enough, since `start()`'s own watchdog-scheduling loop also
        skips arming a fresh timer for any symbol still in `_last_tick_at`
        (`if symbol in self._fallback_symbols or symbol in
        self._last_tick_at: continue`) — silently disarming the one safety
        net (`_check_ws_health`) that should have caught this. Confirmed
        live: zero `quote_ticks` for a full trading day despite strategies
        scanning normally and TrueData reporting a clean reconnect.
        """
        self._last_tick_at.pop(symbol, None)

    def stop(self, contract_symbols: list[str]) -> None:
        fallback_here = [s for s in contract_symbols if s in self._fallback_symbols]
        still_ws = [s for s in contract_symbols if s not in self._fallback_symbols]
        if still_ws:
            self._provider.unsubscribe_ticks(still_ws)
        for symbol in fallback_here:
            # Remove-then-join, not the reverse — `_poll_loop` checks
            # membership every 0.1s, so dropping it first is what actually
            # makes this a bounded wait rather than racing the thread's own
            # sleep. Same "stopped must mean no more callbacks fire, not
            # just asked to stop" discipline as `ShoonyaWSClient.stop`.
            self._fallback_symbols.discard(symbol)
            thread = self._poll_threads.pop(symbol, None)
            if thread is not None:
                thread.join(timeout=5.0)

    def reset_daily_indicators(self) -> None:
        """Called once per trading day (see `market_data_scheduler
        .MarketDataScheduler`'s PRE_MARKET transition) so VWAP — a
        session-cumulative value, see `IndicatorEngine.reset_session`'s own
        docstring — actually starts fresh each day instead of accumulating
        across days for the life of this process. No-op when constructed
        without an `indicator_engine` (mirrors every other `if self.
        _indicator_engine is not None` guard already in this class).
        """
        if self._indicator_engine is not None:
            self._indicator_engine.reset_session()

    def _on_tick(self, tick: Tick) -> None:
        ref = self._symbol_map.get(tick.contract_symbol)
        if ref is None:
            return
        kind, row_id = ref
        if kind == "instrument" and not self._is_plausible_price(tick.contract_symbol, tick.ltp):
            self._warn_implausible_price_once(tick.contract_symbol, tick.ltp, "WS tick")
            # Deliberately does NOT update _last_tick_at -- a rejected tick
            # must not look like a healthy one to the WS-health watchdog.
            # Falling back to REST goes through this exact same guard (see
            # _poll_once), so it can't silently "fix" this by switching
            # paths; both correctly degrade to stale/no-data instead.
            return
        if kind == "option_contract" and not is_plausible_option_tick(
            tick.ltp, tick.bid, tick.ask, tick.volume
        ):
            self._warn_implausible_option_price_once(
                tick.contract_symbol, tick.ltp, tick.bid, tick.ask, tick.volume, "WS tick"
            )
            # Same "don't fake freshness for a rejected value" discipline as
            # the instrument branch above -- mirrored regardless of whether
            # anything currently watches an option contract's own tick
            # recency the way the WS-health watchdog does for underlyings.
            return
        # Recorded regardless of what happens below — this is the WS-health
        # signal `_check_ws_health` reads, decoupled from whether the DB
        # write itself succeeds.
        self._last_tick_at[tick.contract_symbol] = tick.ts
        with self._session_factory() as db:
            db.add(
                QuoteTickRow(
                    id=uuid.uuid4(),
                    instrument_id=row_id if kind == "instrument" else None,
                    option_contract_id=row_id if kind == "option_contract" else None,
                    ltp=tick.ltp,
                    bid=tick.bid,
                    ask=tick.ask,
                    volume=tick.volume,
                    oi=tick.oi,
                    ts=tick.ts,
                )
            )

            if kind == "instrument" and self._indicator_engine is not None:
                updated, completed_bar = self._indicator_engine.on_tick(row_id, tick)
                for indicator_name, value in updated.items():
                    db.add(
                        IndicatorSnapshotRow(
                            id=uuid.uuid4(),
                            instrument_id=row_id,
                            indicator_name=indicator_name,
                            timeframe=f"{self._indicator_engine.timeframe_seconds}s",
                            value=value,
                            ts=tick.ts,
                        )
                    )
                if completed_bar is not None:
                    db.add(
                        PriceBarRow(
                            id=uuid.uuid4(),
                            instrument_id=row_id,
                            timeframe=f"{self._indicator_engine.timeframe_seconds}s",
                            bucket_start=completed_bar.bucket_start,
                            open=completed_bar.open,
                            high=completed_bar.high,
                            low=completed_bar.low,
                            close=completed_bar.close,
                            volume=completed_bar.volume,
                        )
                    )

    def _on_depth(self, depth: DepthSnapshot) -> None:
        ref = self._symbol_map.get(depth.contract_symbol)
        if ref is None or ref[0] != "option_contract":
            # Depth is option-contract-only per the schema (see domain/market/models.py) —
            # an underlying's own order book isn't part of this system's design.
            return
        with self._session_factory() as db:
            db.add(
                DepthSnapshotRow(
                    id=uuid.uuid4(),
                    option_contract_id=ref[1],
                    ts=depth.ts,
                    bid_levels=[
                        {"price": level.price, "qty": level.qty, "orders": level.orders}
                        for level in depth.bid_levels
                    ],
                    ask_levels=[
                        {"price": level.price, "qty": level.qty, "orders": level.orders}
                        for level in depth.ask_levels
                    ],
                )
            )


def _preserve_open_position_pricing(
    db: Session,
    broker: BrokerPort,
    instrument_id: uuid.UUID,
    expiry: date,
    plausible_entries: list[OptionChainEntry],
) -> None:
    """Mutates `plausible_entries` in place, appending one entry per
    genuinely open `Position` on this instrument+expiry whose contract
    symbol didn't already come back in `broker.get_option_chain`'s own
    (now ATM-window-narrowed) result — see `record_option_chain_snapshot`'s
    own docstring for why this matters. A `get_quote` failure for one
    position degrades that one entry (skipped, not persisted) rather than
    the whole snapshot, same discipline as the main per-strike loop this
    mirrors.
    """
    already_present = {e.contract_symbol for e in plausible_entries}
    # Explicit .distinct(), even though this system routinely has more than
    # one open Position on the identical OptionContract (multiple strategies
    # sharing a strike is normal, confirmed live via this exact repo's own
    # trading_sessions today) and the raw JOIN below does produce one SQL row
    # per matching Position. Verified empirically: the legacy `Query.all()`
    # API already collapses those into a single ORM object by primary-key
    # identity even without this call (confirmed via `db.execute(query
    # .statement).fetchall()` returning 2 raw rows while `query.all()`
    # returned 1) -- so this isn't fixing an active bug, but making that
    # collapsing explicit and not dependent on an ORM-API-specific behavior
    # this codebase doesn't rely on anywhere else being asserted.
    query = (
        db.query(OptionContract)
        .join(Position, Position.option_contract_id == OptionContract.id)
        .filter(
            Position.status == PositionStatus.OPEN,
            OptionContract.instrument_id == instrument_id,
            OptionContract.expiry_date == expiry,
        )
        .distinct()
    )
    if already_present:
        query = query.filter(OptionContract.symbol.notin_(already_present))
    open_contracts = query.all()
    for contract in open_contracts:
        try:
            tick = broker.get_quote(contract.symbol)
        except BrokerError:
            logger.warning(
                "Failed to fetch a preservation quote for open position's contract %s "
                "(instrument=%s, expiry=%s); its chain entry stays absent this refresh",
                contract.symbol,
                instrument_id,
                expiry,
            )
            continue
        if not is_plausible_option_tick(tick.ltp, tick.bid, tick.ask, tick.volume):
            logger.error(
                "REJECTED implausible preservation quote for open position's contract %r "
                "(instrument=%s, expiry=%s): ltp=%.4f bid=%.4f ask=%.4f volume=%d",
                contract.symbol,
                instrument_id,
                expiry,
                tick.ltp,
                tick.bid,
                tick.ask,
                tick.volume,
            )
            continue
        plausible_entries.append(
            OptionChainEntry(
                contract_symbol=contract.symbol,
                strike=float(contract.strike),
                # OptionContract.option_type is declared Mapped[OptionType]
                # (app.domain.market.models's own StrEnum) but the underlying
                # column is a plain String(2) with no SQLAlchemy Enum type
                # decorator -- at runtime this attribute is always a bare str
                # ("CE"/"PE"), never an actual enum instance with a `.value`
                # to read. OptionChainEntry needs this module's own OptionType
                # (a separate StrEnum class, same CE/PE values), constructed
                # directly from that string.
                option_type=BrokerOptionType(contract.option_type),
                ltp=tick.ltp,
                bid=tick.bid,
                ask=tick.ask,
                volume=tick.volume,
                oi=tick.oi or 0,
            )
        )


def record_option_chain_snapshot(
    db_underlying_instrument_id: uuid.UUID,
    broker: BrokerPort,
    underlying_symbol: str,
    expiry: date,
    session_factory: SessionFactory = session_scope,
) -> OptionChainSnapshotRow:
    """One-shot fetch + persist — called on a schedule (Scheduler) or on
    demand, not via the streaming path; option chain snapshots are a
    point-in-time picture, not a per-tick stream.

    Per-entry plausibility filtered the same way `_on_tick`'s option branch
    is (see `tick_plausibility.py`) before anything is persisted — this is
    the single write point for every strategy's `rank_from_latest_snapshot`
    entry-price/ranking source and `current_contract_price`'s REST fallback
    (`latest_snapshot_tick`), so filtering here protects both for free
    rather than needing a separate guard in each reader. A rejected entry
    is dropped, not zeroed — `latest_snapshot_tick`/`rank_from_latest_snapshot`
    already treat "contract not in this chain" as a normal gap (an expired/
    rolled-off contract, say), so this degrades the same safe way. Not
    deduped per-symbol like the WS path's warning — chain snapshots refresh
    on a ~25s+ cadence per instrument/expiry, not per tick, so log volume is
    naturally bounded without needing the same cap.

    **2026-09-03**: `broker.get_option_chain` now only REST-prices an
    ATM-centered window (`ShoonyaBrokerAdapter._CHAIN_QUOTE_STRIKE_RADIUS`,
    fixing a real GetQuotes-per-second rate-limit violation — see
    `core.rate_limiter.make_option_chain_quote_limiter`'s own docstring),
    not the wider chain this function used to receive. `current_contract_price`'s
    REST-snapshot fallback (`latest_snapshot_tick`) is the *only* reliable
    price source for an open position's option contract today — per-contract
    WS ticks don't deliver in this deployment (see
    `execution_engine.paper.position_manager`'s own module docstring) — so a
    position whose strike drifts outside the narrower window must still end
    up in this snapshot, or its stop/target/trail pricing would silently
    fall through to `broker.get_quote()` as an absolute last resort, which
    for a paper-routed position prices from the mock's own synthetic,
    strategy-independent seed instead of the real market. Bounded and safe:
    at most one extra `get_quote` call per genuinely open position on this
    exact instrument+expiry, never proportional to chain width.
    """
    chain = broker.get_option_chain(underlying_symbol, expiry)
    plausible_entries = []
    for e in chain.entries:
        if is_plausible_option_tick(e.ltp, e.bid, e.ask, e.volume):
            plausible_entries.append(e)
        else:
            logger.error(
                "REJECTED implausible option-chain entry for %r (underlying=%s, expiry=%s): "
                "ltp=%.4f bid=%.4f ask=%.4f volume=%d -- looks like a leaked underlying/"
                "wrong-instrument value, not a real option premium (see tick_plausibility.py). "
                "Dropping this entry from the snapshot rather than persisting it.",
                e.contract_symbol,
                underlying_symbol,
                expiry,
                e.ltp,
                e.bid,
                e.ask,
                e.volume,
            )
    with session_factory() as db:
        # Only when the broker actually returned *some* real chain data --
        # never for a genuinely empty/dead chain (a broker with no
        # instruments configured, a real outage). An empty chain already
        # correctly classifies as DEAD (`_snapshot_has_live_prices`), which
        # sends every caller straight to `broker.get_quote()` on every
        # cycle -- exactly right for "we have no market data right now."
        # Manufacturing one preserved entry would instead make that chain
        # look LIVE, freezing it at whatever price happened to be fetched
        # this one time until the next 60s refresh -- confirmed live via a
        # real test regression: MockBrokerAdapter-backed tests that
        # intentionally mutate `broker._prices` *after* dispatch, relying on
        # the empty-chain-forces-DEAD-forces-fresh-get_quote path, started
        # reading a stale cached price instead.
        if plausible_entries:
            _preserve_open_position_pricing(
                db, broker, db_underlying_instrument_id, expiry, plausible_entries
            )
        row = OptionChainSnapshotRow(
            id=uuid.uuid4(),
            instrument_id=db_underlying_instrument_id,
            expiry_date=expiry,
            ts=chain.ts,
            chain_data=[
                {
                    "contract_symbol": e.contract_symbol,
                    "strike": e.strike,
                    "option_type": e.option_type.value,
                    "ltp": e.ltp,
                    "bid": e.bid,
                    "ask": e.ask,
                    "volume": e.volume,
                    "oi": e.oi,
                }
                for e in plausible_entries
            ],
        )
        db.add(row)
        db.flush()
        db.refresh(row)
        # session_scope() commits + closes on exit; with the default
        # expire_on_commit=True, the caller touching any attribute afterward
        # would hit a DetachedInstanceError trying to lazily reload from an
        # already-closed session. Expunge so the already-loaded values are
        # kept as-is and no further reload is ever attempted.
        db.expunge(row)
        return row
