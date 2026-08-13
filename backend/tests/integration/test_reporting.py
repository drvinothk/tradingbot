"""Reporting v1 — `build_daily_report`/`build_scorecard` computed against
known, fully-controlled trades (dispatch + close driven directly, bypassing
Risk Service, with the mock broker's fill price explicitly forced at each
step) so every stat can be checked against a hand-computed expected value,
not just "some number came back". Requires real Postgres (dispatch/close
run under advisory locks, same reasoning as test_execution_paper_service.py).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from app.domain.execution.models import ExitReason, Position
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
from app.modules.execution_engine.paper.service import close_position, dispatch_trade_intent
from app.modules.reporting.service import build_daily_report, build_scorecard

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
        label="report-test-account",
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


def _trading_session(db: Session, workspace, broker_account, user: User) -> TradingSession:
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


def _strategy_config_and_run(
    db: Session, workspace, trading_session: TradingSession, user: User, name: str
) -> StrategyRun:
    config = StrategyConfig(id=uuid.uuid4(), workspace_id=workspace.id, name=name)
    db.add(config)
    db.flush()

    run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
    )
    db.add(run)
    db.flush()
    return run


def _contract(db: Session, instrument: Instrument, strike: float, tag: str) -> OptionContract:
    contract = OptionContract(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        expiry_date=EXPIRY,
        strike=strike,
        option_type=OptionType.CE,
        symbol=f"NIFTY26JUL{int(strike)}CE-{tag}",
    )
    db.add(contract)
    db.flush()
    return contract


def _closed_trade(
    db: Session,
    broker: MockBrokerAdapter,
    trading_session: TradingSession,
    strategy_run: StrategyRun,
    option_contract: OptionContract,
    *,
    entry_price: float,
    exit_price: float,
    intended_price: float,
    exit_reason: ExitReason,
) -> None:
    """Builds a Signal/TradeIntent directly (bypassing Risk Service, same as
    the other Phase 3 test files) and drives it through a real dispatch +
    close for exact, known realized_pnl/slippage numbers.

    Since the Stage 1 price-source fix, entry/exit fills come from
    `entry_price`/`intended_price` themselves (plus PAPER_FILL_SLIPPAGE_PCT,
    0.0 unless configured), not from MockBrokerAdapter's own independent
    price -- the `broker._prices[...]` assignments below are now a no-op as
    far as the actual fill goes; `exit_price` is accepted for call-site
    readability at existing callers but no longer drives the result (that's
    `intended_price`'s job now). Callers where `exit_price == intended_price`
    are unaffected either way.
    """
    now = datetime.now(UTC)
    broker._prices[option_contract.symbol] = entry_price  # noqa: SLF001 - no longer affects the fill

    signal = Signal(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        strategy_config_id=strategy_run.strategy_config_id,
        strategy_run_id=strategy_run.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        side=SignalSide.BUY,
        entry_price=entry_price,
        stop_price=entry_price - 10,
        target_price=entry_price + 10,
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
        entry_price=entry_price,
        stop_price=entry_price - 10,
        target_price=entry_price + 10,
        status=TradeIntentStatus.DISPATCHED,
        created_at=now,
        dispatched_at=now,
    )
    db.add(trade_intent)
    db.flush()

    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()

    broker._prices[option_contract.symbol] = exit_price  # noqa: SLF001 - no longer affects the fill
    outcome = close_position(
        db, trading_session, position, exit_reason, intended_price, broker=broker
    )
    assert outcome is not None


def test_build_daily_report_matches_hand_computed_stats(
    db: Session, broker, workspace, broker_account, instrument, user
):
    trading_session = _trading_session(db, workspace, broker_account, user)
    strategy_run = _strategy_config_and_run(
        db, workspace, trading_session, user, "daily-report-strategy"
    )

    # Trade A: win. Fills at its own intended_price (target, 120) -- exit_price
    # here is a no-op leftover, see _closed_trade's own docstring.
    _closed_trade(
        db, broker, trading_session, strategy_run, _contract(db, instrument, 22000, "A"),
        entry_price=100.0, exit_price=120.0, intended_price=120.0, exit_reason=ExitReason.TARGET,
    )
    # Trade B: loss. Fills at its own intended_price (stop, 92).
    _closed_trade(
        db, broker, trading_session, strategy_run, _contract(db, instrument, 22050, "B"),
        entry_price=100.0, exit_price=90.0, intended_price=92.0, exit_reason=ExitReason.STOP,
    )
    # Trade C: win. Fills at its own intended_price (target, 110).
    _closed_trade(
        db, broker, trading_session, strategy_run, _contract(db, instrument, 22100, "C"),
        entry_price=100.0, exit_price=115.0, intended_price=110.0, exit_reason=ExitReason.TARGET,
    )

    report = build_daily_report(db, trading_session)

    # A: (120-100)*25 = 500 win. B: (92-100)*25 = -200 loss.
    # C: (110-100)*25 = 250 win. PAPER_FILL_SLIPPAGE_PCT unset (0.0) in every
    # test run unless a test explicitly configures it, so every fill lands
    # exactly on its own intended_price and slippage is 0.0 throughout --
    # see test_paper_slippage.py (tests/unit) for the nonzero-slippage case.
    assert report.trading_session_id == trading_session.id
    assert report.trade_count == 3
    assert report.win_count == 2
    assert report.loss_count == 1
    assert report.win_rate == pytest.approx(2 / 3)
    assert report.avg_win == pytest.approx((500.0 + 250.0) / 2)
    assert report.avg_loss == pytest.approx(-200.0)
    assert report.profit_factor == pytest.approx(750.0 / 200.0)
    assert report.max_drawdown == pytest.approx(200.0)
    assert report.total_realized_pnl == pytest.approx(550.0)
    assert report.total_slippage == pytest.approx(0.0)
    assert report.signal_count == 3
    assert report.dispatched_count == 3
    assert report.filled_count == 3


def test_build_daily_report_with_no_trades_is_all_zero(
    db: Session, workspace, broker_account, user
):
    trading_session = _trading_session(db, workspace, broker_account, user)

    report = build_daily_report(db, trading_session)

    assert report.trade_count == 0
    assert report.win_rate == 0.0
    assert report.profit_factor is None
    assert report.max_drawdown == 0.0
    assert report.total_realized_pnl == 0.0


def test_build_scorecard_aggregates_across_sessions_for_the_same_strategy(
    db: Session, broker, workspace, broker_account, instrument, user
):
    session_1 = _trading_session(db, workspace, broker_account, user)
    session_2 = _trading_session(db, workspace, broker_account, user)

    config = StrategyConfig(id=uuid.uuid4(), workspace_id=workspace.id, name="scorecard-strategy")
    db.add(config)
    db.flush()

    run_1 = StrategyRun(
        id=uuid.uuid4(), strategy_config_id=config.id, trading_session_id=session_1.id,
        execution_mode=ExecutionMode.AUTO, status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC), started_by_user_id=user.id,
    )
    run_2 = StrategyRun(
        id=uuid.uuid4(), strategy_config_id=config.id, trading_session_id=session_2.id,
        execution_mode=ExecutionMode.AUTO, status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC), started_by_user_id=user.id,
    )
    db.add_all([run_1, run_2])
    db.flush()

    # One winning trade in each session, same strategy_config.
    _closed_trade(
        db, broker, session_1, run_1, _contract(db, instrument, 23000, "D"),
        entry_price=100.0, exit_price=110.0, intended_price=110.0, exit_reason=ExitReason.TARGET,
    )
    _closed_trade(
        db, broker, session_2, run_2, _contract(db, instrument, 23050, "E"),
        entry_price=100.0, exit_price=105.0, intended_price=105.0, exit_reason=ExitReason.TARGET,
    )

    scorecard = build_scorecard(db, config.id)

    assert scorecard.strategy_config_id == config.id
    assert scorecard.trade_count == 2
    assert scorecard.win_count == 2
    assert scorecard.total_realized_pnl == pytest.approx((10.0 + 5.0) * 25)
    assert scorecard.signal_count == 2
    assert scorecard.dispatched_count == 2
    assert scorecard.filled_count == 2


def test_build_scorecard_is_isolated_per_strategy_config(
    db: Session, broker, workspace, broker_account, instrument, user
):
    trading_session = _trading_session(db, workspace, broker_account, user)
    run_a = _strategy_config_and_run(db, workspace, trading_session, user, "strategy-a")
    run_b = _strategy_config_and_run(db, workspace, trading_session, user, "strategy-b")

    _closed_trade(
        db, broker, trading_session, run_a, _contract(db, instrument, 24000, "F"),
        entry_price=100.0, exit_price=110.0, intended_price=110.0, exit_reason=ExitReason.TARGET,
    )

    scorecard_a = build_scorecard(db, run_a.strategy_config_id)
    scorecard_b = build_scorecard(db, run_b.strategy_config_id)

    assert scorecard_a.trade_count == 1
    assert scorecard_b.trade_count == 0
    assert scorecard_b.profit_factor is None
