"""Reconciliation Service — diffs local `positions` against
`broker.get_positions()`. Requires real Postgres (`run_reconciliation` calls
`transition_mode`, which runs under `LOCK_EXECUTION_SINGLETON`, same
reasoning as the rest of this suite's DB-backed tests).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from app.domain.broker.models import BrokerSyncState, ReconciliationRun, ReconciliationTrigger
from app.domain.execution.models import Position
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.market.models import Instrument, OptionContract, OptionType
from app.domain.ops.models import SystemAlert
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
from app.modules.broker_adapter.base.contracts import OrderRequest, OrderSide, OrderType
from app.modules.broker_adapter.mock.adapter import MockBrokerAdapter
from app.modules.execution_engine.paper.service import dispatch_trade_intent
from app.modules.reconciliation.service import run_reconciliation

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
        label="recon-test-account",
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
        symbol="NIFTY26JUL22000CE-RC",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(id=uuid.uuid4(), workspace_id=workspace.id, name="recon-test-strategy")
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
        stop_price=72.0,
        target_price=92.0,
        qty_lots=1,
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
        stop_price=72.0,
        target_price=92.0,
        status=TradeIntentStatus.DISPATCHED,
        created_at=now,
        dispatched_at=now,
    )
    db.add(trade_intent)
    db.flush()

    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    return db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()


def test_run_reconciliation_clean_when_local_matches_broker(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    _dispatch_position(db, trading_session, strategy_run, option_contract, broker)

    run = run_reconciliation(db, broker, trading_session, ReconciliationTrigger.EVENT)

    assert run.mismatches_found == 0
    assert run.action_taken == "none"
    assert (
        db.query(SystemAlert)
        .filter(
            SystemAlert.trading_session_id == trading_session.id,
            SystemAlert.category == "reconciliation_mismatch",
        )
        .count()
        == 0
    )

    sync_state = (
        db.query(BrokerSyncState)
        .filter(
            BrokerSyncState.trading_session_id == trading_session.id,
            BrokerSyncState.option_contract_id == option_contract.id,
        )
        .one()
    )
    assert sync_state.local_qty == sync_state.broker_qty == 25
    assert sync_state.is_mismatched is False


def test_run_reconciliation_flags_an_injected_mismatch(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    _dispatch_position(db, trading_session, strategy_run, option_contract, broker)

    # Inject an inconsistency directly against the broker's own book — e.g.
    # a real broker fill the local system never recorded — independent of
    # execution_engine.paper.service, which is exactly what "an injected
    # inconsistency" means for this done-when criterion.
    broker.place_order(
        OrderRequest(
            idempotency_key=f"manual-injection-{uuid.uuid4()}",
            contract_symbol=option_contract.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            qty=25,
        )
    )

    run = run_reconciliation(db, broker, trading_session, ReconciliationTrigger.EVENT)

    assert run.mismatches_found == 1
    assert run.action_taken == "alert_raised"  # paper_only: flagged, not blocked

    alerts = db.query(SystemAlert).filter(
        SystemAlert.trading_session_id == trading_session.id,
        SystemAlert.category == "reconciliation_mismatch",
    ).all()
    assert len(alerts) == 1

    sync_state = (
        db.query(BrokerSyncState)
        .filter(
            BrokerSyncState.trading_session_id == trading_session.id,
            BrokerSyncState.option_contract_id == option_contract.id,
        )
        .one()
    )
    assert sync_state.is_mismatched is True
    assert sync_state.local_qty == 25
    assert sync_state.broker_qty == 50

    # paper_only has no live money at risk — flagged, but not mode-blocked.
    assert trading_session.mode == SafeMode.PAPER_ONLY


def test_run_reconciliation_enters_reconciliation_lock_from_guarded_live(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    db.add(trading_session)
    db.flush()

    _dispatch_position(db, trading_session, strategy_run, option_contract, broker)
    broker.place_order(
        OrderRequest(
            idempotency_key=f"manual-injection-{uuid.uuid4()}",
            contract_symbol=option_contract.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            qty=10,
        )
    )

    run = run_reconciliation(db, broker, trading_session, ReconciliationTrigger.POLL)

    assert run.mismatches_found == 1
    assert run.action_taken == "reconciliation_lock_entered"
    assert trading_session.mode == SafeMode.RECONCILIATION_LOCK


def test_run_reconciliation_records_a_run_row_even_when_clean(
    db: Session, broker, trading_session
):
    run_reconciliation(db, broker, trading_session, ReconciliationTrigger.POLL)

    stored = (
        db.query(ReconciliationRun)
        .filter(ReconciliationRun.trading_session_id == trading_session.id)
        .one()
    )
    assert stored.trigger_type == ReconciliationTrigger.POLL
    assert stored.mismatches_found == 0
