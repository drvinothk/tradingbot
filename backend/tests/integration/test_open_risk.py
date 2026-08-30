"""compute_position_open_risk (execution_engine.paper.exit_legs) — real-money
"how much do I lose if every open stop hits right now" figure used by the
Control Room UI's "Today's Activity" card. Covers the legacy single-
StopPlan/TrailPlan path and the multi-leg PositionExitLeg path, plus the
"no stop data at all" -> None case (distinct from a genuine 0).
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
from app.modules.execution_engine.paper.exit_legs import compute_position_open_risk


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="open-risk-test-account",
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
        symbol="NIFTY26JUL22000CE-RISK",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name="open-risk-test", strategy_type="orb"
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
        target_price=entry_price + 12,
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
        target_price=entry_price + 12,
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


def _stop_plan(db: Session, position: Position, *, stop_price: float, qty: int) -> StopPlan:
    now = datetime.now(UTC)
    plan = StopPlan(
        id=uuid.uuid4(),
        position_id=position.id,
        stop_price=stop_price,
        qty=qty,
        created_at=now,
        updated_at=now,
    )
    db.add(plan)
    db.flush()
    return plan


def _trail_plan(
    db: Session, position: Position, *, status: TrailPlanStatus, current_stop_price: float | None
) -> TrailPlan:
    plan = TrailPlan(
        id=uuid.uuid4(),
        position_id=position.id,
        trail_type="pct",
        activation_price=90.0,
        trail_value=0.5,
        current_stop_price=current_stop_price,
        status=status,
        updated_at=datetime.now(UTC),
    )
    db.add(plan)
    db.flush()
    return plan


def _leg(
    db: Session,
    position: Position,
    *,
    leg_index: int,
    qty: int,
    stop_price: float | None,
    trail_status: TrailPlanStatus = TrailPlanStatus.INACTIVE,
    trail_current_stop_price: float | None = None,
) -> PositionExitLeg:
    now = datetime.now(UTC)
    leg = PositionExitLeg(
        id=uuid.uuid4(),
        position_id=position.id,
        leg_index=leg_index,
        kind="custom",
        qty=qty,
        stop_price=stop_price,
        status=PositionExitLegStatus.OPEN,
        trail_status=trail_status,
        trail_current_stop_price=trail_current_stop_price,
        created_at=now,
        updated_at=now,
    )
    db.add(leg)
    db.flush()
    return leg


class TestLegacyPath:
    def test_no_stop_data_returns_none(
        self, db: Session, trading_session, option_contract, strategy_run
    ):
        position = _make_position(db, trading_session, option_contract, strategy_run)
        assert compute_position_open_risk(db, position) is None

    def test_fixed_stop_only(self, db: Session, trading_session, option_contract, strategy_run):
        position = _make_position(
            db, trading_session, option_contract, strategy_run, entry_price=80.0, qty=25
        )
        _stop_plan(db, position, stop_price=72.0, qty=25)
        # (80 - 72) * 25 = 200
        assert compute_position_open_risk(db, position) == pytest.approx(200.0)

    def test_active_trail_tighter_than_stop_wins(
        self, db: Session, trading_session, option_contract, strategy_run
    ):
        position = _make_position(
            db, trading_session, option_contract, strategy_run, entry_price=80.0, qty=25
        )
        _stop_plan(db, position, stop_price=72.0, qty=25)
        _trail_plan(db, position, status=TrailPlanStatus.ACTIVE, current_stop_price=76.0)
        # trail (76) is tighter than stop (72) -> (80 - 76) * 25 = 100
        assert compute_position_open_risk(db, position) == pytest.approx(100.0)

    def test_inactive_trail_falls_back_to_stop_price(
        self, db: Session, trading_session, option_contract, strategy_run
    ):
        position = _make_position(
            db, trading_session, option_contract, strategy_run, entry_price=80.0, qty=25
        )
        _stop_plan(db, position, stop_price=72.0, qty=25)
        _trail_plan(db, position, status=TrailPlanStatus.INACTIVE, current_stop_price=76.0)
        # trail exists but not ACTIVE -> ignored, uses stop_price (72)
        assert compute_position_open_risk(db, position) == pytest.approx(200.0)


class TestMultiLegPath:
    def test_mix_of_trailing_and_fixed_stop_legs_sums_correctly(
        self, db: Session, trading_session, option_contract, strategy_run
    ):
        position = _make_position(
            db, trading_session, option_contract, strategy_run, entry_price=80.0, qty=25
        )
        _leg(db, position, leg_index=0, qty=10, stop_price=72.0)
        _leg(
            db,
            position,
            leg_index=1,
            qty=15,
            stop_price=70.0,
            trail_status=TrailPlanStatus.ACTIVE,
            trail_current_stop_price=77.0,
        )
        # leg0: (80-72)*10 = 80; leg1 trail wins over stop: (80-77)*15 = 45
        assert compute_position_open_risk(db, position) == pytest.approx(125.0)

    def test_leg_with_no_stop_configured_is_skipped_not_zeroed(
        self, db: Session, trading_session, option_contract, strategy_run
    ):
        position = _make_position(
            db, trading_session, option_contract, strategy_run, entry_price=80.0, qty=25
        )
        _leg(db, position, leg_index=0, qty=10, stop_price=None)
        _leg(db, position, leg_index=1, qty=15, stop_price=70.0)
        # leg0 contributes nothing (no stop); leg1: (80-70)*15 = 150
        assert compute_position_open_risk(db, position) == pytest.approx(150.0)

    def test_all_legs_with_no_stop_returns_none(
        self, db: Session, trading_session, option_contract, strategy_run
    ):
        position = _make_position(
            db, trading_session, option_contract, strategy_run, entry_price=80.0, qty=25
        )
        _leg(db, position, leg_index=0, qty=25, stop_price=None)
        assert compute_position_open_risk(db, position) is None

    def test_closed_leg_excluded_from_risk(
        self, db: Session, trading_session, option_contract, strategy_run
    ):
        position = _make_position(
            db, trading_session, option_contract, strategy_run, entry_price=80.0, qty=25
        )
        open_leg = _leg(db, position, leg_index=0, qty=10, stop_price=72.0)
        closed_leg = _leg(db, position, leg_index=1, qty=15, stop_price=70.0)
        closed_leg.status = PositionExitLegStatus.CLOSED
        db.flush()
        # only the open leg counts: (80-72)*10 = 80
        assert compute_position_open_risk(db, position) == pytest.approx(80.0)
        assert open_leg.status == PositionExitLegStatus.OPEN
