"""GET /orders' `intended_exit_reason` field -- added so the Control Room's
"Today's Trades" table can label an in-flight (not-yet-filled) exit order by
what's actually driving it (target/stop/trail/structure_break/manual/eod/...)
instead of a generic "Exit order" -- see buildTradeRows.ts's own use of it.

Pure passthrough: `Order.intended_exit_reason` already existed (set by
close_position at placement time, 2026-08-25) and is picked up automatically
by `OrderOut.model_validate(order)` since the field name matches the ORM
column -- no new write path, this just exercises that it actually reaches
the response. Also confirms the LIVE resting protective stop (order_type=
sl_limit) never sets it, which is exactly what lets the frontend use
order_type alone to identify that one case (see place_protective_stop's own
docstring -- it never passes intended_exit_reason to _new_order).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from app.api.v1.execution import list_orders
from app.domain.execution.models import (
    ExitReason,
    Order,
    OrderMode,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
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


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="intended-exit-reason-test-account",
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
        mode=SafeMode.LIVE_ENABLED,
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
        expiry_date=date(2026, 7, 30),
        strike=22000,
        option_type=OptionType.CE,
        symbol="NIFTY26JUL22000CE",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_run(
    db: Session, workspace, trading_session: TradingSession, user: User
) -> StrategyRun:
    config = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        name="intended-exit-reason-test",
        strategy_type="orb",
    )
    db.add(config)
    db.flush()

    run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.IN_POSITION,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
    )
    db.add(run)
    db.flush()
    return run


def _make_open_position(
    db: Session,
    trading_session: TradingSession,
    option_contract: OptionContract,
    strategy_run: StrategyRun,
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

    intent = TradeIntent(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        signal_id=signal.id,
        strategy_run_id=strategy_run.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        idempotency_key=f"intent:{uuid.uuid4()}",
        side=SignalSide.BUY,
        qty_lots=1,
        entry_price=80.0,
        stop_price=72.0,
        target_price=92.0,
        status=TradeIntentStatus.DISPATCHED,
        created_at=now,
        dispatched_at=now,
    )
    db.add(intent)
    db.flush()

    entry_order = Order(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=intent.id,
        idempotency_key=f"entry:{uuid.uuid4()}",
        mode=OrderMode.LIVE,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        qty=25,
        status=OrderStatus.FILLED,
        filled_qty=25,
        avg_fill_price=80.0,
        submitted_at=now,
        updated_at=now,
    )
    db.add(entry_order)
    db.flush()

    position = Position(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=intent.id,
        opening_order_id=entry_order.id,
        side=OrderSide.BUY,
        qty=25,
        entry_price=80.0,
        status=PositionStatus.OPEN,
        opened_at=now,
        closed_at=None,
    )
    db.add(position)
    db.flush()
    return position


def test_genuine_exit_attempt_exposes_its_intended_exit_reason(
    db: Session,
    trading_session: TradingSession,
    option_contract: OptionContract,
    strategy_run: StrategyRun,
    user: User,
):
    position = _make_open_position(db, trading_session, option_contract, strategy_run)
    now = datetime.now(UTC)
    exit_order = Order(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=None,
        position_id=position.id,
        idempotency_key=f"exit:{position.id}",
        mode=OrderMode.LIVE,
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        qty=25,
        status=OrderStatus.PENDING,
        filled_qty=0,
        avg_fill_price=None,
        submitted_at=now,
        updated_at=now,
        intended_exit_reason=ExitReason.TARGET,
    )
    db.add(exit_order)
    db.flush()

    orders = list_orders(trading_session_id=trading_session.id, db=db, user=user)
    row = next(o for o in orders if o.id == exit_order.id)

    assert row.order_type == "limit"
    assert row.intended_exit_reason == "target"


def test_resting_protective_stop_never_carries_an_intended_exit_reason(
    db: Session,
    trading_session: TradingSession,
    option_contract: OptionContract,
    strategy_run: StrategyRun,
    user: User,
):
    """The frontend relies on order_type='sl_limit' alone (never
    intended_exit_reason) to identify the resting protective stop -- this
    pins that place_protective_stop really does leave it unset, matching its
    own docstring's silence on the field.
    """
    position = _make_open_position(db, trading_session, option_contract, strategy_run)
    now = datetime.now(UTC)
    stop_order = Order(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=None,
        position_id=position.id,
        idempotency_key=f"stop:{position.id}",
        mode=OrderMode.LIVE,
        side=OrderSide.SELL,
        order_type=OrderType.SL_LIMIT,
        qty=25,
        status=OrderStatus.PENDING,
        filled_qty=0,
        avg_fill_price=None,
        submitted_at=now,
        updated_at=now,
    )
    db.add(stop_order)
    db.flush()

    orders = list_orders(trading_session_id=trading_session.id, db=db, user=user)
    row = next(o for o in orders if o.id == stop_order.id)

    assert row.order_type == "sl_limit"
    assert row.intended_exit_reason is None
