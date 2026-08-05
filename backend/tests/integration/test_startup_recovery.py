"""`app.main._run_startup_recovery_check` — the first real exercise of the
startup-recovery hook (a no-op stub since Phase 0): a trading_session left
`ACTIVE` with an open `Position`, as if the backend crashed mid-position,
must come back up with its `PositionManager` resumed and an immediate
reconciliation pass run, not idle. Requires real Postgres (dispatch/close
and reconciliation all run under advisory locks).

Also covers `app.main._resume_strategy_runners` — the equivalent gap for
`StrategyRunner` (an in-process thread with even less durable state than
`PositionManager`: before `StrategyRun.instrument_id`/`expiry_date` existed,
a restart could never resume a run even in principle). `StrategyRunner`
itself is monkeypatched to a no-op stand-in here, same reasoning
`test_api_strategies.py`'s `_FakeRunner` exists for: the real one spawns a
background thread against the *production* DB via its default
`session_factory=session_scope`.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from app.domain.broker.models import ReconciliationRun
from app.domain.execution.models import (
    Order,
    OrderMode,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
)
from app.domain.execution.models import Position as PositionRow
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.market.models import Instrument, OptionContract, OptionType
from app.domain.session.models import FundingMode, SafeMode, TradingSession, TradingSessionStatus
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
from app.modules.execution_engine.paper import registry
from app.modules.execution_engine.paper.registry import get_running_position_manager

EXPIRY = date(2026, 7, 30)


class _FakeStrategyRunner:
    """Records start/stop calls; never spawns a thread or touches any DB —
    same reasoning as test_api_strategies.py's own _FakeRunner.
    """

    instances: list[_FakeStrategyRunner] = []

    def __init__(self, strategy, strategy_run_id, interval_seconds=30.0, **kwargs):
        self.strategy = strategy
        self.strategy_run_id = strategy_run_id
        self.interval_seconds = interval_seconds
        self.started = False
        _FakeStrategyRunner.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        pass


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="startup-recovery-account",
        credentials_ref="config/credentials/shoonya.env",
        status=BrokerAccountStatus.ACTIVE,
    )
    db.add(account)
    db.flush()
    return account


@pytest.fixture(autouse=True)
def _stop_any_started_manager():
    """`ensure_position_manager_running` (called by the code under test)
    starts a real background thread — clean it up unconditionally after
    each test so nothing keeps polling in the background once the test's
    `db` session (and its rolled-back transaction) is gone.
    """
    yield
    registry.stop_all()


def _crashed_session_with_open_position(
    db: Session, workspace, broker_account, user: User
) -> TradingSession:
    """Builds an ACTIVE trading_session with a real open Position, entirely
    by direct DB inserts (not via dispatch_trade_intent) — standing in for
    "this is what the row shape looks like after a real crash", independent
    of whichever code path originally created it.
    """
    now = datetime.now(UTC)
    trading_session = TradingSession(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_account_id=broker_account.id,
        started_by_user_id=user.id,
        mode=SafeMode.PAPER_ONLY,
        status=TradingSessionStatus.ACTIVE,
        started_at=now,
        budget_amount=1_000_000,
        daily_target_profit=1_000_000,
        daily_loss_cap=1_000_000,
        funding_mode=FundingMode.CASH,
    )
    db.add(trading_session)
    db.flush()

    instrument = Instrument(
        id=uuid.uuid4(), symbol="NIFTY-RECOVERY", exchange="NFO", lot_size=25, tick_size=0.05
    )
    db.add(instrument)
    db.flush()

    option_contract = OptionContract(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        expiry_date=EXPIRY,
        strike=22000,
        option_type=OptionType.CE,
        symbol="NIFTY-RECOVERY-26JUL22000CE",
    )
    db.add(option_contract)
    db.flush()

    strategy_config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name="startup-recovery-strategy"
    )
    db.add(strategy_config)
    db.flush()

    strategy_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=now,
        started_by_user_id=user.id,
    )
    db.add(strategy_run)
    db.flush()

    signal = Signal(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        strategy_config_id=strategy_config.id,
        strategy_run_id=strategy_run.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        side=SignalSide.BUY,
        entry_price=80.0,
        stop_price=72.0,
        target_price=92.0,
        qty_lots=1,
        generated_at=now,
    )
    db.add(signal)
    db.flush()

    trade_intent = TradeIntent(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        signal_id=signal.id,
        strategy_run_id=strategy_run.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        idempotency_key=f"signal:{signal.id}",
        side=SignalSide.BUY,
        qty_lots=1,
        entry_price=80.0,
        stop_price=72.0,
        target_price=92.0,
        status=TradeIntentStatus.DISPATCHED,
        created_at=now,
        dispatched_at=now,
    )
    db.add(trade_intent)
    db.flush()

    order = Order(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=trade_intent.id,
        idempotency_key=trade_intent.idempotency_key,
        mode=OrderMode.PAPER,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        qty=25,
        status=OrderStatus.FILLED,
        filled_qty=25,
        avg_fill_price=80.0,
        submitted_at=now,
        updated_at=now,
    )
    db.add(order)
    db.flush()

    position = PositionRow(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=trade_intent.id,
        opening_order_id=order.id,
        side=OrderSide.BUY,
        qty=25,
        entry_price=80.0,
        status=PositionStatus.OPEN,
        opened_at=now,
    )
    db.add(position)
    db.flush()

    return trading_session


def test_startup_recovery_resumes_position_manager_and_reconciles(
    db: Session, workspace, broker_account, user, monkeypatch
):
    trading_session = _crashed_session_with_open_position(db, workspace, broker_account, user)

    # _run_startup_recovery_check does `from app.core.db.session import
    # session_scope` as a local import (late-bound), so patching the
    # attribute here is picked up at call time — it must see this test's
    # in-progress, rolled-back transaction, not the production DB.
    @contextmanager
    def _fake_session_scope():
        yield db

    import app.core.db.session as db_session_module

    monkeypatch.setattr(db_session_module, "session_scope", _fake_session_scope)

    from app.main import _run_startup_recovery_check

    _run_startup_recovery_check()

    manager = get_running_position_manager(trading_session.id)
    assert manager is not None, "PositionManager should have been resumed for the crashed session"
    assert manager.is_alive()

    run = (
        db.query(ReconciliationRun)
        .filter(ReconciliationRun.trading_session_id == trading_session.id)
        .one_or_none()
    )
    assert run is not None, "startup recovery should run an immediate reconciliation pass"


def test_startup_recovery_ignores_active_sessions_with_no_open_positions(
    db: Session, workspace, broker_account, user, monkeypatch
):
    now = datetime.now(UTC)
    trading_session = TradingSession(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_account_id=broker_account.id,
        started_by_user_id=user.id,
        mode=SafeMode.PAPER_ONLY,
        status=TradingSessionStatus.ACTIVE,
        started_at=now,
        budget_amount=1_000_000,
        daily_target_profit=1_000_000,
        daily_loss_cap=1_000_000,
        funding_mode=FundingMode.CASH,
    )
    db.add(trading_session)
    db.flush()

    @contextmanager
    def _fake_session_scope():
        yield db

    import app.core.db.session as db_session_module

    monkeypatch.setattr(db_session_module, "session_scope", _fake_session_scope)

    from app.main import _run_startup_recovery_check

    _run_startup_recovery_check()

    assert get_running_position_manager(trading_session.id) is None


@pytest.fixture(autouse=True)
def _clear_fake_strategy_runners():
    yield
    _FakeStrategyRunner.instances.clear()
    from app.api.v1.strategies import _RUNNERS

    _RUNNERS.clear()


def _patch_strategy_resume_collaborators(monkeypatch, db: Session):
    """Everything `_resume_strategy_runners` locally imports that would
    otherwise touch the production DB or spawn a real thread — mirrors
    `test_api_strategies.py`'s `fake_runner` fixture, patched at each
    collaborator's *source* module since the function under test re-imports
    them fresh on every call (late-bound local imports, not module-level).
    """

    @contextmanager
    def _fake_session_scope():
        yield db

    import app.core.db.session as db_session_module
    import app.modules.execution_engine.paper.registry as position_manager_registry
    import app.modules.market_data.registry as market_data_registry
    import app.modules.strategy_engine.runner as runner_module

    monkeypatch.setattr(db_session_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(runner_module, "StrategyRunner", _FakeStrategyRunner)
    monkeypatch.setattr(
        position_manager_registry, "ensure_position_manager_running", lambda *a, **k: None
    )
    monkeypatch.setattr(market_data_registry, "ensure_ingestion_running", lambda *a, **k: None)


def _active_session(db: Session, workspace, broker_account, user) -> TradingSession:
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
    return trading_session


def _scanning_run(
    db: Session, workspace, user, trading_session, *, expiry_date: date | None = None
) -> tuple[StrategyRun, Instrument]:
    """`expiry_date=None` (the default) builds a row with `instrument_id`/
    `expiry_date` both left NULL — standing in for a pre-migration row that
    can't be resumed. Pass `expiry_date=EXPIRY` for a fully resumable row.
    """
    strategy_config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name=f"resume-test-{uuid.uuid4().hex[:8]}"
    )
    db.add(strategy_config)
    db.flush()

    instrument = Instrument(
        id=uuid.uuid4(),
        symbol=f"RESUME-{uuid.uuid4().hex[:6]}",
        exchange="NFO",
        lot_size=25,
        tick_size=0.05,
    )
    db.add(instrument)
    db.flush()

    strategy_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
        instrument_id=instrument.id if expiry_date is not None else None,
        expiry_date=expiry_date,
    )
    db.add(strategy_run)
    db.flush()
    return strategy_run, instrument


def test_resume_strategy_runners_resumes_scanning_run_with_instrument_and_expiry(
    db: Session, workspace, broker_account, user, monkeypatch
):
    trading_session = _active_session(db, workspace, broker_account, user)
    strategy_run, instrument = _scanning_run(
        db, workspace, user, trading_session, expiry_date=EXPIRY
    )
    _patch_strategy_resume_collaborators(monkeypatch, db)

    from app.main import _resume_strategy_runners

    _resume_strategy_runners()

    from app.api.v1.strategies import _RUNNERS

    assert strategy_run.id in _RUNNERS
    resumed = _RUNNERS[strategy_run.id]
    assert isinstance(resumed, _FakeStrategyRunner)
    assert resumed.started is True
    assert resumed.strategy.instrument_id == instrument.id
    assert resumed.strategy.expiry_date == EXPIRY


def test_resume_strategy_runners_skips_runs_missing_instrument_id(
    db: Session, workspace, broker_account, user, monkeypatch
):
    """Simulates a row that predates the instrument_id/expiry_date columns —
    must be left alone (still non-STOPPED, but not resumed), not crash the
    whole resume pass or the rest of startup.
    """
    trading_session = _active_session(db, workspace, broker_account, user)
    strategy_run, _instrument = _scanning_run(db, workspace, user, trading_session)
    assert strategy_run.instrument_id is None
    assert strategy_run.expiry_date is None
    _patch_strategy_resume_collaborators(monkeypatch, db)

    from app.main import _resume_strategy_runners

    _resume_strategy_runners()

    from app.api.v1.strategies import _RUNNERS

    assert strategy_run.id not in _RUNNERS
    assert _FakeStrategyRunner.instances == []


def test_resume_strategy_runners_ignores_non_active_session(
    db: Session, workspace, broker_account, user, monkeypatch
):
    trading_session = TradingSession(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_account_id=broker_account.id,
        started_by_user_id=user.id,
        mode=SafeMode.PAPER_ONLY,
        status=TradingSessionStatus.ENDED,
        started_at=datetime.now(UTC),
        budget_amount=1_000_000,
        daily_target_profit=1_000_000,
        daily_loss_cap=1_000_000,
        funding_mode=FundingMode.CASH,
    )
    db.add(trading_session)
    db.flush()
    strategy_run, _instrument = _scanning_run(
        db, workspace, user, trading_session, expiry_date=EXPIRY
    )
    _patch_strategy_resume_collaborators(monkeypatch, db)

    from app.main import _resume_strategy_runners

    _resume_strategy_runners()

    from app.api.v1.strategies import _RUNNERS

    assert strategy_run.id not in _RUNNERS
    assert _FakeStrategyRunner.instances == []


def test_resume_strategy_runners_ignores_stopped_runs(
    db: Session, workspace, broker_account, user, monkeypatch
):
    trading_session = _active_session(db, workspace, broker_account, user)
    strategy_run, _instrument = _scanning_run(
        db, workspace, user, trading_session, expiry_date=EXPIRY
    )
    strategy_run.status = StrategyRunStatus.STOPPED
    db.add(strategy_run)
    db.flush()
    _patch_strategy_resume_collaborators(monkeypatch, db)

    from app.main import _resume_strategy_runners

    _resume_strategy_runners()

    from app.api.v1.strategies import _RUNNERS

    assert strategy_run.id not in _RUNNERS
    assert _FakeStrategyRunner.instances == []
