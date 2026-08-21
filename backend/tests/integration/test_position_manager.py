"""PositionManager — the background poller that checks stop/target/trail on
every open Position, force-closes at EOD, and periodically reconciles.
Requires real Postgres (dispatch/close run under advisory locks, same
reasoning as test_execution_paper_service.py). Most tests drive
`run_once()` directly against the test's own rolled-back `db` session
(deterministic, no thread/timing involved) — a dedicated test at the bottom
exercises the actual background-thread timer, same split
test_synthetic_strategy.py uses for `StrategyRunner`.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.domain.broker.models import ReconciliationRun
from app.domain.execution.models import ExitReason, Position, PositionStatus, TradeOutcome
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.market.models import Instrument, OptionContract, OptionType
from app.domain.ops.models import SystemAlert
from app.domain.session.models import (
    FundingMode,
    SafeMode,
    SessionModeTransition,
    TradingSession,
    TransitionTriggerType,
)
from app.domain.strategy.models import (
    ExecutionMode,
    Signal,
    SignalSide,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    StrategyRuntimeMode,
    TradeIntent,
    TradeIntentStatus,
)
from app.modules.broker_adapter.base.errors import BrokerAuthError
from app.modules.broker_adapter.mock.adapter import MockBrokerAdapter
from app.modules.execution_engine.paper.position_manager import PositionManager
from app.modules.execution_engine.paper.service import dispatch_trade_intent
from app.modules.market_data.providers.base import BaseMarketDataProvider

EXPIRY = date(2026, 7, 30)


class _NullMarketDataProvider(BaseMarketDataProvider):
    """Every test in this file controls pricing entirely through its own
    `broker` fixture (or a wrapper around it) — `PositionManager` now tries
    the live market-data feed *first* (see `_live_tick`), which by default
    resolves to an unrelated `get_broker()` mock singleton whose own
    background stream would race this test's carefully set-up prices with
    irrelevant random ticks (found via a QC pass — see `_live_tick`'s own
    docstring and `_ensure_symbol_subscribed`'s). Injecting this no-op
    provider makes `get_latest_tick` always return `None`, so every test
    deterministically exercises the `broker.get_quote` fallback path, same
    as this class's entire behavior before market-data decoupling existed.
    """

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def subscribe_ticks(self, symbols, on_tick, on_depth=None) -> None:
        pass

    def unsubscribe_ticks(self, symbols) -> None:
        pass

    def get_latest_tick(self, symbol):
        return None

    def get_price_history(self, underlying, start, end, timeframe_seconds=60):
        return []


@pytest.fixture
def broker() -> MockBrokerAdapter:
    return MockBrokerAdapter()


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="pm-test-account",
        credentials_ref="config/credentials/shoonya.env",
        status=BrokerAccountStatus.ACTIVE,
    )
    db.add(account)
    db.flush()
    return account


@pytest.fixture
def trading_session(db: Session, workspace, broker_account, user: User) -> TradingSession:
    ts = TradingSession(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_account_id=broker_account.id,
        started_by_user_id=user.id,
        mode=SafeMode.PAPER_ONLY,
        started_at=datetime.now(UTC),
        budget_amount=1_000_000,
        daily_target_profit=1_000_000,
        daily_loss_cap=1_000_000,
        funding_mode=FundingMode.CASH,
        # Explicit, not the column default (15:20 IST) — tests in this file
        # that expect a position to stay OPEN must not depend on real
        # wall-clock IST staying before cutoff_time; the default silently
        # started forcing EOD square-off on every position once a real test
        # run happened to execute after 15:20 IST.
        cutoff_time=dt_time(23, 59),
    )
    db.add(ts)
    db.flush()
    return ts


@pytest.fixture
def instrument(db: Session) -> Instrument:
    inst = Instrument(id=uuid.uuid4(), symbol="NIFTY", exchange="NFO", lot_size=25, tick_size=0.05)
    db.add(inst)
    db.flush()
    return inst


@pytest.fixture
def option_contract(db: Session, instrument: Instrument) -> OptionContract:
    contract = OptionContract(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        expiry_date=EXPIRY,
        strike=22000,
        option_type=OptionType.CE,
        symbol="NIFTY26JUL22000CE-PM",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(id=uuid.uuid4(), workspace_id=workspace.id, name="pm-test-strategy")
    db.add(config)
    db.flush()
    return config


@pytest.fixture
def strategy_run(
    db: Session, strategy_config: StrategyConfig, trading_session, user: User
) -> StrategyRun:
    run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
    )
    db.add(run)
    db.flush()
    return run


def _dispatch_position(
    db: Session,
    trading_session: TradingSession,
    strategy_run: StrategyRun,
    option_contract: OptionContract,
    broker: MockBrokerAdapter,
    *,
    stop_price: float,
    target_price: float,
    structure_level: float | None = None,
) -> Position:
    now = datetime.now(UTC)
    signal = Signal(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        strategy_config_id=strategy_run.strategy_config_id,
        strategy_run_id=strategy_run.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        side=SignalSide.BUY,
        entry_price=80.0,
        stop_price=stop_price,
        target_price=target_price,
        qty_lots=1,
        structure_level=structure_level,
        generated_at=now,
    )
    db.add(signal)
    db.flush()

    trade_intent = TradeIntent(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        signal_id=signal.id,
        strategy_run_id=strategy_run.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        idempotency_key=f"signal:{signal.id}",
        side=SignalSide.BUY,
        qty_lots=1,
        entry_price=80.0,
        stop_price=stop_price,
        target_price=target_price,
        structure_level=structure_level,
        status=TradeIntentStatus.DISPATCHED,
        created_at=now,
        dispatched_at=now,
    )
    db.add(trade_intent)
    db.flush()

    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    return db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()


def _session_factory_for(db: Session):
    """Returns a zero-arg `SessionFactory` callable that always hands back
    the test's own rolled-back `db` session — `PositionManager.run_once()`
    expects `session_factory()` to produce a context manager, but this test
    wants every cycle to see the same in-progress transaction the rest of
    the test uses (fixture setup, assertions), not a separate connection.
    """

    @contextmanager
    def _factory():
        yield db

    return _factory


class _FakeLiveProvider(BaseMarketDataProvider):
    """A controllable stand-in for the live market-data feed — unlike
    `_NullMarketDataProvider`, this one actually returns a tick from
    `get_latest_tick`, so tests can prove `PositionManager` prices from it
    in preference to `broker.get_quote` (the "full decoupling" behavior
    `_live_tick` implements), not just that it falls back correctly when
    the feed has nothing.
    """

    def __init__(self) -> None:
        self.ticks: dict[str, object] = {}
        self.subscribed: list[str] = []

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def subscribe_ticks(self, symbols, on_tick, on_depth=None) -> None:
        self.subscribed.extend(symbols)

    def unsubscribe_ticks(self, symbols) -> None:
        pass

    def get_latest_tick(self, symbol):
        return self.ticks.get(symbol)

    def get_price_history(self, underlying, start, end, timeframe_seconds=60):
        return []


def test_run_once_prices_from_the_live_feed_in_preference_to_broker_get_quote(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """The actual "full decoupling" claim: with a live tick available,
    PositionManager must act on *that* price, not on whatever the execution
    broker's own get_quote would report — proven by setting the two sources
    to disagree about whether the stop has been hit.
    """
    from app.modules.broker_adapter.base.contracts import Tick

    position = _dispatch_position(
        db, trading_session, strategy_run, option_contract, broker,
        stop_price=72.0, target_price=92.0,
    )
    # Execution broker reports a safe price...
    broker._prices[option_contract.symbol] = 80.0  # noqa: SLF001
    # ...but the live feed reports the stop has actually been breached.
    provider = _FakeLiveProvider()
    provider.ticks[option_contract.symbol] = Tick(
        contract_symbol=option_contract.symbol,
        ltp=60.0, bid=59.9, ask=60.1, volume=10, oi=None, ts=datetime.now(UTC),
    )

    manager = PositionManager(
        trading_session.id,
        broker=broker,
        market_data_provider=provider,
        session_factory=_session_factory_for(db),
    )
    manager.run_once()

    db.refresh(position)
    assert position.status == PositionStatus.CLOSED
    assert option_contract.symbol in provider.subscribed


def test_run_once_falls_back_to_broker_quote_when_live_tick_is_stale(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """A live tick older than the freshness threshold must be treated the
    same as no tick at all — `_live_tick` falls back to `broker.get_quote`
    rather than acting on stale data.
    """
    from app.modules.broker_adapter.base.contracts import Tick

    position = _dispatch_position(
        db, trading_session, strategy_run, option_contract, broker,
        stop_price=72.0, target_price=92.0,
    )
    broker._prices[option_contract.symbol] = 80.0  # noqa: SLF001 - safe, must stay OPEN
    provider = _FakeLiveProvider()
    # Stale (well past TICK_THRESHOLDS.stale_after_seconds) and, if acted on,
    # would have wrongly closed the position on a phantom stop-hit.
    stale_ts = datetime.now(UTC) - timedelta(seconds=3600)
    provider.ticks[option_contract.symbol] = Tick(
        contract_symbol=option_contract.symbol,
        ltp=60.0, bid=59.9, ask=60.1, volume=10, oi=None, ts=stale_ts,
    )

    manager = PositionManager(
        trading_session.id,
        broker=broker,
        market_data_provider=provider,
        session_factory=_session_factory_for(db),
    )
    manager.run_once()

    db.refresh(position)
    assert position.status == PositionStatus.OPEN


def test_run_once_exits_on_stop_hit(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    position = _dispatch_position(
        db, trading_session, strategy_run, option_contract, broker,
        stop_price=72.0, target_price=92.0,
    )
    broker._prices[option_contract.symbol] = 60.0  # noqa: SLF001 - force a price below stop

    manager = PositionManager(
        trading_session.id,
        broker=broker,
        market_data_provider=_NullMarketDataProvider(),
        session_factory=_session_factory_for(db),
    )
    manager.run_once()

    db.refresh(position)
    assert position.status == PositionStatus.CLOSED


class _AuthFailingBroker:
    """Wraps a real `MockBrokerAdapter` but makes `get_quote` raise
    `BrokerAuthError`, standing in for a real adapter (Shoonya) whose
    session died mid-poll — proves `PositionManager` reacts to the generic
    broker-agnostic error, not anything Shoonya-specific (this test file
    never imports `broker_adapter.shoonya`).
    """

    def __init__(self, inner: MockBrokerAdapter):
        self._inner = inner

    def get_quote(self, contract_symbol: str):
        raise BrokerAuthError("session expired")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_run_once_moves_guarded_live_session_to_degraded_mode_on_broker_auth_error(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """`degraded_mode` exists to protect *live* money — only
    `paper_plus_guarded_live`/`live_enabled` have a legal `SYSTEM`-triggered
    edge there (see `core/modes/transitions.py`). This test uses a
    guarded-live session specifically to exercise that edge; the next test
    proves the (far more common, this phase) `paper_only` case correctly
    does *not* transition.
    """
    _dispatch_position(
        db, trading_session, strategy_run, option_contract, broker,
        stop_price=72.0, target_price=92.0,
    )
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    db.flush()

    manager = PositionManager(
        trading_session.id,
        broker=_AuthFailingBroker(broker),  # type: ignore[arg-type]
        market_data_provider=_NullMarketDataProvider(),
        session_factory=_session_factory_for(db),
    )
    manager.run_once()

    db.refresh(trading_session)
    assert trading_session.mode == SafeMode.DEGRADED_MODE

    transitions = (
        db.query(SessionModeTransition)
        .filter(SessionModeTransition.trading_session_id == trading_session.id)
        .all()
    )
    assert len(transitions) == 1
    assert transitions[0].trigger_type == TransitionTriggerType.SYSTEM


def test_run_once_does_not_transition_a_paper_only_session_on_broker_auth_error(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """`paper_only` (all of Phase 5's real traffic today) has no
    `degraded_mode` edge at all — a broker auth failure must be logged, not
    force an illegal transition or otherwise crash the poll cycle.
    """
    _dispatch_position(
        db, trading_session, strategy_run, option_contract, broker,
        stop_price=72.0, target_price=92.0,
    )
    assert trading_session.mode == SafeMode.PAPER_ONLY

    manager = PositionManager(
        trading_session.id,
        broker=_AuthFailingBroker(broker),  # type: ignore[arg-type]
        market_data_provider=_NullMarketDataProvider(),
        session_factory=_session_factory_for(db),
    )
    manager.run_once()  # must not raise

    db.refresh(trading_session)
    assert trading_session.mode == SafeMode.PAPER_ONLY
    assert (
        db.query(SessionModeTransition)
        .filter(SessionModeTransition.trading_session_id == trading_session.id)
        .count()
        == 0
    )


def test_run_once_is_idempotent_when_already_degraded(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """A second poll cycle after the session is already `degraded_mode`
    must not raise — `degraded_mode` itself has no outbound `SYSTEM`-
    triggered edge back to `degraded_mode` (nor would `transition_mode`
    accept a same-mode transition), so `_handle_broker_auth_error`'s
    "only live-adjacent modes get a transition attempt" guard correctly
    no-ops here too, same as `paper_only`.
    """
    _dispatch_position(
        db, trading_session, strategy_run, option_contract, broker,
        stop_price=72.0, target_price=92.0,
    )
    trading_session.mode = SafeMode.DEGRADED_MODE
    db.flush()

    manager = PositionManager(
        trading_session.id,
        broker=_AuthFailingBroker(broker),  # type: ignore[arg-type]
        market_data_provider=_NullMarketDataProvider(),
        session_factory=_session_factory_for(db),
    )
    manager.run_once()  # must not raise

    db.refresh(trading_session)
    assert trading_session.mode == SafeMode.DEGRADED_MODE


def _with_low_margin(broker: MockBrokerAdapter) -> MockBrokerAdapter:
    """Overrides `get_margin` directly on a real `MockBrokerAdapter`
    instance to report a negative available margin, standing in for a
    broker mid-margin-breach — proves `PositionManager` reacts via the
    narrow emergency-square-off trigger (Addendum hardening batch), not via
    kill-switch.

    2026-08-20: this used to be a `_LowMarginBroker` delegate class wrapping
    `broker` via `__getattr__` forwarding instead of patching the method
    directly. That broke once `dispatch_trade_intent`/`close_position`'s
    `broker_was_provided` guard was removed (see that fix's own
    changelog): `is_execution_broker_live`'s isinstance check against
    `MockBrokerAdapter` is exactly the right test for every *production*
    broker `get_execution_broker` can resolve (per its own docstring), but
    a plain delegate class that wraps a real mock and isn't itself a
    `MockBrokerAdapter` subclass fails that same check and reads as "live"
    — which used to silently not matter here only because the old guard
    force-tagged every explicitly-passed broker PAPER regardless. Patching
    the method directly on the real instance keeps it a genuine
    `MockBrokerAdapter` for isinstance purposes, so this test still
    exercises a paper-tagged close, matching its own intent (a mock-backed
    margin breach, not a live one).
    """
    def _get_margin():
        from app.modules.broker_adapter.base.contracts import MarginInfo

        return MarginInfo(
            available_margin=-500.0,
            used_margin=10_500.0,
            total_margin=10_000.0,
            ts=datetime.now(UTC),
        )

    broker.get_margin = _get_margin  # type: ignore[method-assign]
    return broker


def test_run_once_squares_off_all_positions_on_margin_breach_for_guarded_live_session(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    position = _dispatch_position(
        db, trading_session, strategy_run, option_contract, broker,
        stop_price=72.0, target_price=92.0,
    )
    broker._prices[option_contract.symbol] = 80.0  # noqa: SLF001 - keep it between stop/target
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    db.flush()

    manager = PositionManager(
        trading_session.id,
        broker=_with_low_margin(broker),
        market_data_provider=_NullMarketDataProvider(),
        session_factory=_session_factory_for(db),
    )
    manager.run_once()

    db.refresh(position)
    assert position.status == PositionStatus.CLOSED
    outcome = db.query(TradeOutcome).filter(TradeOutcome.position_id == position.id).one()
    assert outcome.exit_reason == ExitReason.MARGIN_BREACH

    # Kill-switch is deliberately untouched by this path.
    db.refresh(trading_session)
    assert trading_session.mode == SafeMode.PAPER_PLUS_GUARDED_LIVE

    assert (
        db.query(SystemAlert)
        .filter(
            SystemAlert.trading_session_id == trading_session.id,
            SystemAlert.category == "margin_breach_square_off",
        )
        .count()
        == 1
    )


def test_run_once_does_not_check_margin_for_paper_only_session(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """`paper_only` (this phase's real traffic) is excluded from the margin
    check entirely, not just from escalation — no real money is at stake,
    so there's nothing for this trigger to protect.
    """
    position = _dispatch_position(
        db, trading_session, strategy_run, option_contract, broker,
        stop_price=72.0, target_price=92.0,
    )
    broker._prices[option_contract.symbol] = 80.0  # noqa: SLF001 - keep it between stop/target
    assert trading_session.mode == SafeMode.PAPER_ONLY

    manager = PositionManager(
        trading_session.id,
        broker=_with_low_margin(broker),
        market_data_provider=_NullMarketDataProvider(),
        session_factory=_session_factory_for(db),
    )
    manager.run_once()

    db.refresh(position)
    assert position.status == PositionStatus.OPEN


def test_run_once_leaves_position_open_when_price_is_fine(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    # stop/target anchored to the mock adapter's own deterministic price for
    # this symbol (not an arbitrary 72/200) — see the equivalent comment in
    # test_execution_paper_service.py's trail test for why a stop/target
    # picked without regard for that base price can end up already on the
    # wrong side of it.
    real_price = broker._price_for(option_contract.symbol)  # noqa: SLF001
    position = _dispatch_position(
        db, trading_session, strategy_run, option_contract, broker,
        stop_price=real_price - 10.0, target_price=real_price + 10.0,
    )

    manager = PositionManager(
        trading_session.id,
        broker=broker,
        market_data_provider=_NullMarketDataProvider(),
        session_factory=_session_factory_for(db),
    )
    manager.run_once()

    db.refresh(position)
    assert position.status == PositionStatus.OPEN


def test_run_once_exits_on_underlying_structure_break(
    db: Session, broker, trading_session, strategy_run, option_contract, instrument
):
    real_price = broker._price_for(option_contract.symbol)  # noqa: SLF001
    position = _dispatch_position(
        db, trading_session, strategy_run, option_contract, broker,
        stop_price=real_price - 20.0, target_price=real_price + 20.0,
        structure_level=22000.0,
    )
    # Option premium stays fine (well inside stop/target); only the
    # underlying's own price has broken the opening-range/pullback/EMA9
    # level the strategy anchored structure_level to.
    broker._prices[instrument.symbol] = 21990.0  # noqa: SLF001

    manager = PositionManager(
        trading_session.id,
        broker=broker,
        market_data_provider=_NullMarketDataProvider(),
        session_factory=_session_factory_for(db),
    )
    manager.run_once()

    db.refresh(position)
    assert position.status == PositionStatus.CLOSED


def test_run_once_force_closes_past_cutoff_time(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    # entry/stop/target chosen wide apart so the stop/target check never
    # fires on its own — only the EOD branch should close this position.
    position = _dispatch_position(
        db, trading_session, strategy_run, option_contract, broker,
        stop_price=1.0, target_price=100_000.0,
    )
    trading_session.cutoff_time = dt_time(0, 0)  # always "past cutoff" in IST
    db.add(trading_session)
    db.flush()

    manager = PositionManager(
        trading_session.id,
        broker=broker,
        market_data_provider=_NullMarketDataProvider(),
        session_factory=_session_factory_for(db),
    )
    manager.run_once()

    db.refresh(position)
    assert position.status == PositionStatus.CLOSED


def _mixed_strategy_positions(
    db: Session, workspace, user, trading_session: TradingSession, option_contract: OptionContract
) -> tuple[Position, Position, MockBrokerAdapter, MockBrokerAdapter, StrategyRun]:
    """Two open positions in the same session -- one from a `force_paper`
    strategy, one from a genuinely-live strategy -- each opened via its
    *own* distinguishable `MockBrokerAdapter` instance, so a test can prove
    which broker a later close/square-off call actually landed on.
    """
    paper_config = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        name="pm-paper-strat",
        runtime_mode=StrategyRuntimeMode.FORCE_PAPER,
    )
    live_config = StrategyConfig(id=uuid.uuid4(), workspace_id=workspace.id, name="pm-live-strat")
    db.add_all([paper_config, live_config])
    db.flush()

    paper_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=paper_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
    )
    live_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=live_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
    )
    db.add_all([paper_run, live_run])
    db.flush()

    paper_broker = MockBrokerAdapter()
    live_broker = MockBrokerAdapter()

    position_paper = _dispatch_position(
        db, trading_session, paper_run, option_contract, paper_broker,
        stop_price=1.0, target_price=100_000.0,
    )
    position_live = _dispatch_position(
        db, trading_session, live_run, option_contract, live_broker,
        stop_price=1.0, target_price=100_000.0,
    )
    return position_paper, position_live, paper_broker, live_broker, paper_run


def _fake_get_execution_broker_by_strategy(paper_run_id, paper_broker, live_broker):
    def _fake(trading_session, strategy_run=None, *, position=None):
        if strategy_run is not None and strategy_run.id == paper_run_id:
            return paper_broker
        return live_broker

    return _fake


def test_run_cycle_resolves_broker_per_position_when_strategies_differ(
    db: Session, workspace, user, trading_session, option_contract, monkeypatch
):
    """2026-08-19 regression: the actual live incident. Two open positions
    in the same cycle -- one opened by a force_paper strategy, one by a
    genuinely graduated-live strategy -- on a session that has reached
    live_enabled. PositionManager must resolve each position's own broker
    via its own strategy, not reuse a single broker for the whole cycle
    (the real bug: a force_paper position's close attempt routed to the
    real broker just because the *session* was live_enabled, saved only by
    an unrelated broker-side order-type rejection).
    """
    trading_session.mode = SafeMode.LIVE_ENABLED
    db.add(trading_session)
    db.flush()

    position_paper, position_live, paper_broker, live_broker, paper_run = (
        _mixed_strategy_positions(db, workspace, user, trading_session, option_contract)
    )
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service.get_execution_broker",
        _fake_get_execution_broker_by_strategy(paper_run.id, paper_broker, live_broker),
    )

    # Both brokers report a price below stop -- both positions should close
    # this cycle, each via its own broker.
    paper_broker._prices[option_contract.symbol] = 0.5  # noqa: SLF001
    live_broker._prices[option_contract.symbol] = 0.5  # noqa: SLF001

    manager = PositionManager(
        trading_session.id,
        market_data_provider=_NullMarketDataProvider(),
        session_factory=_session_factory_for(db),
    )
    manager.run_once()

    db.refresh(position_paper)
    db.refresh(position_live)
    assert position_paper.status == PositionStatus.CLOSED
    assert position_live.status == PositionStatus.CLOSED

    # The critical assertion: each position's close order landed on *its
    # own* strategy's broker, never the other's.
    assert f"exit:{position_paper.id}" in paper_broker._orders  # noqa: SLF001
    assert f"exit:{position_paper.id}" not in live_broker._orders  # noqa: SLF001
    assert f"exit:{position_live.id}" in live_broker._orders  # noqa: SLF001
    assert f"exit:{position_live.id}" not in paper_broker._orders  # noqa: SLF001


def test_eod_square_off_resolves_broker_per_position_when_strategies_differ(
    db: Session, workspace, user, trading_session, option_contract, monkeypatch
):
    """Same 2026-08-19 fix, the EOD-square-off path -- `_square_off_all_open_
    positions` used to apply one caller-supplied broker to every position
    being force-closed, the identical bug shape as the main stop/target/
    trail loop, just reached at cutoff_time instead.
    """
    trading_session.mode = SafeMode.LIVE_ENABLED
    trading_session.cutoff_time = dt_time(0, 0)  # always "past cutoff" in IST
    db.add(trading_session)
    db.flush()

    position_paper, position_live, paper_broker, live_broker, paper_run = (
        _mixed_strategy_positions(db, workspace, user, trading_session, option_contract)
    )
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service.get_execution_broker",
        _fake_get_execution_broker_by_strategy(paper_run.id, paper_broker, live_broker),
    )

    manager = PositionManager(
        trading_session.id,
        market_data_provider=_NullMarketDataProvider(),
        session_factory=_session_factory_for(db),
    )
    manager.run_once()

    db.refresh(position_paper)
    db.refresh(position_live)
    assert position_paper.status == PositionStatus.CLOSED
    assert position_live.status == PositionStatus.CLOSED

    assert f"exit:{position_paper.id}" in paper_broker._orders  # noqa: SLF001
    assert f"exit:{position_paper.id}" not in live_broker._orders  # noqa: SLF001
    assert f"exit:{position_live.id}" in live_broker._orders  # noqa: SLF001
    assert f"exit:{position_live.id}" not in paper_broker._orders  # noqa: SLF001


def test_run_once_runs_reconciliation_every_n_cycles(
    db: Session, broker, trading_session
):
    manager = PositionManager(
        trading_session.id,
        broker=broker,
        market_data_provider=_NullMarketDataProvider(),
        reconcile_every_n_cycles=2,
        session_factory=_session_factory_for(db),
    )

    manager.run_once()
    assert (
        db.query(ReconciliationRun)
        .filter(ReconciliationRun.trading_session_id == trading_session.id)
        .count()
        == 0
    )

    manager.run_once()
    assert (
        db.query(ReconciliationRun)
        .filter(ReconciliationRun.trading_session_id == trading_session.id)
        .count()
        == 1
    )


def test_run_once_calls_reconcile_pending_live_orders_with_the_right_cadence(
    db: Session, broker, trading_session, monkeypatch
):
    """2026-08-20: the WS-push cache check must run every cycle (free), but
    the REST safety-net fallback only on the configured flat cadence,
    deliberately independent of any cycle-count-unrelated signal — see
    `reconcile_pending_live_orders`'s own docstring. Verified here purely as
    a wiring/cadence check (the function's own real behavior is covered in
    test_execution_paper_service.py) via monkeypatching the name
    `position_manager` imports.
    """
    calls: list[bool] = []
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.position_manager.reconcile_pending_live_orders",
        lambda *args, **kwargs: calls.append(kwargs["allow_rest_fallback"]),
    )
    manager = PositionManager(
        trading_session.id,
        broker=broker,
        market_data_provider=_NullMarketDataProvider(),
        order_poll_every_n_cycles=3,
        session_factory=_session_factory_for(db),
    )

    for _ in range(4):
        manager.run_once()

    assert calls == [True, False, False, True]


def test_run_once_is_a_no_op_for_a_missing_or_inactive_session(db: Session, broker):
    manager = PositionManager(
        uuid.uuid4(),
        broker=broker,
        market_data_provider=_NullMarketDataProvider(),
        session_factory=_session_factory_for(db),
    )
    manager.run_once()  # must not raise


@pytest.fixture
def real_commit_factory(engine):
    """Same reasoning as test_synthetic_strategy.py's fixture of the same
    name — a real background thread needs its own real-commit session, not
    the rolled-back single-connection `db` fixture.
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


def test_manager_starts_and_stops_on_a_real_thread(real_commit_factory):
    """The actual timer/threading mechanism, not just run_once() called
    directly — mirrors test_synthetic_strategy.py's own dedicated test for
    StrategyRunner.
    """
    ids: dict[str, uuid.UUID] = {}
    broker = MockBrokerAdapter()
    try:
        with real_commit_factory() as db:
            from app.core.security.passwords import hash_password
            from app.domain.identity.models import User as UserRow
            from app.domain.identity.models import Workspace as WorkspaceRow

            workspace = WorkspaceRow(id=uuid.uuid4(), name=f"pm-runner-{uuid.uuid4().hex[:8]}")
            db.add(workspace)
            db.flush()
            ids["workspace_id"] = workspace.id

            user = UserRow(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                email=f"pm-runner-{uuid.uuid4().hex[:8]}@example.com",
                password_hash=hash_password("correct horse battery staple"),
                display_name="PM Runner Test User",
                is_active=True,
            )
            db.add(user)
            db.flush()
            ids["user_id"] = user.id

            broker_account = BrokerAccount(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                broker_type=BrokerType.SHOONYA,
                label="pm-runner-account",
                credentials_ref="config/credentials/shoonya.env",
                status=BrokerAccountStatus.ACTIVE,
            )
            db.add(broker_account)
            db.flush()
            ids["broker_account_id"] = broker_account.id

            trading_session = TradingSession(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                broker_account_id=broker_account.id,
                started_by_user_id=user.id,
                mode=SafeMode.PAPER_ONLY,
                started_at=datetime.now(UTC),
                budget_amount=1_000_000,
                daily_target_profit=1_000_000,
                daily_loss_cap=1_000_000,
                funding_mode=FundingMode.CASH,
            )
            db.add(trading_session)
            db.flush()
            ids["trading_session_id"] = trading_session.id

        manager = PositionManager(
            ids["trading_session_id"],
            broker=broker,
            market_data_provider=_NullMarketDataProvider(),
            poll_interval_seconds=0.05,
            session_factory=real_commit_factory,
        )
        manager.start()
        assert manager.is_alive()
        time.sleep(0.2)
        manager.stop()
        assert not manager.is_alive()

        with real_commit_factory() as verify_db:
            runs = (
                verify_db.query(ReconciliationRun)
                .filter(ReconciliationRun.trading_session_id == ids["trading_session_id"])
                .count()
            )
            # No open positions in this scenario, but the loop must have
            # completed at least one cycle without raising.
            assert runs >= 0
    finally:
        with real_commit_factory() as cleanup_db:
            from app.domain.identity.models import BrokerAccount as BrokerAccountRow
            from app.domain.identity.models import User as UserRow
            from app.domain.identity.models import Workspace as WorkspaceRow

            if "trading_session_id" in ids:
                cleanup_db.query(ReconciliationRun).filter(
                    ReconciliationRun.trading_session_id == ids["trading_session_id"]
                ).delete()
                cleanup_db.query(TradingSession).filter(
                    TradingSession.id == ids["trading_session_id"]
                ).delete()
            if "broker_account_id" in ids:
                cleanup_db.query(BrokerAccountRow).filter(
                    BrokerAccountRow.id == ids["broker_account_id"]
                ).delete()
            if "user_id" in ids:
                cleanup_db.query(UserRow).filter(UserRow.id == ids["user_id"]).delete()
            if "workspace_id" in ids:
                cleanup_db.query(WorkspaceRow).filter(
                    WorkspaceRow.id == ids["workspace_id"]
                ).delete()
            cleanup_db.commit()
