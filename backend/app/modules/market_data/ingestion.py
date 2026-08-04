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
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.db.session import session_scope
from app.domain.market.models import DepthSnapshot as DepthSnapshotRow
from app.domain.market.models import IndicatorSnapshot as IndicatorSnapshotRow
from app.domain.market.models import Instrument, OptionContract
from app.domain.market.models import OptionChainSnapshot as OptionChainSnapshotRow
from app.domain.market.models import PriceBar as PriceBarRow
from app.domain.market.models import QuoteTick as QuoteTickRow
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.base.contracts import DepthSnapshot, PriceCandle, Tick
from app.modules.market_data.indicators.engine import IndicatorEngine

logger = logging.getLogger("app.market_data")

# How long to wait after subscribing before deciding WS isn't delivering
# ticks and falling back to REST polling for this symbol — long enough to
# not misfire on a normal reconnect blip (`ShoonyaWSClient`'s own backoff
# caps at 30s between attempts), short enough that a real outage doesn't
# leave price_bars empty for minutes before anyone notices.
_WS_HEALTH_GRACE_SECONDS = 15.0

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

_SymbolRef = tuple[str, uuid.UUID]  # ("instrument" | "option_contract", row id)
SessionFactory = Callable[[], AbstractContextManager[Session]]


class MarketDataIngestionService:
    """`session_factory` defaults to the real app-wide `session_scope` (each
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
        broker: BrokerPort,
        session_factory: SessionFactory = session_scope,
        indicator_engine: IndicatorEngine | None = None,
        *,
        ws_health_grace_seconds: float = _WS_HEALTH_GRACE_SECONDS,
        rest_poll_interval_seconds: float = _REST_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._broker = broker
        self._session_factory = session_factory
        self._indicator_engine = indicator_engine
        self._symbol_map: dict[str, _SymbolRef] = {}

        # WS-health watchdog / REST-polling fallback — see module docstring.
        # `_last_tick_at` is only ever written by `_on_tick`; a symbol
        # missing from it after the grace window means WS genuinely
        # delivered nothing, not just a slow first tick.
        self._ws_health_grace_seconds = ws_health_grace_seconds
        self._rest_poll_interval_seconds = rest_poll_interval_seconds
        self._last_tick_at: dict[str, datetime] = {}
        self._fallback_symbols: set[str] = set()
        self._poll_threads: dict[str, threading.Thread] = {}
        self._last_polled_bucket: dict[str, datetime] = {}

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
        self._broker.subscribe_quotes(
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
            timer = threading.Timer(
                self._ws_health_grace_seconds, self._check_ws_health, args=(symbol,)
            )
            timer.daemon = True
            timer.start()

    def _check_ws_health(self, symbol: str) -> None:
        if symbol in self._fallback_symbols or symbol in self._last_tick_at:
            return  # already on REST, or WS came through in time
        logger.warning(
            "No WS tick received for %r within %.0fs of subscribing — "
            "falling back to REST polling for price_bars",
            symbol,
            self._ws_health_grace_seconds,
        )
        self._start_rest_fallback(symbol)

    def _start_rest_fallback(self, symbol: str) -> None:
        """One-way fallback: once a symbol switches to REST polling it stays
        there for the life of this service instance, even if WS later
        recovers — flip-flopping back would need its own health check on
        the *REST* side too, and this codebase has never once seen WS work
        to know what "recovered" would even look like yet. Explicitly drops
        the WS subscription for this symbol (not just leaving it dangling)
        so a hypothetical later WS recovery can't race a REST-polled insert
        for the same `price_bars` bucket — the same "stopped must mean no
        more callbacks fire" discipline `unsubscribe_quotes` already
        promises elsewhere in this codebase.
        """
        ref = self._symbol_map.get(symbol)
        if ref is None or ref[0] != "instrument":
            return
        instrument_id = ref[1]
        self._fallback_symbols.add(symbol)
        try:
            self._broker.unsubscribe_quotes([symbol])
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

    def _poll_loop(self, symbol: str, instrument_id: uuid.UUID) -> None:
        while symbol in self._fallback_symbols:
            try:
                self._poll_once(symbol, instrument_id)
            except Exception:
                logger.exception("REST poll failed for %r; will retry next cycle", symbol)
            # Checked in short slices, not one long sleep, so `stop()`
            # dropping this symbol from `_fallback_symbols` is noticed
            # promptly rather than up to a full interval late.
            for _ in range(int(self._rest_poll_interval_seconds * 10)):
                if symbol not in self._fallback_symbols:
                    return
                threading.Event().wait(0.1)

    def _poll_once(self, symbol: str, instrument_id: uuid.UUID) -> None:
        timeframe_seconds = (
            self._indicator_engine.timeframe_seconds if self._indicator_engine is not None else 60
        )
        now = datetime.now(UTC)
        candles = self._broker.get_price_history(
            symbol, now - _REST_POLL_LOOKBACK, now, timeframe_seconds
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

        with self._session_factory() as db:
            for candle in new_candles:
                self._persist_candle(db, instrument_id, candle, timeframe_seconds)
        self._last_polled_bucket[symbol] = new_candles[-1].bucket_start

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

    def stop(self, contract_symbols: list[str]) -> None:
        fallback_here = [s for s in contract_symbols if s in self._fallback_symbols]
        still_ws = [s for s in contract_symbols if s not in self._fallback_symbols]
        if still_ws:
            self._broker.unsubscribe_quotes(still_ws)
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

    def _on_tick(self, tick: Tick) -> None:
        ref = self._symbol_map.get(tick.contract_symbol)
        if ref is None:
            return
        # Recorded regardless of what happens below — this is the WS-health
        # signal `_check_ws_health` reads, decoupled from whether the DB
        # write itself succeeds.
        self._last_tick_at[tick.contract_symbol] = tick.ts
        kind, row_id = ref
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
    """
    chain = broker.get_option_chain(underlying_symbol, expiry)
    with session_factory() as db:
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
                for e in chain.entries
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
