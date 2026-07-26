"""Paper Execution Service — dispatch, exit (stop/target/trail), and
idempotency. Requires a real Postgres (LOCK_EXECUTION_SINGLETON/
LOCK_RISK_EVALUATION_QUEUE advisory locks aren't meaningfully testable
against SQLite, same reasoning as test_risk_engine.py). Each test builds its
own `MockBrokerAdapter()` and passes it explicitly via `broker=` rather than
relying on the process-wide `get_broker()` singleton, so fill prices are
fully under the test's control instead of depending on global state.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from app.domain.execution.models import (
    ExitReason,
    Order,
    OrderStatus,
    Position,
    PositionStatus,
    StopPlan,
    TrailPlan,
    TrailPlanStatus,
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
from app.modules.broker_adapter.mock.adapter import MockBrokerAdapter
from app.modules.execution_engine.paper.service import (
    close_position,
    dispatch_trade_intent,
    evaluate_open_position,
)

EXPIRY = date(2026, 7, 30)


@pytest.fixture
def broker() -> MockBrokerAdapter:
    return MockBrokerAdapter()


def _price(value: float | None) -> float:
    assert value is not None
    return float(value)


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="exec-test-account",
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
        symbol="NIFTY26JUL22000CE-EXEC",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(id=uuid.uuid4(), workspace_id=workspace.id, name="exec-test-strategy")
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


def _make_trade_intent(
    db: Session,
    trading_session: TradingSession,
    strategy_run: StrategyRun,
    option_contract: OptionContract,
    *,
    side: SignalSide = SignalSide.BUY,
    entry_price: float = 80.0,
    stop_price: float = 72.0,
    target_price: float = 92.0,
    qty_lots: int = 1,
) -> TradeIntent:
    now = datetime.now(UTC)
    signal = Signal(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        strategy_config_id=strategy_run.strategy_config_id,
        strategy_run_id=strategy_run.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        side=side,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        qty_lots=qty_lots,
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
        side=side,
        qty_lots=qty_lots,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        status=TradeIntentStatus.DISPATCHED,
        created_at=now,
        dispatched_at=now,
    )
    db.add(trade_intent)
    db.flush()
    return trade_intent


# -- dispatch_trade_intent ----------------------------------------------------


def test_dispatch_creates_order_position_stop_and_trail(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)

    order = dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)

    assert order.status == OrderStatus.FILLED
    assert order.qty == 25  # qty_lots(1) x lot_size(25)
    assert order.trade_intent_id == trade_intent.id

    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    assert position.status == PositionStatus.OPEN
    assert position.qty == 25
    assert position.entry_price == pytest.approx(_price(order.avg_fill_price))

    stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one()
    assert float(stop_plan.stop_price) == pytest.approx(72.0)
    assert stop_plan.qty == 25

    trail_plan = db.query(TrailPlan).filter(TrailPlan.position_id == position.id).one()
    assert trail_plan.status == TrailPlanStatus.INACTIVE
    # Activation is halfway from entry to target (12.0 wide -> 6.0 in).
    assert float(trail_plan.activation_price) == pytest.approx(
        _price(order.avg_fill_price) + 6.0
    )


def test_dispatch_is_idempotent_on_trade_intent_key(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)

    first = dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    second = dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)

    assert first.id == second.id
    assert db.query(Order).filter(Order.trade_intent_id == trade_intent.id).count() == 1
    assert db.query(Position).filter(Position.trade_intent_id == trade_intent.id).count() == 1


# -- close_position -----------------------------------------------------------


def test_close_position_computes_pnl_and_slippage_on_stop(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    order = dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    entry_price = _price(order.avg_fill_price)

    # MockBrokerAdapter.place_order fills at the current cached price for
    # the symbol, which is otherwise unchanged between dispatch and close in
    # this test (nothing ticks it) — force a real price move so this test
    # exercises actual P&L/slippage math instead of a trivial 0.0 no-op.
    broker._prices[option_contract.symbol] = entry_price - 9.0  # noqa: SLF001

    outcome = close_position(
        db, trading_session, position, ExitReason.STOP, intended_price=72.0, broker=broker
    )

    assert outcome is not None
    db.refresh(position)
    assert position.status == PositionStatus.CLOSED
    assert position.closing_order_id is not None

    exit_fill_price = float(outcome.exit_price)
    assert exit_fill_price == pytest.approx(entry_price - 9.0)
    assert outcome.realized_pnl == pytest.approx((exit_fill_price - entry_price) * 25)
    assert outcome.realized_pnl == pytest.approx(-9.0 * 25)
    assert outcome.slippage == pytest.approx((exit_fill_price - 72.0) * 25)

    stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one()
    assert stop_plan.status == "triggered"


def test_close_position_is_idempotent_when_already_closed(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    first = close_position(
        db, trading_session, position, ExitReason.MANUAL, intended_price=80.0, broker=broker
    )
    assert first is not None

    second = close_position(
        db, trading_session, position, ExitReason.MANUAL, intended_price=80.0, broker=broker
    )
    assert second is None
    assert db.query(Order).filter(Order.position_id == position.id).count() == 1


def test_close_position_updates_session_pnl_and_can_trigger_kill_switch(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trading_session.daily_loss_cap = 1.0  # trivially small so any loss breaches it
    db.add(trading_session)
    db.flush()

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    order = dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    # Force a real loss (see the comment in the slippage test above for why
    # this is otherwise a no-op price move).
    broker._prices[option_contract.symbol] = _price(order.avg_fill_price) - 5.0  # noqa: SLF001

    outcome = close_position(
        db, trading_session, position, ExitReason.STOP, intended_price=72.0, broker=broker
    )

    assert outcome is not None
    assert outcome.realized_pnl < 0
    # record_trade_outcome_effects (risk_engine.service) is what updates
    # these — this is Phase 3's real replacement for
    # record_synthetic_outcome, now fed by an actual fill instead of a
    # random P&L.
    assert float(trading_session.cumulative_realized_pnl) == pytest.approx(outcome.realized_pnl)
    assert trading_session.consecutive_losses == 1
    assert trading_session.mode == SafeMode.KILL_SWITCH


# -- evaluate_open_position: stop/target/trail --------------------------------


def test_evaluate_open_position_exits_on_stop_hit(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    outcome = evaluate_open_position(db, trading_session, position, tick_price=71.0, broker=broker)

    assert outcome is not None
    assert outcome.exit_reason == ExitReason.STOP
    db.refresh(position)
    assert position.status == PositionStatus.CLOSED


def test_evaluate_open_position_exits_on_target_hit(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    outcome = evaluate_open_position(db, trading_session, position, tick_price=93.0, broker=broker)

    assert outcome is not None
    assert outcome.exit_reason == ExitReason.TARGET


def test_evaluate_open_position_no_exit_when_price_between_stop_and_target(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=80.0, stop_price=72.0, target_price=92.0,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    outcome = evaluate_open_position(db, trading_session, position, tick_price=81.0, broker=broker)

    assert outcome is None
    db.refresh(position)
    assert position.status == PositionStatus.OPEN


def test_evaluate_open_position_trail_activates_then_triggers(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    # entry/stop/target are deliberately anchored to the mock adapter's own
    # deterministic price for this symbol (not an arbitrary 80.0) — the mock
    # seeds a symbol's base price independently of whatever a test hardcodes
    # as "entry_price" on the TradeIntent, so a stop/target picked without
    # regard for that base price can end up already on the wrong side of it
    # (e.g. stop_price above the actual fill price), which is exactly what
    # broke this test's first version.
    real_price = broker._price_for(option_contract.symbol)  # noqa: SLF001
    entry_price = real_price
    stop_price = real_price - 8.0
    target_price = real_price + 20.0

    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract,
        entry_price=entry_price, stop_price=stop_price, target_price=target_price,
    )
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
    trail_plan = db.query(TrailPlan).filter(TrailPlan.position_id == position.id).one()
    activation_price = float(trail_plan.activation_price)  # entry + 10 (50% of the 20-wide range)
    assert activation_price == pytest.approx(entry_price + 10.0)

    # 1. Price reaches activation — trail activates, locks in 0 (exactly at
    # activation), no exit yet.
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=activation_price, broker=broker
    )
    assert outcome is None
    db.refresh(trail_plan)
    assert trail_plan.status == TrailPlanStatus.ACTIVE
    assert _price(trail_plan.current_stop_price) == pytest.approx(activation_price)

    # 2. Price advances further (+6 beyond activation) — trail tightens to
    # lock in half of that (+3), still no exit.
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=activation_price + 6.0, broker=broker
    )
    assert outcome is None
    db.refresh(trail_plan)
    assert _price(trail_plan.current_stop_price) == pytest.approx(activation_price + 3.0)

    # 3. Price pulls back through the trailed stop — exits via TRAIL, not STOP.
    outcome = evaluate_open_position(
        db, trading_session, position, tick_price=_price(trail_plan.current_stop_price) - 0.5,
        broker=broker,
    )
    assert outcome is not None
    assert outcome.exit_reason == ExitReason.TRAIL
