"""market_data.registry — regression coverage for a real bug found during
Phase 4 manual QC: `MockBrokerAdapter.subscribe_quotes` has exactly one
`on_tick`/`on_depth` callback slot (matching `BrokerPort`'s own "single
shared connection" contract), so a `MarketDataIngestionService` instance
*per underlying instrument* silently clobbers all but the most recently
subscribed one — the earlier registry design before this test existed.

`ensure_ingestion_running` itself isn't exercised with a real background
thread here — same reasoning `execution_engine.paper.registry.
ensure_position_manager_running` is never called for real in HTTP-level
tests either (test_api_strategies.py monkeypatches it away wholesale): its
default session_factory is `session_scope`, bound to the *production* DB,
not the isolated test engine. The real regression (one shared service, two
underlyings, no clobbering) is proven directly against
`MarketDataIngestionService` with an injected `test_session_factory`, same
pattern test_market_data_ingestion.py already uses; the registry's own
bookkeeping (idempotent per symbol, one shared instance) is proven with the
`MarketDataIngestionService` class itself monkeypatched to a thread/DB-free
fake.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.domain.market.mock_universe import build_mock_universe
from app.domain.market.models import (
    IndicatorSnapshot,
    Instrument,
    OptionContract,
    PriceBar,
    QuoteTick,
)
from app.modules.broker_adapter.mock.adapter import MockBrokerAdapter
from app.modules.market_data import registry as market_data_registry
from app.modules.market_data.indicators import IndicatorEngine
from app.modules.market_data.ingestion import MarketDataIngestionService
from app.modules.market_data.providers.broker_port_shim import BrokerPortMarketDataAdapter

EXPIRY = date(2026, 7, 31)


@pytest.fixture(autouse=True)
def reset_registry():
    market_data_registry.reset()
    yield
    market_data_registry.reset()


@pytest.fixture
def test_session_factory(engine):
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
    universe = build_mock_universe(EXPIRY)
    instrument_ids: dict[str, uuid.UUID] = {}
    with test_session_factory() as seed_db:
        for info in universe:
            if info.is_option:
                continue
            instrument = Instrument(
                id=uuid.uuid4(), symbol=info.symbol, exchange=info.exchange,
                lot_size=info.lot_size, tick_size=info.tick_size,
            )
            seed_db.add(instrument)
            seed_db.flush()
            instrument_ids[info.symbol] = instrument.id
        for info in universe:
            if not info.is_option:
                continue
            assert info.underlying is not None
            assert info.option_type is not None
            seed_db.add(OptionContract(
                id=uuid.uuid4(), instrument_id=instrument_ids[info.underlying],
                expiry_date=info.expiry, strike=info.strike,
                option_type=info.option_type.value, symbol=info.symbol, broker_token="",
            ))

    yield universe, instrument_ids

    with test_session_factory() as cleanup_db:
        cleanup_db.query(QuoteTick).delete()
        cleanup_db.query(PriceBar).delete()
        cleanup_db.query(IndicatorSnapshot).delete()
        cleanup_db.query(OptionContract).delete()
        cleanup_db.query(Instrument).delete()


def test_one_shared_service_delivers_ticks_for_both_underlyings(
    seeded_universe, test_session_factory
):
    """The actual regression: two `.start()` calls on one service (what
    `ensure_ingestion_running` does for two different underlyings) must not
    let the second call's subscription silently stop the first's ticks.
    """
    universe, instrument_ids = seeded_universe
    broker = MockBrokerAdapter(instruments=universe, seed=42, tick_interval_seconds=0.05)
    service = MarketDataIngestionService(
        BrokerPortMarketDataAdapter(broker),
        session_factory=test_session_factory,
        indicator_engine=IndicatorEngine(),
    )

    service.start(["NIFTY"])
    service.start(["BANKNIFTY"])
    time.sleep(0.4)
    service.stop(["NIFTY", "BANKNIFTY"])

    with test_session_factory() as verify_db:
        nifty_count = (
            verify_db.query(IndicatorSnapshot)
            .filter(IndicatorSnapshot.instrument_id == instrument_ids["NIFTY"])
            .count()
        )
        banknifty_count = (
            verify_db.query(IndicatorSnapshot)
            .filter(IndicatorSnapshot.instrument_id == instrument_ids["BANKNIFTY"])
            .count()
        )

    assert nifty_count > 0, "first-subscribed underlying must still receive ticks"
    assert banknifty_count > 0, "second-subscribed underlying must also receive ticks"


def test_ensure_ingestion_running_shares_one_service_and_is_idempotent_per_symbol(
    monkeypatch,
):
    """Pure bookkeeping check, no real thread/DB — `MarketDataIngestionService`
    itself is replaced with a fake that just records `.start()` calls.
    """
    starts: list[list[str]] = []

    class _FakeIngestionService:
        def __init__(self, provider, session_factory=None, indicator_engine=None):
            pass

        def start(self, symbols):
            starts.append(list(symbols))

    monkeypatch.setattr(market_data_registry, "MarketDataIngestionService", _FakeIngestionService)

    service_1 = market_data_registry.ensure_ingestion_running("NIFTY", provider=object())
    service_2 = market_data_registry.ensure_ingestion_running("NIFTY", provider=object())
    service_3 = market_data_registry.ensure_ingestion_running("BANKNIFTY", provider=object())

    assert service_1 is service_2 is service_3
    assert starts == [["NIFTY"], ["BANKNIFTY"]]  # the repeated "NIFTY" call was a no-op


def test_reset_for_reconnect_stops_and_rebuilds_every_previously_subscribed_symbol(
    monkeypatch,
):
    """2026-08-12 regression: `get_market_data_provider()` is a lazy
    singleton resolved once, at process startup, always against the
    startup-default mock (a real Shoonya session can't exist yet at that
    point). Without `reset_for_reconnect`, ingestion would silently keep
    quoting whatever it first resolved to forever, even after a real
    Shoonya reconnect moments later. This proves the actual mechanics:
    every previously-subscribed symbol is stopped on the old service, the
    `provider_composition` singleton is cleared, and every one of those
    symbols is immediately re-subscribed against a freshly-built service.
    """
    starts: list[list[str]] = []
    stops: list[list[str]] = []

    class _FakeIngestionService:
        def __init__(self, provider, session_factory=None, indicator_engine=None):
            pass

        def start(self, symbols):
            starts.append(list(symbols))

        def stop(self, symbols):
            stops.append(list(symbols))

    monkeypatch.setattr(market_data_registry, "MarketDataIngestionService", _FakeIngestionService)
    monkeypatch.setattr(
        "app.modules.market_data.provider_composition.get_market_data_provider",
        lambda: object(),
    )
    set_calls: list[object | None] = []
    monkeypatch.setattr(
        "app.modules.market_data.provider_composition.set_market_data_provider",
        set_calls.append,
    )

    market_data_registry.ensure_ingestion_running("NIFTY", provider=object())
    market_data_registry.ensure_ingestion_running("BANKNIFTY", provider=object())
    starts.clear()  # only the re-subscribe after reset matters below

    market_data_registry.reset_for_reconnect()

    assert stops == [["BANKNIFTY", "NIFTY"]], "both symbols stopped on the old service"
    assert set_calls == [None], "provider_composition singleton must be cleared"
    assert starts == [["BANKNIFTY"], ["NIFTY"]], "both symbols re-subscribed on the new service"


def test_reset_for_reconnect_is_a_harmless_noop_with_nothing_subscribed(monkeypatch):
    set_calls: list[object | None] = []
    monkeypatch.setattr(
        "app.modules.market_data.provider_composition.set_market_data_provider",
        set_calls.append,
    )

    market_data_registry.reset_for_reconnect()  # must not raise

    assert set_calls == [None]
