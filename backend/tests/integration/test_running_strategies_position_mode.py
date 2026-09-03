"""GET /strategies/running's `open_position.mode` field -- regression
coverage for a real production bug found live on 2026-09-03: the Control
Room's "Today's Activity" Live box scoped a run's still-open position by
`RunningStrategyOut.is_live` (a "would a *new* dispatch go live right now"
question, recomputed off *current* session mode + strategy config -- see
`is_strategy_routed_live`'s own docstring). A position opened while a
strategy was routed to paper (session `paper_only`, or the config was
`FORCE_PAPER` at the time) stays a genuine paper position for its whole
lifetime -- but once the session later flipped to `live_enabled` (or the
config's `runtime_mode` was cleared), `is_live` started reporting `True` for
that run even though its actual open position never touched real money.
The Control Room then leaked that paper position's non-zero
`potential_profit`/`open_risk` into the "Live" scope with zero real live
positions open (confirmed live: ₹13,130 phantom Potential Profit against a
session with 3 open positions, all genuinely paper).

`open_position.mode` fixes this by reading the position's own *opening*
Order.mode directly -- the same "never inferred from current session/config
state" ground truth `broker_adapter.composition._position_opened_live` and
`execution_engine.paper.service`'s own `order.mode *is* the per-position
live/paper signal` comment already establish for the broker-resolution and
protective-stop-placement paths. This file exercises the read-only `GET
/strategies/running` projection directly against that same ground truth,
independent of `is_live` (already covered by
`test_running_strategies_is_live.py`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from app.api.v1.strategies import RunningStrategyOut, list_running_strategies
from app.domain.execution.models import (
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
    StrategyRuntimeMode,
    TradeIntent,
    TradeIntentStatus,
)


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="position-mode-test-account",
        credentials_ref="config/credentials/shoonya.env",
        status=BrokerAccountStatus.ACTIVE,
    )
    db.add(account)
    db.flush()
    return account


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


def _make_session(db: Session, workspace, broker_account, user: User, *, mode: SafeMode):
    ts = TradingSession(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_account_id=broker_account.id,
        started_by_user_id=user.id,
        mode=mode,
        started_at=datetime.now(UTC),
        budget_amount=1_000_000,
        daily_target_profit=1_000_000,
        daily_loss_cap=1_000_000,
        funding_mode=FundingMode.CASH,
    )
    db.add(ts)
    db.flush()
    return ts


def _make_run(
    db: Session,
    workspace,
    trading_session: TradingSession,
    user: User,
    *,
    runtime_mode: StrategyRuntimeMode | None = None,
):
    config = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        name=f"position-mode-test-{uuid.uuid4().hex[:6]}",
        strategy_type="orb",
        runtime_mode=runtime_mode,
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


def _open_position(
    db: Session,
    trading_session: TradingSession,
    option_contract: OptionContract,
    strategy_run: StrategyRun,
    *,
    opening_order_mode: OrderMode,
) -> Position:
    """Full trade_intent -> order -> position chain, matching
    test_common_rules.py's own `_make_trade_intent`/`_make_position` shape --
    the opening Order's `mode` is the one thing this test varies.
    """
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
        idempotency_key=f"test:{uuid.uuid4()}",
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

    order = Order(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=intent.id,
        idempotency_key=intent.idempotency_key,
        mode=opening_order_mode,
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

    position = Position(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=intent.id,
        opening_order_id=order.id,
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


def _row_for(rows: list[RunningStrategyOut], run_id: uuid.UUID) -> RunningStrategyOut:
    return next(r for r in rows if r.strategy_run_id == run_id)


def test_paper_position_stays_paper_after_session_flips_to_live_enabled(
    db: Session, workspace, broker_account, option_contract, user: User
):
    """The exact live incident: a normal (not FORCE_PAPER) strategy's
    position was opened while paper -- e.g. the session was `paper_only` at
    dispatch time -- and is still open now that the session is
    `live_enabled`. `is_live` (current-config) is `True`, but the position
    itself must still report `mode == "paper"`.
    """
    session = _make_session(db, workspace, broker_account, user, mode=SafeMode.LIVE_ENABLED)
    run = _make_run(db, workspace, session, user)
    _open_position(
        db, session, option_contract, run, opening_order_mode=OrderMode.PAPER
    )

    rows = list_running_strategies(db=db, user=user)
    row = _row_for(rows, run.id)

    assert row.is_live is True  # current-config question, unchanged by this fix
    assert row.open_position is not None
    assert row.open_position.mode == "paper"  # the position's own real history


def test_live_position_reports_live_mode(
    db: Session, workspace, broker_account, option_contract, user: User
):
    session = _make_session(db, workspace, broker_account, user, mode=SafeMode.LIVE_ENABLED)
    run = _make_run(db, workspace, session, user)
    _open_position(
        db, session, option_contract, run, opening_order_mode=OrderMode.LIVE
    )

    rows = list_running_strategies(db=db, user=user)
    row = _row_for(rows, run.id)

    assert row.is_live is True
    assert row.open_position is not None
    assert row.open_position.mode == "live"


def test_force_paper_strategys_position_reports_paper_mode(
    db: Session, workspace, broker_account, option_contract, user: User
):
    """Sanity check for the common, non-buggy case: a FORCE_PAPER strategy's
    position is paper both by `is_live` (already covered by
    test_running_strategies_is_live.py) and by its own opening order mode.
    """
    session = _make_session(db, workspace, broker_account, user, mode=SafeMode.LIVE_ENABLED)
    run = _make_run(db, workspace, session, user, runtime_mode=StrategyRuntimeMode.FORCE_PAPER)
    _open_position(
        db, session, option_contract, run, opening_order_mode=OrderMode.PAPER
    )

    rows = list_running_strategies(db=db, user=user)
    row = _row_for(rows, run.id)

    assert row.is_live is False
    assert row.open_position is not None
    assert row.open_position.mode == "paper"


def test_no_open_position_leaves_open_position_none(
    db: Session, workspace, broker_account, user: User
):
    session = _make_session(db, workspace, broker_account, user, mode=SafeMode.LIVE_ENABLED)
    run = _make_run(db, workspace, session, user)
    # StrategyRun defaults to IN_POSITION in _make_run for the other tests'
    # sake, but no Position row exists here -- this covers the
    # opening_order-is-None branch of the new lookup.

    rows = list_running_strategies(db=db, user=user)
    row = _row_for(rows, run.id)

    assert row.open_position is None
