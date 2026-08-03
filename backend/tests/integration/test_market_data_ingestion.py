"""MarketDataIngestionService writes via a background-thread callback, so it
needs its own session per event — session_factory is injected here to point
at the isolated test database (see conftest.py's `engine` fixture) instead of
the real dev DB the default `session_scope` targets.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from datetime import date, timedelta

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
from app.modules.broker_adapter.base.contracts import Tick
from app.modules.broker_adapter.mock import MockBrokerAdapter
from app.modules.market_data import MarketDataIngestionService, record_option_chain_snapshot
from app.modules.market_data.freshness import FreshnessState, ensure_fresh_option_chain
from app.modules.market_data.indicators import IndicatorEngine

EXPIRY = date(2026, 7, 31)


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
    service = MarketDataIngestionService(broker, session_factory=test_session_factory)

    symbols = [i.symbol for i in seeded_universe[:5]]
    symbol_map = service._build_symbol_map(symbols)  # noqa: SLF001

    assert set(symbol_map.keys()) == set(symbols)


def test_unknown_symbol_does_not_raise(seeded_universe, test_session_factory, caplog):
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=1)
    service = MarketDataIngestionService(broker, session_factory=test_session_factory)

    symbol_map = service._build_symbol_map(["NOT-A-REAL-SYMBOL"])  # noqa: SLF001
    assert symbol_map == {}


def test_streamed_ticks_are_persisted_for_option_contract(
    seeded_universe, test_session_factory, db: Session
):
    option_symbol = next(i.symbol for i in seeded_universe if i.is_option)
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=2, tick_interval_seconds=0.1)
    service = MarketDataIngestionService(broker, session_factory=test_session_factory)

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
    service = MarketDataIngestionService(broker, session_factory=test_session_factory)

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
        broker,
        session_factory=test_session_factory,
        indicator_engine=IndicatorEngine(timeframe_seconds=60),
    )
    service._symbol_map = service._build_symbol_map([underlying_symbol])  # noqa: SLF001

    def snapshot_names() -> set[str]:
        rows = db.query(IndicatorSnapshot).filter(IndicatorSnapshot.instrument_id == nifty.id).all()
        return {row.indicator_name for row in rows}

    base_ts = broker.get_quote(underlying_symbol).ts
    first_tick = Tick(
        underlying_symbol, ltp=100.0, bid=99.9, ask=100.1, volume=10, oi=None, ts=base_ts
    )
    # First tick: VWAP should persist immediately (one sample is enough),
    # EMA9/EMA20 need 9 completed 60s bars, so neither should exist yet.
    service._on_tick(first_tick)  # noqa: SLF001
    assert snapshot_names() == {"VWAP"}

    # 9 more ticks, each in a new 60s bucket, completes 9 bars -> EMA9 warms up.
    for i in range(1, 10):
        tick_ts = base_ts + timedelta(seconds=60 * i)
        service._on_tick(  # noqa: SLF001
            Tick(underlying_symbol, ltp=100.0 + i, bid=0, ask=0, volume=10, oi=None, ts=tick_ts)
        )

    names_after_warmup = snapshot_names()
    assert "EMA9" in names_after_warmup
    assert "VWAP" in names_after_warmup
    # EMA20 shouldn't exist yet — only 9 bars have completed.
    assert "EMA20" not in names_after_warmup


def test_completed_bars_are_persisted_for_underlying_instrument(
    seeded_universe, test_session_factory, db: Session
):
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    nifty = db.query(Instrument).filter(Instrument.symbol == underlying_symbol).one()

    broker = MockBrokerAdapter(instruments=seeded_universe, seed=7)
    service = MarketDataIngestionService(
        broker,
        session_factory=test_session_factory,
        indicator_engine=IndicatorEngine(timeframe_seconds=60),
    )
    service._symbol_map = service._build_symbol_map([underlying_symbol])  # noqa: SLF001

    base_ts = broker.get_quote(underlying_symbol).ts
    service._on_tick(  # noqa: SLF001
        Tick(underlying_symbol, ltp=100.0, bid=0, ask=0, volume=10, oi=None, ts=base_ts)
    )
    assert db.query(PriceBar).count() == 0  # no bucket boundary crossed yet

    service._on_tick(  # noqa: SLF001
        Tick(
            underlying_symbol,
            ltp=105.0,
            bid=0,
            ask=0,
            volume=5,
            oi=None,
            ts=base_ts + timedelta(seconds=60),
        )
    )

    bars = db.query(PriceBar).filter(PriceBar.instrument_id == nifty.id).all()
    assert len(bars) == 1
    assert float(bars[0].open) == 100.0
    assert float(bars[0].close) == 100.0
    assert bars[0].timeframe == "60s"


def test_depth_only_persisted_for_option_contracts_not_underlying(
    seeded_universe, test_session_factory, db: Session
):
    # Force on_depth to fire deterministically rather than relying on the
    # mock adapter's ~30% random chance per tick.
    underlying_symbol = next(i.symbol for i in seeded_universe if not i.is_option)
    broker = MockBrokerAdapter(instruments=seeded_universe, seed=4, tick_interval_seconds=0.05)
    service = MarketDataIngestionService(broker, session_factory=test_session_factory)
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
