"""MarketDataIngestionService writes via a background-thread callback, so it
needs its own session per event — session_factory is injected here to point
at the isolated test database (see conftest.py's `engine` fixture) instead of
the real dev DB the default `session_scope` targets.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.domain.market.mock_universe import build_mock_universe
from app.domain.market.models import (
    DepthSnapshot,
    IndicatorSnapshot,
    Instrument,
    OptionChainSnapshot,
    OptionContract,
    PriceBar,
    QuoteTick,
)
from app.modules.broker_adapter.base.contracts import PriceCandle, Tick
from app.modules.broker_adapter.base.errors import BrokerRateLimitedError
from app.modules.broker_adapter.mock import MockBrokerAdapter
from app.modules.market_data import MarketDataIngestionService, record_option_chain_snapshot
from app.modules.market_data.freshness import FreshnessState, ensure_fresh_option_chain
from app.modules.market_data.indicators import IndicatorEngine
from app.modules.market_data.providers.broker_port_shim import BrokerPortMarketDataAdapter

EXPIRY = date(2026, 7, 31)


def _provider(broker):
    """MarketDataIngestionService now depends on BaseMarketDataProvider, not
    BrokerPort directly (see that class's own updated docstring) — every
    fake broker in this file already has BrokerPort's shape
    (subscribe_quotes/unsubscribe_quotes/get_price_history), so wrapping it
    here is all that's needed; none of the fakes themselves change.
    """
    return BrokerPortMarketDataAdapter(broker)  # type: ignore[arg-type]


@pytest.fixture
def test_session_factory(engine):
    """Mirrors core.db.session.session_scope's contract (commit on success,
    rollback on error, always close) but bound to the isolated test engine
    rather than the app-wide one.
    """
    session_factory = sessionmaker(bind=engine, future=True)

    @contextmanager
    def _scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _scope


@pytest.fixture
def seeded_universe(db: Session, test_session_factory):
    """Seeds instruments/option_contracts via the *same* engine the ingestion
    service's injected session_factory will read from — using the `db`
    fixture's own (uncommitted, per-test) transaction wouldn't be visible to
    a separate session on the same engine, so this seeds+commits directly.

    Because this bypasses the `db` fixture's rollback-based isolation (real
    commits are the whole point — they simulate the background-thread writes
    under test), it must clean up explicitly at teardown or seeded rows leak
    into the next test and collide on the unique (symbol, exchange) constraint.
    """
    universe = build_mock_universe(EXPIRY)
    with test_session_factory() as seed_db:
        instrument_ids: dict[str, uuid.UUID] = {}
        for info in universe:
            if info.is_option:
                continue
            instrument = Instrument(
                id=uuid.uuid4(),
                symbol=info.symbol,
                exchange=info.exchange,
                lot_size=info.lot_size,
                tick_size=info.tick_size,
            )
            seed_db.add(instrument)
            seed_db.flush()
            instrument_ids[info.symbol] = instrument.id

        for info in universe:
            if not info.is_option:
                continue
            assert info.underlying is not None
            assert info.option_type is not None
            seed_db.add(
                OptionContract(
                    id=uuid.uuid4(),
                    instrument_id=instrument_ids[info.underlying],
                    expiry_date=info.expiry,
                    strike=info.strike,
                    option_type=info.option_type.value,
                    symbol=info.symbol,
                    broker_token="",
                )
            )

    yield universe

    with test_session_factory() as cleanup_db:
        cleanup_db.query(QuoteTick).delete()
        cleanup_db.query(DepthSnapshot).delete()
        cleanup_db.query(OptionChainSnapshot).delete()
        cleanup_db.query(IndicatorSnapshot).delete()
        cleanup_db.query(PriceBar).delete()
        cleanup_db.query(OptionContract).delete()
        cleanup_db.query(Instrument).delete()


def test_symbol_map_resolves_both_instruments_and_option_contracts(
    seeded_universe, test_session_factory
):
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=1)
    service = MarketDataIngestionService(_provider(broker), session_factory=test_session_factory)

    symbols = [i.symbol for i in seeded_universe[:5]]
    symbol_map = service._build_symbol_map(symbols)  # noqa: SLF001

    assert set(symbol_map.keys()) == set(symbols)


def test_unknown_symbol_does_not_raise(seeded_universe, test_session_factory, caplog):
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=1)
    service = MarketDataIngestionService(_provider(broker), session_factory=test_session_factory)

    symbol_map = service._build_symbol_map(["NOT-A-REAL-SYMBOL"])  # noqa: SLF001
    assert symbol_map == {}


def test_streamed_ticks_are_persisted_for_option_contract(
    seeded_universe, test_session_factory, db: Session
):
    option_symbol = next(i.symbol for i in seeded_universe if i.is_option)
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=2, tick_interval_seconds=0.1)
    service = MarketDataIngestionService(_provider(broker), session_factory=test_session_factory)

    service.start([option_symbol])
    time.sleep(0.35)
    service.stop([option_symbol])

    ticks = db.query(QuoteTick).all()
    assert len(ticks) >= 2
    assert all(t.option_contract_id is not None for t in ticks)
    assert all(t.instrument_id is None for t in ticks)


def test_streamed_ticks_are_persisted_for_underlying_instrument(
    seeded_universe, test_session_factory, db: Session
):
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=3, tick_interval_seconds=0.1)
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        # MockBrokerAdapter's crc32-seeded price for "NIFTY" is in the 50-250
        # range (a deliberately synthetic scale) -- not what the 2026-08-20
        # plausibility guard exists to test.
        min_plausible_price_by_symbol={},
    )

    service.start([underlying_symbol])
    time.sleep(0.35)
    service.stop([underlying_symbol])

    ticks = db.query(QuoteTick).all()
    assert len(ticks) >= 2
    assert all(t.instrument_id is not None for t in ticks)
    assert all(t.option_contract_id is None for t in ticks)


def test_indicator_engine_persists_vwap_immediately_and_ema_after_warmup(
    seeded_universe, test_session_factory, db: Session
):
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    nifty = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one()

    broker = MockBrokerAdapter(instruments=seeded_universe, seed=6)
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        indicator_engine=IndicatorEngine(timeframe_seconds=60),
    )
    service._symbol_map = service._build_symbol_map([underlying_symbol])  # noqa: SLF001

    def snapshot_names() -> set[str]:
        rows = db.query(IndicatorSnapshot).filter(IndicatorSnapshot.instrument_id == nifty.id).all()
        return {row.indicator_name for row in rows}

    base_ts = broker.get_quote(underlying_symbol).ts
    # Realistic index-scale prices -- _MIN_PLAUSIBLE_PRICE_BY_SYMBOL (2026-08-20)
    # would otherwise reject a synthetic sub-5000 "NIFTY" tick as implausible.
    first_tick = Tick(
        underlying_symbol, ltp=24100.0, bid=24099.9, ask=24100.1, volume=10, oi=None, ts=base_ts
    )
    # First tick: VWAP should persist immediately (one sample is enough),
    # EMA9/EMA20 need 9 completed 60s bars, so neither should exist yet.
    service._on_tick(first_tick)  # noqa: SLF001
    assert snapshot_names() == {"VWAP"}

    # 9 more ticks, each in a new 60s bucket, completes 9 bars -> EMA9 warms up.
    for i in range(1, 10):
        tick_ts = base_ts + timedelta(seconds=60 * i)
        service._on_tick(  # noqa: SLF001
            Tick(underlying_symbol, ltp=24100.0 + i, bid=0, ask=0, volume=10, oi=None, ts=tick_ts)
        )

    names_after_warmup = snapshot_names()
    assert "EMA9" in names_after_warmup
    assert "VWAP" in names_after_warmup
    # EMA20 shouldn't exist yet — only 9 bars have completed.
    assert "EMA20" not in names_after_warmup


def test_start_warm_starts_ema_atr_from_pre_existing_price_bars(
    seeded_universe, test_session_factory, db: Session
):
    """The actual fix under test: a *fresh* service (standing in for a
    process having just restarted) must warm EMA9/EMA20/ATR14 together from
    whatever `price_bars` already exist for this instrument, before any live
    tick ever arrives — closing the real, live-confirmed gap recorded in
    `IndicatorEngine.warm_start`'s own docstring (EMA9 re-arming ~11 minutes
    before EMA20 after a cold restart, which fed
    `EMAMicroPullbackStrategy`'s expansion filter a stale/fresh mismatched
    pair and produced a real losing trade on 2026-08-26).
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    nifty = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one()

    base = datetime(2026, 7, 24, 9, 15, 0, tzinfo=UTC)
    with test_session_factory() as seed_db:
        for i in range(25):
            seed_db.add(
                PriceBar(
                    id=uuid.uuid4(),
                    instrument_id=nifty.id,
                    timeframe="60s",
                    bucket_start=base + timedelta(minutes=i),
                    open=24100.0,
                    high=24101.0,
                    low=24099.0,
                    close=24100.0 + i,
                    volume=0,
                )
            )

    broker = _NeverTicksBroker()  # deterministic: this test cares only about
    # start()'s own synchronous warm-start effect, not real streaming.
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        indicator_engine=IndicatorEngine(timeframe_seconds=60),
    )

    service.start([underlying_symbol])

    engine = service._indicator_engine  # noqa: SLF001
    assert engine is not None
    assert engine._ema9[nifty.id].is_warmed_up  # noqa: SLF001
    assert engine._ema20[nifty.id].is_warmed_up  # noqa: SLF001
    assert engine._atr[nifty.id].is_warmed_up  # noqa: SLF001


def test_start_warm_start_does_not_touch_the_database(
    seeded_universe, test_session_factory, db: Session
):
    """`warm_start` is in-memory-only by design (see its own docstring) —
    replaying already-persisted bars must never write new
    `indicator_snapshots` rows or re-insert `price_bars` rows (which would
    hit `uq_price_bar_bucket` and crash startup, the exact regression risk
    this fix's own design review flagged).
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    nifty = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one()

    base = datetime(2026, 7, 24, 9, 15, 0, tzinfo=UTC)
    with test_session_factory() as seed_db:
        for i in range(25):
            seed_db.add(
                PriceBar(
                    id=uuid.uuid4(),
                    instrument_id=nifty.id,
                    timeframe="60s",
                    bucket_start=base + timedelta(minutes=i),
                    open=24100.0,
                    high=24101.0,
                    low=24099.0,
                    close=24100.0 + i,
                    volume=0,
                )
            )

    broker = _NeverTicksBroker()
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        indicator_engine=IndicatorEngine(timeframe_seconds=60),
    )

    service.start([underlying_symbol])

    assert (
        db.query(IndicatorSnapshot).filter(IndicatorSnapshot.instrument_id == nifty.id).count()
        == 0
    )
    assert db.query(PriceBar).filter(PriceBar.instrument_id == nifty.id).count() == 25


def test_start_without_pre_existing_price_bars_leaves_indicators_cold(
    seeded_universe, test_session_factory, db: Session
):
    """No `price_bars` yet for this instrument (a genuinely new one, or the
    very first restart of the day before any bar has completed) — `start`
    must behave exactly as it did before this fix existed: indicators warm
    up from live ticks only, from zero.
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    nifty = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one()

    broker = _NeverTicksBroker()
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        indicator_engine=IndicatorEngine(timeframe_seconds=60),
    )

    service.start([underlying_symbol])

    engine = service._indicator_engine  # noqa: SLF001
    assert engine is not None
    assert nifty.id not in engine._ema9  # noqa: SLF001
    assert nifty.id not in engine._ema20  # noqa: SLF001


def test_start_without_an_indicator_engine_skips_warm_start(
    seeded_universe, test_session_factory
):
    """`indicator_engine=None` (e.g. a test double, or a future caller with
    no indicator use at all) must not crash `start` just because warm-start
    has nothing to warm.
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    broker = _NeverTicksBroker()
    service = MarketDataIngestionService(_provider(broker), session_factory=test_session_factory)

    service.start([underlying_symbol])  # must not raise


def test_reset_daily_indicators_resets_the_underlying_engine(
    seeded_universe, test_session_factory
):
    """The daily VWAP-reset call `MarketDataScheduler`'s PRE_MARKET
    transition makes — proves it reaches the real `IndicatorEngine`, not
    just that `IndicatorEngine.reset_session` itself works in isolation
    (already covered in test_indicators.py).
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=6)
    engine = IndicatorEngine(timeframe_seconds=60)
    service = MarketDataIngestionService(
        _provider(broker), session_factory=test_session_factory, indicator_engine=engine
    )
    service._symbol_map = service._build_symbol_map([underlying_symbol])  # noqa: SLF001
    instrument_id = next(
        row_id for kind, row_id in service._symbol_map.values() if kind == "instrument"  # noqa: SLF001
    )

    base_ts = broker.get_quote(underlying_symbol).ts
    service._on_tick(  # noqa: SLF001
        Tick(
            underlying_symbol, ltp=24100.0, bid=24099.9, ask=24100.1, volume=10, oi=None, ts=base_ts
        )
    )
    assert engine._vwap[instrument_id].value is not None  # noqa: SLF001

    service.reset_daily_indicators()

    assert engine._vwap[instrument_id].value is None  # noqa: SLF001


def test_reset_daily_indicators_is_a_harmless_noop_without_an_indicator_engine(
    seeded_universe, test_session_factory
):
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=6)
    service = MarketDataIngestionService(_provider(broker), session_factory=test_session_factory)

    service.reset_daily_indicators()  # must not raise


def test_completed_bars_are_persisted_for_underlying_instrument(
    seeded_universe, test_session_factory, db: Session
):
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    nifty = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one()

    broker = MockBrokerAdapter(instruments=seeded_universe, seed=7)
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        indicator_engine=IndicatorEngine(timeframe_seconds=60),
    )
    service._symbol_map = service._build_symbol_map([underlying_symbol])  # noqa: SLF001

    base_ts = broker.get_quote(underlying_symbol).ts
    service._on_tick(  # noqa: SLF001
        Tick(underlying_symbol, ltp=24100.0, bid=0, ask=0, volume=10, oi=None, ts=base_ts)
    )
    assert db.query(PriceBar).count() == 0  # no bucket boundary crossed yet

    service._on_tick(  # noqa: SLF001
        Tick(
            underlying_symbol,
            ltp=24105.0,
            bid=0,
            ask=0,
            volume=5,
            oi=None,
            ts=base_ts + timedelta(seconds=60),
        )
    )

    bars = db.query(PriceBar).filter(PriceBar.instrument_id == nifty.id).all()
    assert len(bars) == 1
    assert float(bars[0].open) == 24100.0
    assert float(bars[0].close) == 24100.0
    assert bars[0].timeframe == "60s"


# -- price-plausibility guard (2026-08-20) ----------------------------------


def test_on_tick_rejects_an_implausible_price_for_a_known_underlying(
    seeded_universe, test_session_factory, db: Session
):
    """See ingestion.py's own _MIN_PLAUSIBLE_PRICE_BY_SYMBOL docstring for
    the live incident this guards against: a known underlying (NIFTY/
    BANKNIFTY) receiving a real but wrongly-attributed instrument's price
    (option-premium scale, not index scale) must never reach quote_ticks/
    price_bars/indicator_snapshots."""
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    nifty = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one()
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=1)
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        indicator_engine=IndicatorEngine(timeframe_seconds=60),
    )
    service._symbol_map = service._build_symbol_map([underlying_symbol])  # noqa: SLF001

    tick_ts = broker.get_quote(underlying_symbol).ts
    service._on_tick(  # noqa: SLF001
        Tick(underlying_symbol, ltp=124.5, bid=124.4, ask=124.6, volume=100, oi=None, ts=tick_ts)
    )

    assert db.query(QuoteTick).filter(QuoteTick.instrument_id == nifty.id).count() == 0
    assert (
        db.query(IndicatorSnapshot).filter(IndicatorSnapshot.instrument_id == nifty.id).count()
        == 0
    )
    assert underlying_symbol not in service._last_tick_at  # noqa: SLF001 -- must not look healthy


def test_on_tick_accepts_a_realistic_price_for_a_known_underlying(
    seeded_universe, test_session_factory, db: Session
):
    """Sanity check the other direction -- the guard is a floor, not a
    blanket block on the symbol."""
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    nifty = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one()
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=1)
    service = MarketDataIngestionService(_provider(broker), session_factory=test_session_factory)
    service._symbol_map = service._build_symbol_map([underlying_symbol])  # noqa: SLF001

    tick_ts = broker.get_quote(underlying_symbol).ts
    service._on_tick(  # noqa: SLF001
        Tick(
            underlying_symbol,
            ltp=24150.0,
            bid=24149.9,
            ask=24150.1,
            volume=100,
            oi=None,
            ts=tick_ts,
        )
    )

    assert db.query(QuoteTick).filter(QuoteTick.instrument_id == nifty.id).count() == 1
    assert underlying_symbol in service._last_tick_at  # noqa: SLF001


def test_on_tick_never_rejects_an_option_contract_regardless_of_price(
    seeded_universe, test_session_factory, db: Session
):
    """The plausibility floor is scoped to known underlyings only -- an
    option contract's own (deliberately much lower) premium must never be
    checked against an index-scale floor."""
    option_symbol = next(i.symbol for i in seeded_universe if i.is_option)
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=1)
    service = MarketDataIngestionService(_provider(broker), session_factory=test_session_factory)
    service._symbol_map = service._build_symbol_map([option_symbol])  # noqa: SLF001

    tick_ts = broker.get_quote(option_symbol).ts
    service._on_tick(  # noqa: SLF001
        Tick(option_symbol, ltp=12.5, bid=12.4, ask=12.6, volume=100, oi=None, ts=tick_ts)
    )

    assert db.query(QuoteTick).filter(QuoteTick.option_contract_id.isnot(None)).count() == 1


def test_poll_once_rejects_implausible_candles_without_advancing_last_polled_bucket(
    seeded_universe, test_session_factory, db: Session
):
    """get_price_history routes through the exact same underlying-token
    resolution the WS path does -- a rejected candle must not silently
    "fix itself" by falling back to REST, since REST would carry the same
    corruption. _last_polled_bucket staying unadvanced means a later,
    genuinely-fixed poll can still make forward progress."""
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    nifty = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one()
    now = datetime.now(UTC)
    bad_bucket = now - timedelta(seconds=now.timestamp() % 60 + 60)
    candles = [
        PriceCandle(
            bucket_start=bad_bucket, open=124.0, high=125.0, low=123.0, close=124.5, volume=0
        )
    ]
    broker = _NeverTicksBroker(candles_by_symbol={underlying_symbol: candles})
    service = MarketDataIngestionService(_provider(broker), session_factory=test_session_factory)
    service._symbol_map = service._build_symbol_map([underlying_symbol])  # noqa: SLF001

    service._poll_once(underlying_symbol, nifty.id)  # noqa: SLF001

    assert db.query(PriceBar).filter(PriceBar.instrument_id == nifty.id).count() == 0
    assert service._last_polled_bucket.get(underlying_symbol) is None  # noqa: SLF001


class _NeverTicksBroker:
    """Models the real, live case this feature was built for: Shoonya's WS
    auth consistently returns `NOT_OK`, so `subscribe_quotes` "succeeds"
    (registers the callback) but never actually calls it. Tracks
    subscribe/unsubscribe calls and serves canned candles per symbol so
    REST-fallback behavior can be tested deterministically, without relying
    on `MockBrokerAdapter`'s own timing-based streaming.
    """

    def __init__(self, candles_by_symbol: dict[str, list[PriceCandle]] | None = None) -> None:
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.price_history_calls: list[tuple] = []
        self._candles_by_symbol = candles_by_symbol or {}
        # Settable per test: raised from get_price_history instead of
        # returning canned candles, when set.
        self.get_price_history_error: Exception | None = None

    def subscribe_quotes(self, contract_symbols, on_tick, on_depth=None) -> None:
        self.subscribed.extend(contract_symbols)

    def unsubscribe_quotes(self, contract_symbols) -> None:
        self.unsubscribed.extend(contract_symbols)

    def get_price_history(
        self, underlying: str, start: datetime, end: datetime, timeframe_seconds: int = 60
    ) -> list[PriceCandle]:
        self.price_history_calls.append((underlying, start, end, timeframe_seconds))
        if self.get_price_history_error is not None:
            raise self.get_price_history_error
        return self._candles_by_symbol.get(underlying, [])

    def __getattr__(self, name):
        raise AttributeError(name)


def test_ws_health_watchdog_falls_back_to_rest_polling_when_no_tick_arrives(
    seeded_universe, test_session_factory
):
    """The real, live case: WS "subscribes" successfully but never delivers
    a single tick. Falling back is what actually keeps `price_bars` from
    staying empty forever in that case.
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    broker = _NeverTicksBroker()
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        ws_health_grace_seconds=0.05,
        rest_poll_interval_seconds=1000.0,  # won't fire again during this test
    )

    service.start([underlying_symbol])
    time.sleep(0.2)

    assert underlying_symbol in service._fallback_symbols  # noqa: SLF001
    assert underlying_symbol in broker.unsubscribed  # dual-write race avoided
    service.stop([underlying_symbol])


def test_ws_health_watchdog_uses_a_wider_per_symbol_grace_override(
    seeded_universe, test_session_factory
):
    """2026-08-19: India VIX updates far less often than NIFTY/BANKNIFTY
    (live-observed as low as ~2 ticks/60s) -- a flat grace window would
    keep it perpetually flagged unhealthy. `ws_health_grace_seconds_by_symbol`
    lets one symbol get a wider window than the service's own default,
    verified end-to-end here rather than just unit-testing `_grace_seconds_for`
    in isolation, since the real bug this protects against is in the
    threading.Timer wiring inside `start()`, not the lookup itself.
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    with test_session_factory() as seed_db:
        seed_db.add(
            Instrument(
                id=uuid.uuid4(),
                symbol="INDIA VIX",
                exchange="NSE",
                lot_size=1,
                tick_size=0.05,
            )
        )

    broker = _NeverTicksBroker()
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        ws_health_grace_seconds=0.05,
        ws_health_grace_seconds_by_symbol={"INDIA VIX": 0.3},
        rest_poll_interval_seconds=1000.0,
    )

    service.start([underlying_symbol, "INDIA VIX"])
    time.sleep(0.15)
    assert underlying_symbol in service.fallback_symbols  # default grace already elapsed
    assert "INDIA VIX" not in service.fallback_symbols  # wider override not elapsed yet

    time.sleep(0.25)
    assert "INDIA VIX" in service.fallback_symbols  # now elapsed too

    service.stop([underlying_symbol, "INDIA VIX"])

    with test_session_factory() as cleanup_db:
        cleanup_db.query(Instrument).filter(Instrument.symbol == "INDIA VIX").delete()


def test_ws_health_watchdog_does_not_fall_back_if_a_tick_arrives_in_time(
    seeded_universe, test_session_factory
):
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=1, tick_interval_seconds=0.02)
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        ws_health_grace_seconds=0.2,
        # MockBrokerAdapter's crc32-seeded price for "NIFTY" is in the 50-250
        # range (a deliberately synthetic scale, unrelated to real index
        # levels) -- this test is about WS-health timing, not price content,
        # so the 2026-08-20 plausibility guard is explicitly not relevant here.
        min_plausible_price_by_symbol={},
    )

    service.start([underlying_symbol])
    time.sleep(0.3)

    assert underlying_symbol not in service._fallback_symbols  # noqa: SLF001
    service.stop([underlying_symbol])


class _RecoversOnSecondSubscribeBroker:
    """Never ticks on the first `subscribe_quotes` call (the original
    fallback trigger, same as `_NeverTicksBroker`), but fires one real
    tick immediately and synchronously on any *later* subscribe call —
    models a real WS recovery for `_try_ws_recovery`'s own probe, which
    re-subscribes to test exactly this.
    """

    def __init__(self) -> None:
        self.subscribe_calls: list[list[str]] = []
        self.unsubscribed: list[str] = []

    def subscribe_quotes(self, contract_symbols, on_tick, on_depth=None) -> None:
        self.subscribe_calls.append(list(contract_symbols))
        if len(self.subscribe_calls) >= 2:
            for symbol in contract_symbols:
                on_tick(
                    Tick(
                        contract_symbol=symbol,
                        ltp=24000.0,
                        bid=23999.5,
                        ask=24000.5,
                        volume=1,
                        oi=None,
                        ts=datetime.now(UTC),
                    )
                )

    def unsubscribe_quotes(self, contract_symbols) -> None:
        self.unsubscribed.extend(contract_symbols)

    def get_price_history(
        self, underlying: str, start: datetime, end: datetime, timeframe_seconds: int = 60
    ) -> list[PriceCandle]:
        return []

    def __getattr__(self, name):
        raise AttributeError(name)


def test_ws_recovery_probe_promotes_symbol_back_from_rest_polling(
    seeded_universe, test_session_factory
):
    """2026-08-20: `_start_rest_fallback` used to be a permanent, one-way
    trip -- this proves the new periodic probe actually promotes a symbol
    back to WS-driven ticks once it starts delivering again, restoring
    VWAP (frozen the whole time a symbol stayed on REST -- see
    `IndicatorEngine.on_completed_bar`'s own docstring for why).
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    broker = _RecoversOnSecondSubscribeBroker()
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        ws_health_grace_seconds=0.2,
        min_plausible_price_by_symbol={},
    )
    # start() does two things a bare _fallback_symbols seed can't: builds
    # _symbol_map (_on_tick silently drops any tick for an unknown symbol)
    # and makes the real subscribe call #1 the fake's "only recovers from
    # its second call onward" behavior depends on. Its own watchdog timer
    # is harmless here -- manually seeding _fallback_symbols pre-empts it,
    # and _check_ws_health's own guard makes a later no-op fire safe.
    service.start([underlying_symbol])
    service._fallback_symbols.add(underlying_symbol)  # noqa: SLF001

    recovered = service._try_ws_recovery(underlying_symbol)  # noqa: SLF001

    assert recovered is True
    assert underlying_symbol not in service.fallback_symbols
    assert len(broker.subscribe_calls) == 2  # the original subscribe + the probe's own


def test_ws_recovery_probe_stays_on_rest_when_ws_still_silent(
    seeded_universe, test_session_factory
):
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    broker = _NeverTicksBroker()
    service = MarketDataIngestionService(
        _provider(broker), session_factory=test_session_factory, ws_health_grace_seconds=0.05
    )
    service._fallback_symbols.add(underlying_symbol)  # noqa: SLF001

    recovered = service._try_ws_recovery(underlying_symbol)  # noqa: SLF001

    assert recovered is False
    assert underlying_symbol in service.fallback_symbols
    # Re-torn-down after the failed probe -- same "stopped must mean no
    # more callbacks fire" discipline the original fallback decision uses.
    assert underlying_symbol in broker.unsubscribed


def test_poll_loop_promotes_back_to_ws_via_probe_end_to_end(
    seeded_universe, test_session_factory
):
    """End-to-end through the real `_poll_loop` background thread (not
    calling `_try_ws_recovery` directly) -- proves the probe cadence is
    actually wired into the loop, not just correct in isolation.
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    broker = _RecoversOnSecondSubscribeBroker()
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        ws_health_grace_seconds=0.05,
        rest_poll_interval_seconds=0.05,
        ws_recovery_probe_every_n_polls=1,  # probe every cycle for a fast test
        min_plausible_price_by_symbol={},
    )

    service.start([underlying_symbol])
    # With a 0.05s grace window and a 0.05s poll interval (probing every
    # cycle), fallback and recovery both happen well within this single
    # wait -- there's no stable intermediate moment worth asserting on
    # separately without flaking on timing.
    time.sleep(0.5)

    assert underlying_symbol not in service.fallback_symbols
    service.stop([underlying_symbol])


def test_fallback_symbols_property_reflects_internal_state(
    seeded_universe, test_session_factory
):
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    broker = _NeverTicksBroker()
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        ws_health_grace_seconds=0.05,
        rest_poll_interval_seconds=1000.0,
    )
    assert service.fallback_symbols == frozenset()

    service.start([underlying_symbol])
    time.sleep(0.2)

    assert service.fallback_symbols == frozenset({underlying_symbol})
    service.stop([underlying_symbol])


def test_stale_last_tick_at_suppresses_the_watchdog_until_forget_symbol_clears_it(
    seeded_universe, test_session_factory
):
    """2026-08-19 regression: reproduces the actual mechanism behind a real
    live incident -- a symbol subscribed on a now-dead connection
    ("yesterday") has a stale `_last_tick_at` entry. Calling `start()`
    again for it (as `ensure_ingestion_running` does once
    `registry._subscribed_symbols` is cleared for a new day) must not
    silently skip re-arming the WS-health watchdog just because a tick was
    seen at some point in the past -- `forget_symbol` is what makes a
    fresh `start()` call genuinely fresh.
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    broker = _NeverTicksBroker()
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        ws_health_grace_seconds=0.05,
        rest_poll_interval_seconds=1000.0,
    )
    service._last_tick_at[underlying_symbol] = datetime.now(UTC)  # noqa: SLF001 -- simulates yesterday's stale entry

    service.start([underlying_symbol])
    time.sleep(0.2)
    # watchdog suppressed by the stale entry
    assert underlying_symbol not in service.fallback_symbols

    service.forget_symbol(underlying_symbol)
    service.start([underlying_symbol])
    time.sleep(0.2)
    assert underlying_symbol in service.fallback_symbols  # watchdog now genuinely re-armed
    service.stop([underlying_symbol])


def test_rest_fallback_persists_a_completed_candle_and_skips_the_still_forming_one(
    seeded_universe, test_session_factory, db: Session
):
    """A broker's own "latest" candle can be the one still forming — only a
    bucket whose window has fully closed is safe to treat as a real bar.
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    nifty = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one()

    now = datetime.now(UTC)
    current_bucket = now - timedelta(seconds=now.timestamp() % 60)
    completed_bucket = current_bucket - timedelta(seconds=60)
    candles = [
        PriceCandle(
            bucket_start=completed_bucket,
            open=24100.0,
            high=24101.0,
            low=24099.0,
            close=24100.5,
            volume=0,
        ),
        PriceCandle(  # still forming — must not be persisted
            bucket_start=current_bucket,
            open=24100.5,
            high=24100.6,
            low=24100.4,
            close=24100.5,
            volume=0,
        ),
    ]
    broker = _NeverTicksBroker(candles_by_symbol={underlying_symbol: candles})
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        indicator_engine=IndicatorEngine(timeframe_seconds=60),
        ws_health_grace_seconds=0.02,
        rest_poll_interval_seconds=0.05,
    )

    service.start([underlying_symbol])
    time.sleep(0.3)
    service.stop([underlying_symbol])

    bars = db.query(PriceBar).filter(PriceBar.instrument_id == nifty.id).all()
    assert len(bars) == 1
    assert bars[0].bucket_start == completed_bucket
    assert float(bars[0].close) == 24100.5
    ticks = db.query(QuoteTick).filter(QuoteTick.instrument_id == nifty.id).all()
    assert len(ticks) == 1
    snapshot_names = {
        row.indicator_name
        for row in db.query(IndicatorSnapshot).filter(IndicatorSnapshot.instrument_id == nifty.id)
    }
    assert "VWAP" not in snapshot_names  # never fed on this path — see on_completed_bar


def test_rest_fallback_does_not_repersist_a_bucket_already_seen(
    seeded_universe, test_session_factory, db: Session
):
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    now = datetime.now(UTC)
    completed_bucket = now - timedelta(seconds=now.timestamp() % 60 + 60)
    candles = [
        PriceCandle(
            bucket_start=completed_bucket,
            open=24100.0,
            high=24101.0,
            low=24099.0,
            close=24100.5,
            volume=0,
        )
    ]
    broker = _NeverTicksBroker(candles_by_symbol={underlying_symbol: candles})
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        ws_health_grace_seconds=0.02,
        rest_poll_interval_seconds=0.05,
    )

    service.start([underlying_symbol])
    time.sleep(0.3)  # several poll cycles, identical candle list every time
    service.stop([underlying_symbol])

    nifty = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one()
    assert db.query(PriceBar).filter(PriceBar.instrument_id == nifty.id).count() == 1


def test_rest_fallback_seeds_last_polled_bucket_from_db_across_a_restart(
    seeded_universe, test_session_factory, db: Session
):
    """`_last_polled_bucket` is in-memory only and forgets everything across
    a restart — a *fresh* MarketDataIngestionService instance (standing in
    for the process having been restarted) must not re-attempt a bucket a
    *previous* instance already persisted, and must still make forward
    progress on a genuinely newer one in the same poll. Without seeding from
    the DB, this reproduces the exact live failure from 2026-08-05: a
    uq_price_bar_bucket UniqueViolation that rolls back the whole batch and
    never advances, repeating forever.
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    nifty = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one()

    now = datetime.now(UTC)
    already_persisted_bucket = now - timedelta(seconds=now.timestamp() % 60 + 120)
    newer_bucket = already_persisted_bucket + timedelta(seconds=60)

    # Stands in for "a previous service instance already wrote this bar
    # before the process restarted" — must be a *real*, cross-connection-
    # visible commit via test_session_factory, not the db fixture (whose
    # transaction is never truly committed, only rolled back at teardown —
    # see seeded_universe's own docstring for the same lesson). Using db
    # here would leave the row invisible to the service's own connection,
    # which would then treat the bucket as new, attempt to insert it too,
    # and deadlock waiting on this test's still-open transaction to resolve
    # a unique-constraint ambiguity it can't determine from MVCC alone.
    with test_session_factory() as seed_db:
        seed_db.add(
            PriceBar(
                id=uuid.uuid4(),
                instrument_id=nifty.id,
                timeframe="60s",
                bucket_start=already_persisted_bucket,
                open=24100.0,
                high=24101.0,
                low=24099.0,
                close=24100.5,
                volume=0,
            )
        )

    candles = [
        PriceCandle(  # already in the DB — must be skipped, not re-inserted
            bucket_start=already_persisted_bucket,
            open=24100.0,
            high=24101.0,
            low=24099.0,
            close=24100.5,
            volume=0,
        ),
        PriceCandle(  # genuinely new — must still be persisted this cycle
            bucket_start=newer_bucket,
            open=24100.5,
            high=24100.7,
            low=24100.4,
            close=24100.6,
            volume=0,
        ),
    ]
    broker = _NeverTicksBroker(candles_by_symbol={underlying_symbol: candles})
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        ws_health_grace_seconds=0.02,
        rest_poll_interval_seconds=0.05,
    )

    service.start([underlying_symbol])
    time.sleep(0.3)
    service.stop([underlying_symbol])

    bars = (
        db.query(PriceBar)
        .filter(PriceBar.instrument_id == nifty.id)
        .order_by(PriceBar.bucket_start)
        .all()
    )
    assert [b.bucket_start for b in bars] == [already_persisted_bucket, newer_bucket]


def test_rest_fallback_backs_off_much_longer_on_a_rate_limit_error(
    seeded_universe, test_session_factory
):
    """Live-confirmed 2026-08-06: continuing to poll at the normal fast
    cadence after a rate-limit rejection just keeps hammering an
    already-limited endpoint. A short rest_poll_interval_seconds with a
    slightly-longer-but-still-test-fast rate_limit_backoff_seconds proves
    the dedicated backoff actually takes effect — without it, the poll
    count over this window would be far higher (one call roughly every
    rest_poll_interval_seconds instead of one every
    rate_limit_backoff_seconds).
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    broker = _NeverTicksBroker()
    broker.get_price_history_error = BrokerRateLimitedError(
        "Angel One getCandleData rate-limited (HTTP 403)"
    )
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        ws_health_grace_seconds=0.02,
        rest_poll_interval_seconds=0.02,
        rate_limit_backoff_seconds=0.3,
    )

    service.start([underlying_symbol])
    time.sleep(0.5)
    service.stop([underlying_symbol])

    # Without the dedicated backoff, ~0.5s / 0.02s interval would be ~25
    # calls; with it, at most 2-3 (one immediately on fallback, one more if
    # the 0.3s backoff elapses within the 0.5s window).
    assert 1 <= len(broker.price_history_calls) <= 3


def test_stop_actually_joins_the_rest_poll_thread(seeded_universe, test_session_factory):
    """Same "stopped must mean no more callbacks fire, not just asked to
    stop" discipline `ShoonyaWSClient.unsubscribe_quotes` already promises
    elsewhere in this codebase — a caller tearing down its session right
    after `stop()` returns must not race an in-flight poll.
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    broker = _NeverTicksBroker()
    service = MarketDataIngestionService(
        _provider(broker),
        session_factory=test_session_factory,
        ws_health_grace_seconds=0.02,
        rest_poll_interval_seconds=0.05,
    )

    service.start([underlying_symbol])
    time.sleep(0.15)
    service.stop([underlying_symbol])
    calls_at_stop = len(broker.price_history_calls)
    time.sleep(0.3)

    assert len(broker.price_history_calls) == calls_at_stop


def test_depth_only_persisted_for_option_contracts_not_underlying(
    seeded_universe, test_session_factory, db: Session
):
    # Force on_depth to fire deterministically rather than relying on the
    # mock adapter's ~30% random chance per tick.
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=4, tick_interval_seconds=0.05)
    service = MarketDataIngestionService(_provider(broker), session_factory=test_session_factory)
    service._symbol_map = service._build_symbol_map([underlying_symbol])  # noqa: SLF001

    from app.modules.broker_adapter.base.contracts import DepthLevel
    from app.modules.broker_adapter.base.contracts import DepthSnapshot as DepthDTO

    fake_depth = DepthDTO(
        contract_symbol=underlying_symbol,
        bid_levels=(DepthLevel(price=100.0, qty=10),),
        ask_levels=(DepthLevel(price=101.0, qty=10),),
        ts=broker._make_tick(underlying_symbol, step=False).ts,  # noqa: SLF001
    )
    service._on_depth(fake_depth)  # noqa: SLF001

    assert db.query(DepthSnapshot).count() == 0


def test_option_chain_snapshot_is_persisted_and_usable_after_session_close(
    seeded_universe, test_session_factory, db: Session
):
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=5)
    nifty = db.query(Instrument).filter(Instrument.symbol == "NIFTY").one()

    row = record_option_chain_snapshot(
        nifty.id, broker, "NIFTY", EXPIRY, session_factory=test_session_factory
    )

    # Must not raise DetachedInstanceError — this is exactly the bug the
    # explicit db.expunge() in record_option_chain_snapshot fixes.
    assert row.instrument_id == nifty.id
    assert len(row.chain_data) == 42  # 21 strikes * 2 (CE/PE) for NIFTY


def test_ensure_fresh_option_chain_refreshes_when_none_exists(
    seeded_universe, test_session_factory, db: Session
):
    """The gap this function exists to close: a strategy run that never
    refreshes its chain snapshot after start_strategy's one-shot call.
    """
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=7)
    nifty = db.query(Instrument).filter(Instrument.symbol == "NIFTY").one()
    assert db.query(OptionChainSnapshot).count() == 0

    state = ensure_fresh_option_chain(
        db, broker, nifty.id, EXPIRY, session_factory=test_session_factory
    )

    assert state == FreshnessState.LIVE
    assert db.query(OptionChainSnapshot).count() == 1


def test_ensure_fresh_option_chain_does_not_refetch_when_already_live(
    seeded_universe, test_session_factory, db: Session
):
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=7)
    nifty = db.query(Instrument).filter(Instrument.symbol == "NIFTY").one()
    record_option_chain_snapshot(
        nifty.id, broker, "NIFTY", EXPIRY, session_factory=test_session_factory
    )
    assert db.query(OptionChainSnapshot).count() == 1

    state = ensure_fresh_option_chain(
        db, broker, nifty.id, EXPIRY, session_factory=test_session_factory
    )

    assert state == FreshnessState.LIVE
    assert db.query(OptionChainSnapshot).count() == 1  # no second snapshot written


def test_ensure_fresh_option_chain_reports_dead_on_broker_failure(
    seeded_universe, test_session_factory, db: Session
):
    from app.modules.broker_adapter.base.errors import BrokerConnectivityError

    class _FailingBroker:
        def get_option_chain(self, *args, **kwargs):
            raise BrokerConnectivityError("feed down")

        def __getattr__(self, name):
            raise AttributeError(name)

    nifty = db.query(Instrument).filter(Instrument.symbol == "NIFTY").one()

    state = ensure_fresh_option_chain(
        db, _FailingBroker(), nifty.id, EXPIRY, session_factory=test_session_factory  # type: ignore[arg-type]
    )

    assert state == FreshnessState.DEAD
    assert db.query(OptionChainSnapshot).count() == 0


def test_ensure_fresh_option_chain_reports_dead_on_rate_limit_timeout(
    seeded_universe, test_session_factory, db: Session
):
    """2026-08-31 live incident: 11 concurrently-scanning StrategyRuns all
    tracking the same NIFTY expiry overwhelmed the shared broker call
    limiter (`core.rate_limiter`), which raises `RateLimitExceeded` -- a
    plain `Exception`, not a `BrokerError`. That used to propagate uncaught
    straight out of this function, crashing the caller's entire cycle
    (`StrategyRunner.run_cycle`) instead of being reported as DEAD the same
    way a `BrokerError` already is. Same shape as
    `test_ensure_fresh_option_chain_reports_dead_on_broker_failure` above,
    just the other exception type this function must also contain.
    """
    from app.core.rate_limiter import RateLimitExceeded

    class _RateLimitedBroker:
        def get_option_chain(self, *args, **kwargs):
            raise RateLimitExceeded("broker call limiter timed out waiting to call GetQuotes")

        def __getattr__(self, name):
            raise AttributeError(name)

    nifty = db.query(Instrument).filter(Instrument.symbol == "NIFTY").one()

    state = ensure_fresh_option_chain(
        db, _RateLimitedBroker(), nifty.id, EXPIRY, session_factory=test_session_factory  # type: ignore[arg-type]
    )

    assert state == FreshnessState.DEAD
    assert db.query(OptionChainSnapshot).count() == 0


def test_ensure_fresh_option_chain_coalesces_concurrent_refresh_for_same_key(
    seeded_universe, test_session_factory, db: Session
):
    """The real 2026-08-31 shape: several callers (StrategyRuns) racing to
    refresh the identical (instrument, expiry) should only issue one real
    broker call -- the second caller's own refresh would be pure redundant
    load on the shared rate limiter for data it's about to read anyway once
    the first caller's refresh lands.
    """
    import threading

    nifty = db.query(Instrument).filter(Instrument.symbol == "NIFTY").one()
    call_count = 0
    call_count_lock = threading.Lock()
    entered_event = threading.Event()
    release_event = threading.Event()
    real_broker = MockBrokerAdapter(instruments=seeded_universe, seed=7)

    class _SlowBroker:
        def get_option_chain(self, *args, **kwargs):
            nonlocal call_count
            with call_count_lock:
                call_count += 1
            entered_event.set()
            release_event.wait(timeout=5)
            return real_broker.get_option_chain(*args, **kwargs)

        def __getattr__(self, name):
            raise AttributeError(name)

    broker = _SlowBroker()
    first_state: list[FreshnessState] = []

    def _call_first():
        with test_session_factory() as db1:
            first_state.append(
                ensure_fresh_option_chain(
                    db1, broker, nifty.id, EXPIRY, session_factory=test_session_factory  # type: ignore[arg-type]
                )
            )

    t1 = threading.Thread(target=_call_first)
    t1.start()
    assert entered_event.wait(timeout=5)  # t1 is inside the broker call, lock held

    # A second, concurrent caller for the identical key must see the lock
    # already held and skip straight to re-classifying, not also call the
    # broker.
    with test_session_factory() as db2:
        second_state = ensure_fresh_option_chain(
            db2, broker, nifty.id, EXPIRY, session_factory=test_session_factory  # type: ignore[arg-type]
        )

    release_event.set()
    t1.join(timeout=5)

    assert call_count == 1
    assert second_state == FreshnessState.DEAD  # nothing written yet when it checked
    assert first_state == [FreshnessState.LIVE]
    assert db.query(OptionChainSnapshot).count() == 1


# -- _poll_once: silent-empty-poll observability (2026-08-10 static-audit fix) --
# Calls `_poll_once` directly rather than `start()`+`sleep()` like the REST-
# fallback tests above — this feature is about exact wall-clock stall
# duration, and driving it through the background poll thread's own timing
# would make the 90s threshold either untestable-fast or flaky-slow for no
# benefit; `_poll_once` is already exposed as a standalone unit for this
# same reason `PositionManager.run_once`/`StrategyRunner.run_cycle` are.


def test_poll_once_stays_silent_on_the_very_first_empty_poll(
    seeded_universe, test_session_factory, db: Session, caplog
):
    """No prior `_last_valid_data_time` entry for this symbol yet — nothing
    to measure a stall against, so this must not warn off a missing
    baseline (a fresh service instance, or a symbol that has simply never
    once had data, must not immediately look like a stall).
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    nifty = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one()
    broker = _NeverTicksBroker()  # no candles configured for any symbol
    service = MarketDataIngestionService(_provider(broker), session_factory=test_session_factory)

    with caplog.at_level(logging.WARNING, logger="app.market_data"):
        service._poll_once(underlying_symbol, nifty.id)  # noqa: SLF001

    assert underlying_symbol not in service._last_valid_data_time  # noqa: SLF001
    assert "silently throttling" not in caplog.text


def test_poll_once_warns_once_the_empty_stall_exceeds_the_threshold(
    seeded_universe, test_session_factory, db: Session, caplog
):
    """Live-motivated: `get_price_history` returning `HTTP 200, status: true,
    data: []` for 5 straight days went completely unnoticed until manually
    investigated. This is the fix — a stall past 90s of real elapsed time
    (not a poll-cycle count, which would need to change every time the
    poll interval or the broker does) must produce an explicit warning.
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    nifty = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one()
    broker = _NeverTicksBroker()  # no candles configured for any symbol
    service = MarketDataIngestionService(_provider(broker), session_factory=test_session_factory)
    service._last_valid_data_time[underlying_symbol] = datetime.now(UTC) - timedelta(  # noqa: SLF001
        seconds=95
    )

    with caplog.at_level(logging.WARNING, logger="app.market_data"):
        service._poll_once(underlying_symbol, nifty.id)  # noqa: SLF001

    assert "silently throttling" in caplog.text
    assert underlying_symbol in caplog.text


def test_poll_once_does_not_warn_before_the_threshold_elapses(
    seeded_universe, test_session_factory, db: Session, caplog
):
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    nifty = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one()
    broker = _NeverTicksBroker()  # no candles configured for any symbol
    service = MarketDataIngestionService(_provider(broker), session_factory=test_session_factory)
    service._last_valid_data_time[underlying_symbol] = datetime.now(UTC) - timedelta(  # noqa: SLF001
        seconds=10
    )

    with caplog.at_level(logging.WARNING, logger="app.market_data"):
        service._poll_once(underlying_symbol, nifty.id)  # noqa: SLF001

    assert "silently throttling" not in caplog.text


def test_poll_once_resets_last_valid_data_time_when_candles_come_back(
    seeded_universe, test_session_factory, db: Session
):
    """Proves this actually *resets* the marker on real data, not just
    leaves an already-fresh value untouched — seeded with a stale marker
    that would otherwise still read as a live stall if this were a no-op.
    """
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    nifty = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one()
    now = datetime.now(UTC)
    completed_bucket = now - timedelta(seconds=now.timestamp() % 60 + 60)
    candles = [
        PriceCandle(
            bucket_start=completed_bucket, open=100.0, high=101.0, low=99.0, close=100.5, volume=0
        )
    ]
    broker = _NeverTicksBroker(candles_by_symbol={underlying_symbol: candles})
    service = MarketDataIngestionService(_provider(broker), session_factory=test_session_factory)
    service._last_valid_data_time[underlying_symbol] = now - timedelta(seconds=500)  # noqa: SLF001

    before = datetime.now(UTC)
    service._poll_once(underlying_symbol, nifty.id)  # noqa: SLF001
    after = datetime.now(UTC)

    recorded = service._last_valid_data_time[underlying_symbol]  # noqa: SLF001
    assert before <= recorded <= after
