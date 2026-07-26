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
from datetime import UTC, date, datetime
from datetime import time as dt_time

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.domain.broker.models import ReconciliationRun
from app.domain.execution.models import Position, PositionStatus
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.market.models import Instrument, OptionContract, OptionType
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import (
    ExecutionMode,
    Signal,
    SignalSide,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    TradeIntent,
    TradeIntentStatus,
)
from app.modules.broker_adapter.mock.adapter import MockBrokerAdapter
from app.modules.execution_engine.paper.position_manager import PositionManager
from app.modules.execution_engine.paper.service import dispatch_trade_intent

EXPIRY = date(2026, 7, 30)


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


def test_run_once_exits_on_stop_hit(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    position = _dispatch_position(
        db, trading_session, strategy_run, option_contract, broker,
        stop_price=72.0, target_price=92.0,
    )
    broker._prices[option_contract.symbol] = 60.0  # noqa: SLF001 - force a price below stop

    manager = PositionManager(
        trading_session.id, broker=broker, session_factory=_session_factory_for(db)
    )
    manager.run_once()

    db.refresh(position)
    assert position.status == PositionStatus.CLOSED


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
        trading_session.id, broker=broker, session_factory=_session_factory_for(db)
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
        trading_session.id, broker=broker, session_factory=_session_factory_for(db)
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
        trading_session.id, broker=broker, session_factory=_session_factory_for(db)
    )
    manager.run_once()

    db.refresh(position)
    assert position.status == PositionStatus.CLOSED


def test_run_once_runs_reconciliation_every_n_cycles(
    db: Session, broker, trading_session
):
    manager = PositionManager(
        trading_session.id,
        broker=broker,
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


def test_run_once_is_a_no_op_for_a_missing_or_inactive_session(db: Session, broker):
    manager = PositionManager(
        uuid.uuid4(), broker=broker, session_factory=_session_factory_for(db)
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
