"""compute_position_potential_profit (execution_engine.paper.exit_legs) --
real-money "how much do I gain if every open target hits right now" figure,
the profit-side mirror of compute_position_open_risk (see test_open_risk.py).
Deliberately NOT a literal mirror in implementation: the single-leg case's
target lives on TradeIntent.target_price, not StopPlan (which has no target
column at all) -- see compute_position_potential_profit's own docstring.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.domain.execution.models import (
    Order,
    OrderMode,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionExitLeg,
    PositionExitLegStatus,
    PositionStatus,
)
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
from app.modules.execution_engine.paper.exit_legs import compute_position_potential_profit


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="potential-profit-test-account",
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
        expiry_date="2026-07-30",
        strike=22000,
        option_type=OptionType.CE,
        symbol="NIFTY26JUL22000CE-PROFIT",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        name="potential-profit-test",
        strategy_type="orb",
    )
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


def _make_position(
    db: Session,
    trading_session,
    option_contract,
    strategy_run: StrategyRun,
    *,
    entry_price: float = 80.0,
    target_price: float = 92.0,
    qty: int = 25,
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
        entry_price=entry_price,
        stop_price=entry_price - 8,
        target_price=target_price,
        qty_lots=1,
        generated_at=now,
    )
    db.add(signal)
    db.flush()

    intent = TradeIntent(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        signal_id=signal.id,
        strategy_run_id=strategy_run.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        idempotency_key=f"test:{uuid.uuid4()}",
        side=SignalSide.BUY,
        qty_lots=1,
        entry_price=entry_price,
        stop_price=entry_price - 8,
        target_price=target_price,
        status=TradeIntentStatus.DISPATCHED,
        created_at=now,
        dispatched_at=now,
    )
    db.add(intent)
    db.flush()

    order = Order(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=intent.id,
        idempotency_key=intent.idempotency_key,
        mode=OrderMode.PAPER,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        qty=qty,
        status=OrderStatus.FILLED,
        filled_qty=qty,
        avg_fill_price=entry_price,
        submitted_at=now,
        updated_at=now,
    )
    db.add(order)
    db.flush()

    position = Position(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=intent.id,
        opening_order_id=order.id,
        side=OrderSide.BUY,
        qty=qty,
        entry_price=entry_price,
        status=PositionStatus.OPEN,
        opened_at=now,
        closed_at=None,
    )
    db.add(position)
    db.flush()
    return position


def _leg(
    db: Session,
    position: Position,
    *,
    leg_index: int,
    qty: int,
    target_price: float | None,
) -> PositionExitLeg:
    now = datetime.now(UTC)
    leg = PositionExitLeg(
        id=uuid.uuid4(),
        position_id=position.id,
        leg_index=leg_index,
        kind="custom",
        qty=qty,
        target_price=target_price,
        status=PositionExitLegStatus.OPEN,
        created_at=now,
        updated_at=now,
    )
    db.add(leg)
    db.flush()
    return leg


class TestLegacyPath:
    def test_reads_target_from_trade_intent(
        self, db: Session, trading_session, option_contract, strategy_run
    ):
        position = _make_position(
            db,
            trading_session,
            option_contract,
            strategy_run,
            entry_price=80.0,
            target_price=92.0,
            qty=25,
        )
        # (92 - 80) * 25 = 300
        assert compute_position_potential_profit(db, position) == pytest.approx(300.0)


class TestMultiLegPath:
    def test_sums_across_open_legs_with_a_target(
        self, db: Session, trading_session, option_contract, strategy_run
    ):
        position = _make_position(
            db, trading_session, option_contract, strategy_run, entry_price=80.0, qty=25
        )
        _leg(db, position, leg_index=0, qty=10, target_price=90.0)
        _leg(db, position, leg_index=1, qty=15, target_price=95.0)
        # leg0: (90-80)*10 = 100; leg1: (95-80)*15 = 225
        assert compute_position_potential_profit(db, position) == pytest.approx(325.0)

    def test_runner_leg_with_no_target_is_skipped_not_zeroed(
        self, db: Session, trading_session, option_contract, strategy_run
    ):
        position = _make_position(
            db, trading_session, option_contract, strategy_run, entry_price=80.0, qty=25
        )
        _leg(db, position, leg_index=0, qty=10, target_price=None)
        _leg(db, position, leg_index=1, qty=15, target_price=95.0)
        # leg0 contributes nothing (runner, no target); leg1: (95-80)*15 = 225
        assert compute_position_potential_profit(db, position) == pytest.approx(225.0)

    def test_all_legs_runners_returns_none(
        self, db: Session, trading_session, option_contract, strategy_run
    ):
        position = _make_position(
            db, trading_session, option_contract, strategy_run, entry_price=80.0, qty=25
        )
        _leg(db, position, leg_index=0, qty=25, target_price=None)
        assert compute_position_potential_profit(db, position) is None

    def test_closed_leg_excluded(
        self, db: Session, trading_session, option_contract, strategy_run
    ):
        position = _make_position(
            db, trading_session, option_contract, strategy_run, entry_price=80.0, qty=25
        )
        open_leg = _leg(db, position, leg_index=0, qty=10, target_price=90.0)
        closed_leg = _leg(db, position, leg_index=1, qty=15, target_price=95.0)
        closed_leg.status = PositionExitLegStatus.CLOSED
        db.flush()
        # only the open leg counts: (90-80)*10 = 100
        assert compute_position_potential_profit(db, position) == pytest.approx(100.0)
        assert open_leg.status == PositionExitLegStatus.OPEN
