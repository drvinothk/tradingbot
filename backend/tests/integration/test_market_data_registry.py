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
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType
from app.domain.market.mock_universe import build_mock_universe
from app.domain.market.models import (
    IndicatorSnapshot,
    Instrument,
    OptionContract,
    PriceBar,
    QuoteTick,
)
from app.domain.session.models import FundingMode, SafeMode, TradingSession, TradingSessionStatus
from app.domain.strategy.models import ExecutionMode, StrategyConfig, StrategyRun, StrategyRunStatus
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


def test_reset_daily_indicators_delegates_to_the_shared_service(monkeypatch):
    """Pure bookkeeping check, no real thread/DB — same fake-service shape
    as test_ensure_ingestion_running_shares_one_service_and_is_idempotent_
    per_symbol above.
    """
    reset_calls = 0

    class _FakeIngestionService:
        def __init__(self, provider, session_factory=None, indicator_engine=None):
            pass

        def start(self, symbols):
            pass

        def reset_daily_indicators(self):
            nonlocal reset_calls
            reset_calls += 1

    monkeypatch.setattr(market_data_registry, "MarketDataIngestionService", _FakeIngestionService)

    market_data_registry.ensure_ingestion_running("NIFTY", provider=object())
    market_data_registry.reset_daily_indicators()

    assert reset_calls == 1


def test_reset_daily_indicators_is_a_harmless_noop_before_any_service_exists():
    market_data_registry.reset_daily_indicators()  # must not raise


class _FakeIngestionServiceWithFallbackTracking:
    """Fake matching real `MarketDataIngestionService`'s `fallback_symbols`/
    `forget_symbol` shape, for `reset_subscriptions_for_new_day` tests.
    """

    def __init__(self, provider, session_factory=None, indicator_engine=None):
        self.starts: list[list[str]] = []
        self.forgotten: list[str] = []
        self._fallback: set[str] = set()

    def start(self, symbols):
        self.starts.append(list(symbols))

    @property
    def fallback_symbols(self):
        return frozenset(self._fallback)

    def forget_symbol(self, symbol):
        self.forgotten.append(symbol)


def test_reset_subscriptions_for_new_day_clears_bookkeeping_for_ws_symbols(monkeypatch):
    """2026-08-19 regression: proves the actual mechanics of the fix --
    a symbol subscribed "yesterday" (still in `_subscribed_symbols` from a
    prior `ensure_ingestion_running` call, this process never having
    restarted) must be forgotten so the *next* `ensure_ingestion_running`
    call for it genuinely calls `.start()` again, rather than treating it
    as already handled.
    """
    fake = _FakeIngestionServiceWithFallbackTracking(object())
    monkeypatch.setattr(
        market_data_registry, "MarketDataIngestionService", lambda *a, **k: fake
    )

    market_data_registry.ensure_ingestion_running("NIFTY", provider=object())
    market_data_registry.ensure_ingestion_running("BANKNIFTY", provider=object())
    fake.starts.clear()

    market_data_registry.reset_subscriptions_for_new_day()

    assert set(fake.forgotten) == {"NIFTY", "BANKNIFTY"}
    assert market_data_registry._subscribed_symbols == set()

    market_data_registry.ensure_ingestion_running("NIFTY", provider=object())
    assert fake.starts == [["NIFTY"]], "must genuinely re-subscribe, not treat it as a no-op"


def test_reset_subscriptions_for_new_day_leaves_rest_fallback_symbols_untouched(monkeypatch):
    """A symbol already on REST fallback must not be forgotten or have its
    `_subscribed_symbols` entry cleared -- re-triggering `.start()` for it
    would re-send a WS subscribe that could race a REST-polled insert for
    the same `price_bars` bucket, exactly what the fallback's own
    unsubscribe-on-switch was built to prevent (see `forget_symbol`'s own
    docstring).
    """
    fake = _FakeIngestionServiceWithFallbackTracking(object())
    fake._fallback.add("NIFTY")  # NIFTY already fell back to REST
    monkeypatch.setattr(
        market_data_registry, "MarketDataIngestionService", lambda *a, **k: fake
    )

    market_data_registry.ensure_ingestion_running("NIFTY", provider=object())
    market_data_registry.ensure_ingestion_running("BANKNIFTY", provider=object())
    fake.starts.clear()

    market_data_registry.reset_subscriptions_for_new_day()

    assert fake.forgotten == ["BANKNIFTY"], "NIFTY (on REST fallback) must not be forgotten"
    assert market_data_registry._subscribed_symbols == {"NIFTY"}

    market_data_registry.ensure_ingestion_running("NIFTY", provider=object())
    assert fake.starts == [], "still tracked as subscribed -- must stay a no-op"


def test_reset_subscriptions_for_new_day_is_a_harmless_noop_before_any_service_exists():
    market_data_registry.reset_subscriptions_for_new_day()  # must not raise


def test_reset_for_reconnect_stops_and_rebuilds_every_previously_subscribed_symbol(
    monkeypatch, test_session_factory,
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

    # test_session_factory (not the default session_scope, which is the
    # production DB) -- see resume_ingestion_for_active_runs's own docstring
    # for why this parameter exists. Nothing in the test DB matches the
    # active-run query, so this contributes no extra .start() calls here.
    market_data_registry.reset_for_reconnect(session_factory=test_session_factory)

    assert stops == [["BANKNIFTY", "NIFTY"]], "both symbols stopped on the old service"
    assert set_calls == [None], "provider_composition singleton must be cleared"
    assert starts == [["BANKNIFTY"], ["NIFTY"]], "both symbols re-subscribed on the new service"


def test_reset_for_reconnect_is_a_harmless_noop_with_nothing_subscribed(
    monkeypatch, test_session_factory,
):
    set_calls: list[object | None] = []
    monkeypatch.setattr(
        "app.modules.market_data.provider_composition.set_market_data_provider",
        set_calls.append,
    )

    market_data_registry.reset_for_reconnect(session_factory=test_session_factory)  # must not raise

    assert set_calls == [None]


def _active_run_on_instrument(
    db: Session, workspace, user, instrument: Instrument
) -> StrategyRun:
    broker_account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="registry-test-account",
        credentials_ref="config/credentials/shoonya.env",
        status=BrokerAccountStatus.ACTIVE,
    )
    db.add(broker_account)
    db.flush()

    trading_session = TradingSession(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_account_id=broker_account.id,
        started_by_user_id=user.id,
        mode=SafeMode.PAPER_ONLY,
        status=TradingSessionStatus.ACTIVE,
        started_at=datetime.now(UTC),
        budget_amount=1_000_000,
        daily_target_profit=1_000_000,
        daily_loss_cap=1_000_000,
        funding_mode=FundingMode.CASH,
    )
    db.add(trading_session)
    db.flush()

    strategy_config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name=f"registry-test-{uuid.uuid4().hex[:8]}"
    )
    db.add(strategy_config)
    db.flush()

    strategy_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
        instrument_id=instrument.id,
        expiry_date=EXPIRY,
    )
    db.add(strategy_run)
    db.flush()
    return strategy_run


def test_resume_ingestion_for_active_runs_starts_ingestion_for_active_strategy_runs(
    db: Session, workspace, user, monkeypatch
):
    """`resume_ingestion_for_active_runs` is what actually closes the
    2026-08-14 cold-reconnect gap — proves it queries active, non-STOPPED
    runs directly (not via `_subscribed_symbols`, which a cold restart never
    populated) and starts ingestion for the underlying each one needs.
    """
    instrument = Instrument(
        id=uuid.uuid4(), symbol="NIFTY-REGISTRY", exchange="NFO", lot_size=25, tick_size=0.05
    )
    db.add(instrument)
    db.flush()
    _active_run_on_instrument(db, workspace, user, instrument)

    @contextmanager
    def _fake_session_scope():
        yield db

    starts: list[list[str]] = []

    class _FakeIngestionService:
        def __init__(self, provider, session_factory=None, indicator_engine=None):
            pass

        def start(self, symbols):
            starts.append(list(symbols))

    monkeypatch.setattr(market_data_registry, "MarketDataIngestionService", _FakeIngestionService)

    market_data_registry.resume_ingestion_for_active_runs(session_factory=_fake_session_scope)

    assert starts == [["NIFTY-REGISTRY"]]


def test_reset_for_reconnect_starts_ingestion_for_active_runs_on_a_cold_reconnect(
    db: Session, workspace, user, monkeypatch
):
    """The actual bug scenario: a cold restart never subscribed anything
    (`_resume_strategy_runners` now defers `ensure_ingestion_running` until
    Shoonya connects, see `app.main`'s own docstring), so the pre-existing
    "resubscribe every previously-subscribed symbol" logic alone has nothing
    to act on — `resume_ingestion_for_active_runs` is what makes the first
    reconnect after any restart actually start ingestion for real.
    """
    instrument = Instrument(
        id=uuid.uuid4(), symbol="BANKNIFTY-REGISTRY", exchange="NFO", lot_size=15, tick_size=0.05
    )
    db.add(instrument)
    db.flush()
    _active_run_on_instrument(db, workspace, user, instrument)

    @contextmanager
    def _fake_session_scope():
        yield db

    starts: list[list[str]] = []

    class _FakeIngestionService:
        def __init__(self, provider, session_factory=None, indicator_engine=None):
            pass

        def start(self, symbols):
            starts.append(list(symbols))

    monkeypatch.setattr(market_data_registry, "MarketDataIngestionService", _FakeIngestionService)
    monkeypatch.setattr(
        "app.modules.market_data.provider_composition.set_market_data_provider",
        lambda provider: None,
    )

    assert market_data_registry._subscribed_symbols == set()  # nothing subscribed -- cold start

    market_data_registry.reset_for_reconnect(session_factory=_fake_session_scope)

    assert starts == [["BANKNIFTY-REGISTRY"]]
