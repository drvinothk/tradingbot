"""Multi-leg (staged) exit engine — integration coverage against a real
Postgres + MockBrokerAdapter. Mirrors `test_execution_paper_service.py`'s
fixture style. Covers the plan's edge cases and QC findings for the
PAPER-path leg lifecycle.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from app.domain.execution.models import (
    ExitReason,
    OrderMode,
    Position,
    PositionExitLeg,
    PositionExitLegStatus,
    PositionStatus,
    StopPlan,
    TradeOutcome,
)
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.market.models import Instrument, OptionContract, OptionType
from app.domain.ops.models import SystemAlert
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.exit_legs import ExitLegSpec, serialize_exit_legs
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
from app.modules.execution_engine.paper.exit_legs import build_position_exit_legs
from app.modules.execution_engine.paper.service import (
    close_position,
    dispatch_trade_intent,
    evaluate_open_position,
)
from app.modules.reconciliation.service import _local_net_qty_by_symbol
from app.modules.reporting.service import build_scorecard
from app.modules.scheduler.eod_square_off import run_eod_square_off

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
        label="legs-test-account",
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
        symbol="NIFTY26JUL22000CE-LEGS",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(id=uuid.uuid4(), workspace_id=workspace.id, name="legs-test-strategy")
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


def _make_intent(
    db: Session,
    trading_session: TradingSession,
    strategy_run: StrategyRun,
    option_contract: OptionContract,
    *,
    entry_price: float = 80.0,
    stop_price: float = 72.0,
    target_price: float = 92.0,
    qty_lots: int = 10,
    exit_legs: list[ExitLegSpec] | None = None,
) -> TradeIntent:
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
        stop_price=stop_price,
        target_price=target_price,
        qty_lots=qty_lots,
        exit_legs=serialize_exit_legs(exit_legs),
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
        idempotency_key=f"signal:{signal.id}",
        side=SignalSide.BUY,
        qty_lots=qty_lots,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        exit_legs=serialize_exit_legs(exit_legs),
        status=TradeIntentStatus.DISPATCHED,
        created_at=now,
        dispatched_at=now,
    )
    db.add(intent)
    db.flush()
    return intent


def _three_legs() -> list[ExitLegSpec]:
    # 30% fixed SL + target, 30% S-R (tighter) target, 40% runner (no target).
    return [
        ExitLegSpec(qty_fraction=0.3, kind="fixed_sl", stop_price=72.0, target_price=92.0),
        ExitLegSpec(qty_fraction=0.3, kind="sr_target", stop_price=72.0, target_price=86.0),
        ExitLegSpec(qty_fraction=0.4, kind="runner", stop_price=72.0, target_price=None),
    ]


def _legs(db: Session, position_id: uuid.UUID) -> list[PositionExitLeg]:
    return (
        db.query(PositionExitLeg)
        .filter(PositionExitLeg.position_id == position_id)
        .order_by(PositionExitLeg.leg_index)
        .all()
    )


# --- leg creation ---------------------------------------------------------


def test_dispatch_creates_legs_and_no_stop_plan(
    db, broker, trading_session, strategy_run, option_contract
):
    intent = _make_intent(
        db, trading_session, strategy_run, option_contract, qty_lots=10, exit_legs=_three_legs()
    )
    order = dispatch_trade_intent(db, trading_session, intent, broker=broker)

    position = db.query(Position).filter(Position.trade_intent_id == intent.id).one()
    legs = _legs(db, position.id)
    assert [lg.qty for lg in legs] == [75, 75, 100]  # 3/3/4 lots * 25
    assert sum(lg.qty for lg in legs) == position.qty == order.filled_qty
    assert db.query(StopPlan).filter(StopPlan.position_id == position.id).one_or_none() is None
    assert legs[2].target_price is None  # runner


def test_too_small_position_collapses_to_legacy_with_alert(
    db, broker, trading_session, strategy_run, option_contract
):
    intent = _make_intent(
        db, trading_session, strategy_run, option_contract, qty_lots=2, exit_legs=_three_legs()
    )
    dispatch_trade_intent(db, trading_session, intent, broker=broker)

    position = db.query(Position).filter(Position.trade_intent_id == intent.id).one()
    assert _legs(db, position.id) == []
    assert db.query(StopPlan).filter(StopPlan.position_id == position.id).one() is not None
    alert = (
        db.query(SystemAlert)
        .filter(SystemAlert.category == "exit_legs_collapsed")
        .one_or_none()
    )
    assert alert is not None


def test_build_legs_returns_none_for_live_position(
    db, broker, trading_session, strategy_run, option_contract
):
    # A real paper position (real opening Order), then ask build_position_exit_legs
    # to treat it as LIVE — it must decline and alert rather than half-support it.
    intent = _make_intent(
        db, trading_session, strategy_run, option_contract, qty_lots=10, exit_legs=_three_legs()
    )
    dispatch_trade_intent(db, trading_session, intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == intent.id).one()
    # Wipe the legs that paper dispatch created so build_* runs from scratch.
    for lg in _legs(db, position.id):
        db.delete(lg)
    db.flush()

    out = build_position_exit_legs(
        db, trading_session, position, intent, filled_qty=250, lot_size=25, is_live=True
    )
    assert out is None
    assert (
        db.query(SystemAlert).filter(SystemAlert.category == "exit_legs_collapsed").count() == 1
    )


def test_leg_creation_is_idempotent(
    db, broker, trading_session, strategy_run, option_contract
):
    intent = _make_intent(
        db, trading_session, strategy_run, option_contract, qty_lots=10, exit_legs=_three_legs()
    )
    dispatch_trade_intent(db, trading_session, intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == intent.id).one()

    again = build_position_exit_legs(
        db, trading_session, position, intent, filled_qty=250, lot_size=25, is_live=False
    )
    assert again is not None and len(again) == 3
    assert len(_legs(db, position.id)) == 3  # no duplicates


# --- evaluation ---------------------------------------------------------


def test_leg_target_closes_only_that_leg(
    db, broker, trading_session, strategy_run, option_contract
):
    intent = _make_intent(
        db, trading_session, strategy_run, option_contract, qty_lots=10, exit_legs=_three_legs()
    )
    dispatch_trade_intent(db, trading_session, intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == intent.id).one()

    # Price hits leg1's tighter target (86) but not leg0's (92).
    evaluate_open_position(db, trading_session, position, tick_price=86.0, broker=broker)

    legs = _legs(db, position.id)
    assert legs[0].status == PositionExitLegStatus.OPEN
    assert legs[1].status == PositionExitLegStatus.CLOSED
    assert legs[1].exit_reason == ExitReason.TARGET
    assert legs[2].status == PositionExitLegStatus.OPEN
    assert position.status == PositionStatus.OPEN
    assert position.qty == 75 + 100  # leg1's 75 removed

    outcomes = db.query(TradeOutcome).filter(TradeOutcome.position_id == position.id).all()
    assert len(outcomes) == 1
    assert outcomes[0].position_exit_leg_id == legs[1].id
    assert float(outcomes[0].realized_pnl) == pytest.approx((86.0 - 80.0) * 75)


def test_all_legs_close_finalizes_position_once(
    db, broker, trading_session, strategy_run, option_contract
):
    intent = _make_intent(
        db, trading_session, strategy_run, option_contract, qty_lots=10, exit_legs=_three_legs()
    )
    dispatch_trade_intent(db, trading_session, intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == intent.id).one()

    # 1) leg1 target 86, 2) leg0 target 92, 3) leg2 runner has no target →
    # drop to its stop 72 to close it.
    evaluate_open_position(db, trading_session, position, tick_price=86.0, broker=broker)
    evaluate_open_position(db, trading_session, position, tick_price=92.0, broker=broker)
    evaluate_open_position(db, trading_session, position, tick_price=72.0, broker=broker)

    db.refresh(position)
    assert position.status == PositionStatus.CLOSED
    assert position.qty == 0
    legs = _legs(db, position.id)
    assert all(lg.status == PositionExitLegStatus.CLOSED for lg in legs)
    assert legs[2].exit_reason == ExitReason.STOP

    outcomes = db.query(TradeOutcome).filter(TradeOutcome.position_id == position.id).all()
    assert len(outcomes) == 3
    net = sum(float(o.realized_pnl) for o in outcomes)
    # leg0: (92-80)*75=900 ; leg1: (86-80)*75=450 ; leg2: (72-80)*100=-800
    assert net == pytest.approx(900 + 450 - 800)

    closed_events = [
        e
        for e in _audit_events(db, position.id)
        if e == "position.closed"
    ]
    assert closed_events.count("position.closed") == 1


def _audit_events(db: Session, entity_id: uuid.UUID) -> list[str]:
    from app.domain.audit.models import AuditEvent

    return [
        e.event_type
        for e in db.query(AuditEvent).filter(AuditEvent.entity_id == entity_id).all()
    ]


def test_runner_leg_survives_until_eod_square_off(
    db, broker, trading_session, strategy_run, option_contract
):
    intent = _make_intent(
        db, trading_session, strategy_run, option_contract, qty_lots=10, exit_legs=_three_legs()
    )
    dispatch_trade_intent(db, trading_session, intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == intent.id).one()

    # Close the two targeted legs.
    evaluate_open_position(db, trading_session, position, tick_price=92.0, broker=broker)
    db.refresh(position)
    assert position.status == PositionStatus.OPEN
    assert _legs(db, position.id)[2].status == PositionExitLegStatus.OPEN

    # EOD flattens the remaining runner leg via close_position's has-legs branch.
    run_eod_square_off(db, broker, trading_session)
    db.refresh(position)
    assert position.status == PositionStatus.CLOSED
    assert _legs(db, position.id)[2].exit_reason == ExitReason.EOD_SQUARE_OFF


def test_reconciliation_sees_shrinking_qty(
    db, broker, trading_session, strategy_run, option_contract
):
    intent = _make_intent(
        db, trading_session, strategy_run, option_contract, qty_lots=10, exit_legs=_three_legs()
    )
    dispatch_trade_intent(db, trading_session, intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == intent.id).one()

    before = _local_net_qty_by_symbol(db, trading_session.id, OrderMode.PAPER)
    assert before[option_contract.symbol][0] == 250

    evaluate_open_position(db, trading_session, position, tick_price=86.0, broker=broker)
    after = _local_net_qty_by_symbol(db, trading_session.id, OrderMode.PAPER)
    assert after[option_contract.symbol][0] == 175  # 250 - leg1's 75


def test_scorecard_counts_staged_trade_as_one(
    db, broker, trading_session, strategy_run, strategy_config, option_contract
):
    intent = _make_intent(
        db, trading_session, strategy_run, option_contract, qty_lots=10, exit_legs=_three_legs()
    )
    dispatch_trade_intent(db, trading_session, intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == intent.id).one()
    evaluate_open_position(db, trading_session, position, tick_price=86.0, broker=broker)
    evaluate_open_position(db, trading_session, position, tick_price=92.0, broker=broker)
    evaluate_open_position(db, trading_session, position, tick_price=72.0, broker=broker)

    card = build_scorecard(db, strategy_config.id)
    assert card.trade_count == 1
    assert card.filled_count == 1
    assert card.total_realized_pnl == pytest.approx(900 + 450 - 800)


def test_manual_close_position_flattens_all_legs(
    db, broker, trading_session, strategy_run, option_contract
):
    intent = _make_intent(
        db, trading_session, strategy_run, option_contract, qty_lots=10, exit_legs=_three_legs()
    )
    dispatch_trade_intent(db, trading_session, intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == intent.id).one()

    close_position(db, trading_session, position, ExitReason.MANUAL, 83.0, broker=broker)
    db.refresh(position)
    assert position.status == PositionStatus.CLOSED
    legs = _legs(db, position.id)
    assert all(lg.status == PositionExitLegStatus.CLOSED for lg in legs)
    assert all(lg.exit_reason == ExitReason.MANUAL for lg in legs)
    # All three legs filled at the one intended price → net vs entry 80.
    net = sum(float(o.realized_pnl) for o in db.query(TradeOutcome).filter(
        TradeOutcome.position_id == position.id
    ))
    assert net == pytest.approx((83.0 - 80.0) * 250)


# --- structure-break / trail per leg ------------------------------------


def test_leg_structure_break_closes_that_leg(
    db, broker, trading_session, strategy_run, option_contract, instrument
):
    from app.domain.market.models import PriceBar
    from app.modules.strategy_engine.common_rules import BAR_TIMEFRAME

    # A completed underlying bar below the structure level so the bar-close
    # confirmation (persistence 0 => instant) is satisfied.
    db.add(
        PriceBar(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            timeframe=BAR_TIMEFRAME,
            bucket_start=datetime.now(UTC),
            open=23990, high=23995, low=23980, close=23985,
            volume=1000,
        )
    )
    db.flush()

    legs = [
        ExitLegSpec(qty_fraction=0.5, kind="a", stop_price=72.0, target_price=92.0),
        ExitLegSpec(
            qty_fraction=0.5, kind="b", stop_price=72.0, target_price=92.0,
            structure_level=24000.0,
        ),
    ]
    intent = _make_intent(
        db, trading_session, strategy_run, option_contract, qty_lots=10, exit_legs=legs
    )
    dispatch_trade_intent(db, trading_session, intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == intent.id).one()

    evaluate_open_position(
        db, trading_session, position, tick_price=80.0, broker=broker,
        underlying_price=23985.0,
    )
    rows = _legs(db, position.id)
    assert rows[0].status == PositionExitLegStatus.OPEN  # no structure_level
    assert rows[1].status == PositionExitLegStatus.CLOSED
    assert rows[1].exit_reason == ExitReason.STRUCTURE_BREAK


def test_leg_trail_exit(
    db, broker, trading_session, strategy_run, option_contract
):
    legs = [
        ExitLegSpec(
            qty_fraction=0.5, kind="trailer", stop_price=72.0, target_price=92.0,
            trail_activation_fraction=0.5, trail_lock_fraction=0.5,
        ),
        ExitLegSpec(qty_fraction=0.5, kind="b", stop_price=72.0, target_price=92.0),
    ]
    intent = _make_intent(
        db, trading_session, strategy_run, option_contract, qty_lots=10, exit_legs=legs
    )
    dispatch_trade_intent(db, trading_session, intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == intent.id).one()

    # activation = 80 + 0.5*(92-80) = 86. At 88: trail stop = 86 + 0.5*(88-86) = 87.
    evaluate_open_position(db, trading_session, position, tick_price=88.0, broker=broker)
    rows = _legs(db, position.id)
    assert rows[0].trail_current_stop_price == pytest.approx(87.0)
    assert rows[0].status == PositionExitLegStatus.OPEN

    # Pull back through 87.
    evaluate_open_position(db, trading_session, position, tick_price=86.5, broker=broker)
    rows = _legs(db, position.id)
    assert rows[0].status == PositionExitLegStatus.CLOSED
    assert rows[0].exit_reason == ExitReason.TRAIL
    outcome = (
        db.query(TradeOutcome)
        .filter(TradeOutcome.position_exit_leg_id == rows[0].id)
        .one()
    )
    assert float(outcome.realized_pnl) == pytest.approx((87.0 - 80.0) * 125)


def test_config_template_becomes_concrete_specs_in_submit_signal(
    db, trading_session, strategy_run, strategy_config, option_contract
):
    from app.modules.strategy_engine.interface import TradeProposal
    from app.modules.strategy_engine.service import _apply_exit_leg_templates

    strategy_config.params = {
        "exit_legs": [
            {"qty_fraction": 0.4, "kind": "base", "stop_pct": 0.1},
            {"qty_fraction": 0.6, "kind": "runner", "no_target": True},
        ]
    }
    db.add(strategy_config)
    db.flush()

    proposal = TradeProposal(
        option_contract_id=option_contract.id,
        side=SignalSide.BUY,
        qty_lots=10,
        entry_price=100.0,
        stop_price=88.0,
        target_price=115.0,
    )
    out = _apply_exit_leg_templates(db, strategy_config, proposal)
    assert out.exit_legs is not None and len(out.exit_legs) == 2
    assert out.exit_legs[0].stop_price == pytest.approx(90.0)  # 100 * (1-0.1)
    assert out.exit_legs[0].target_price == 115.0  # base
    assert out.exit_legs[1].target_price is None  # runner


def test_positions_endpoint_does_not_fan_out_on_legs(
    db, broker, trading_session, strategy_run, option_contract
):
    from app.api.v1.execution import list_positions

    intent = _make_intent(
        db, trading_session, strategy_run, option_contract, qty_lots=10, exit_legs=_three_legs()
    )
    dispatch_trade_intent(db, trading_session, intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == intent.id).one()
    evaluate_open_position(db, trading_session, position, tick_price=86.0, broker=broker)
    evaluate_open_position(db, trading_session, position, tick_price=92.0, broker=broker)
    evaluate_open_position(db, trading_session, position, tick_price=72.0, broker=broker)

    class _U:
        workspace_id = None

    _U.workspace_id = trading_session.workspace_id
    rows = list_positions(trading_session.id, db=db, user=_U())  # type: ignore[arg-type]
    mine = [r for r in rows if r.id == position.id]
    assert len(mine) == 1  # one row, not three
    out = mine[0]
    assert len(out.legs) == 3
    assert out.exit_reason == "staged"
    assert out.realized_pnl == pytest.approx(900 + 450 - 800)
