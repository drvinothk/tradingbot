"""GET /strategies/running's `last_signal` field (Market Terminal signal
panel, 2026-08-30) — populated from whatever `SignalStatus` the live
`StrategyRunner` thread's strategy object is currently carrying (see
`strategy_engine.interface.SignalStatus`'s own docstring), `None` when
there's nothing to report yet or no runner is registered for this run
(mirrors `data_freshness`'s own `runner is None` fallback immediately
above it in `list_running_strategies`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from app.api.v1.strategies import _RUNNERS, RunningStrategyOut, list_running_strategies
from app.domain.execution.models import (
    Order,
    OrderMode,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
    StopPlan,
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
from app.modules.strategy_engine.interface import SignalStatus, Strategy, TradeProposal
from app.modules.strategy_engine.runner import StrategyRunner

EXPIRY = date(2026, 7, 30)


class _FakeStrategy(Strategy):
    """Minimal `Strategy` stand-in — `StrategyRunner` only ever reads
    `.instrument_id`/`.expiry_date`/`.last_signal_status` off it, none of
    which requires a real `ConfirmationFilterStrategy` subclass.
    `evaluate` is never called in these tests (only construction +
    registration into `_RUNNERS`), so a stub satisfying the ABC is enough.
    """

    def __init__(self, instrument_id: uuid.UUID, expiry_date: date, last_signal_status):
        self.instrument_id = instrument_id
        self.expiry_date = expiry_date
        self.last_signal_status = last_signal_status

    def evaluate(self, db, strategy_run, latest_bar=None):
        raise NotImplementedError


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="last-signal-test-account",
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
def option_contract_ce(db: Session, instrument: Instrument) -> OptionContract:
    contract = OptionContract(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=EXPIRY,
        strike=22000, option_type=OptionType.CE, symbol="NIFTY26JUL22000CE-SIG",
    )
    db.add(contract)
    db.flush()
    return contract


def _make_session(db: Session, workspace, broker_account, user: User) -> TradingSession:
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


def _make_run(
    db: Session, workspace, trading_session: TradingSession, user: User, instrument: Instrument
) -> StrategyRun:
    config = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        name=f"last-signal-test-{uuid.uuid4().hex[:6]}",
        strategy_type="orb_conviction",
    )
    db.add(config)
    db.flush()

    run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=config.id,
        trading_session_id=trading_session.id,
        instrument_id=instrument.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
    )
    db.add(run)
    db.flush()
    return run


def _row_for(rows: list[RunningStrategyOut], run_id: uuid.UUID) -> RunningStrategyOut:
    return next(r for r in rows if r.strategy_run_id == run_id)


def test_last_signal_none_when_no_runner_registered(
    db: Session, workspace, broker_account, user: User, instrument: Instrument
):
    session = _make_session(db, workspace, broker_account, user)
    run = _make_run(db, workspace, session, user, instrument)

    rows = list_running_strategies(db=db, user=user)

    assert _row_for(rows, run.id).last_signal is None


def test_last_signal_populated_from_registered_runner(
    db: Session, workspace, broker_account, user: User, instrument: Instrument,
    option_contract_ce: OptionContract,
):
    session = _make_session(db, workspace, broker_account, user)
    run = _make_run(db, workspace, session, user, instrument)

    evaluated_at = datetime.now(UTC)
    candidate = TradeProposal(
        option_contract_id=option_contract_ce.id,
        side=SignalSide.BUY,
        qty_lots=1,
        entry_price=42.5,
        stop_price=35.0,
        target_price=60.0,
    )
    status = SignalStatus(
        reason_code="conviction_vix_above_band", candidate=candidate, evaluated_at=evaluated_at
    )
    runner = StrategyRunner(_FakeStrategy(instrument.id, EXPIRY, status), run.id)
    _RUNNERS[run.id] = runner
    try:
        rows = list_running_strategies(db=db, user=user)
        signal = _row_for(rows, run.id).last_signal

        assert signal is not None
        assert signal.reason_code == "conviction_vix_above_band"
        assert signal.option_contract_id == option_contract_ce.id
        assert signal.side == "CE"
        assert signal.strike == 22000.0
        assert signal.expiry_date == EXPIRY
        assert signal.symbol == "NIFTY26JUL22000CE-SIG"
        assert signal.planned_entry == 42.5
        assert signal.stop_price == 35.0
        assert signal.target_price == 60.0
    finally:
        _RUNNERS.pop(run.id, None)


def test_last_signal_none_when_run_is_in_position(
    db: Session, workspace, broker_account, user: User, instrument: Instrument,
    option_contract_ce: OptionContract,
):
    """A stale rejection reason must never show next to an open position --
    `_build_last_signal_out` gates on `StrategyRunStatus.SCANNING`, even if
    the registered runner's strategy object still has a non-empty
    `last_signal_status` left over from before the position opened.
    """
    session = _make_session(db, workspace, broker_account, user)
    run = _make_run(db, workspace, session, user, instrument)
    run.status = StrategyRunStatus.IN_POSITION
    db.add(run)
    db.flush()

    candidate = TradeProposal(
        option_contract_id=option_contract_ce.id,
        side=SignalSide.BUY,
        qty_lots=1,
        entry_price=42.5,
        stop_price=35.0,
        target_price=60.0,
    )
    status = SignalStatus(
        reason_code="conviction_vix_above_band", candidate=candidate, evaluated_at=datetime.now(UTC)
    )
    runner = StrategyRunner(_FakeStrategy(instrument.id, EXPIRY, status), run.id)
    _RUNNERS[run.id] = runner
    try:
        rows = list_running_strategies(db=db, user=user)
        assert _row_for(rows, run.id).last_signal is None
    finally:
        _RUNNERS.pop(run.id, None)


def test_last_signal_shows_position_detail_when_run_is_in_position(
    db: Session, workspace, broker_account, user: User, instrument: Instrument,
    option_contract_ce: OptionContract,
):
    """2026-08-31: the Candidate column went blank the instant a run became
    IN_POSITION even though the open position's own strike/expiry/entry/
    stop/target are already sitting in Position/TradeIntent/StopPlan — this
    covers `_build_position_signal_out`, which now sources exactly that
    data instead of returning `None`. Deliberately no `StrategyRunner`
    registered here (`_RUNNERS` left empty) — proving this path needs no
    live runner thread at all, unlike the SCANNING case above.
    """
    session = _make_session(db, workspace, broker_account, user)
    run = _make_run(db, workspace, session, user, instrument)
    run.status = StrategyRunStatus.IN_POSITION
    db.add(run)
    db.flush()

    now = datetime.now(UTC)
    entry_price, stop_price, target_price = 42.5, 35.0, 60.0
    signal = Signal(
        id=uuid.uuid4(), workspace_id=workspace.id, strategy_config_id=run.strategy_config_id,
        strategy_run_id=run.id, trading_session_id=session.id,
        option_contract_id=option_contract_ce.id, side=SignalSide.BUY,
        entry_price=entry_price, stop_price=stop_price, target_price=target_price,
        qty_lots=1, generated_at=now,
    )
    db.add(signal)
    db.flush()

    intent = TradeIntent(
        id=uuid.uuid4(), workspace_id=workspace.id, signal_id=signal.id,
        strategy_run_id=run.id, trading_session_id=session.id,
        option_contract_id=option_contract_ce.id, idempotency_key=f"test:{uuid.uuid4()}",
        side=SignalSide.BUY, qty_lots=1, entry_price=entry_price, stop_price=stop_price,
        target_price=target_price, status=TradeIntentStatus.DISPATCHED,
        created_at=now, dispatched_at=now,
    )
    db.add(intent)
    db.flush()

    order = Order(
        id=uuid.uuid4(), workspace_id=workspace.id, trading_session_id=session.id,
        option_contract_id=option_contract_ce.id, trade_intent_id=intent.id,
        idempotency_key=intent.idempotency_key, mode=OrderMode.PAPER, side=OrderSide.BUY,
        order_type=OrderType.MARKET, qty=25, status=OrderStatus.FILLED,
        filled_qty=25, avg_fill_price=entry_price, submitted_at=now, updated_at=now,
    )
    db.add(order)
    db.flush()

    position = Position(
        id=uuid.uuid4(), workspace_id=workspace.id, trading_session_id=session.id,
        option_contract_id=option_contract_ce.id, trade_intent_id=intent.id,
        opening_order_id=order.id, side=OrderSide.BUY, qty=25, entry_price=entry_price,
        status=PositionStatus.OPEN, opened_at=now, closed_at=None,
    )
    db.add(position)
    db.flush()

    # A trailed stop, tighter than the entry-time TradeIntent.stop_price --
    # the field under test should read this live value, not the stale one.
    trailed_stop_price = 38.0
    stop_plan = StopPlan(
        id=uuid.uuid4(), position_id=position.id, stop_price=trailed_stop_price, qty=25,
        created_at=now, updated_at=now,
    )
    db.add(stop_plan)
    db.flush()

    rows = list_running_strategies(db=db, user=user)
    signal_out = _row_for(rows, run.id).last_signal

    assert signal_out is not None
    assert signal_out.reason_code == "position_open"
    assert signal_out.option_contract_id == option_contract_ce.id
    assert signal_out.side == "CE"
    assert signal_out.strike == 22000.0
    assert signal_out.expiry_date == EXPIRY
    assert signal_out.symbol == "NIFTY26JUL22000CE-SIG"
    assert signal_out.planned_entry == entry_price
    assert signal_out.stop_price == trailed_stop_price
    assert signal_out.target_price == target_price
