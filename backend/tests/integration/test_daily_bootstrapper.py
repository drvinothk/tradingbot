"""Ops-Hardening Phase 4: app.modules.session.bootstrapper.run_daily_bootstrap
-- session lifecycle (safe auto-close of an empty stale session, alert-not-
close of a non-empty one, idempotent today's-session creation) against a
real DB. Scheduler trigger/throttle timing is covered separately in
tests/unit/test_daily_bootstrap_scheduler.py.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

import app.modules.session.bootstrapper as bootstrapper_module
from app.core.clock import IST
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
from app.domain.ops.models import SystemAlert
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
from app.modules.session.bootstrapper import run_daily_bootstrap

EXPIRY = date(2026, 8, 25)
TODAY = date(2026, 8, 18)


@pytest.fixture(autouse=True)
def _no_real_resume(monkeypatch):
    """`_resume_strategy_runners` is hardcoded to the real `session_scope`
    internally (no injectable factory of its own) -- must never run for
    real inside a test, same "don't let a background write default to the
    production DB inside a test" discipline as every other phase.
    """
    calls: list[None] = []
    monkeypatch.setattr(bootstrapper_module, "_resume_strategy_runners", lambda: calls.append(None))
    return calls


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(
        bootstrapper_module, "now_ist", lambda: datetime(2026, 8, 18, 9, 0, tzinfo=IST)
    )


def _same_session(db: Session):
    @contextmanager
    def _factory():
        yield db

    return _factory


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="bootstrap-test-account",
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
        expiry_date=EXPIRY,
        strike=24000,
        option_type=OptionType.CE,
        symbol="NIFTY25AUG26C24000",
    )
    db.add(contract)
    db.flush()
    return contract


def _yesterday_session(
    db: Session, *, workspace, broker_account, user: User, status=TradingSessionStatus.ACTIVE
) -> TradingSession:
    ts = TradingSession(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_account_id=broker_account.id,
        started_by_user_id=user.id,
        mode=SafeMode.PAPER_ONLY,
        status=status,
        started_at=datetime(2026, 8, 17, 4, 0, tzinfo=UTC),  # 2026-08-17 09:30 IST
        budget_amount=75_000,
        daily_target_profit=6_000,
        daily_loss_cap=6_000,
        funding_mode=FundingMode.MTF,
    )
    db.add(ts)
    db.flush()
    return ts


def _seed_open_position(
    db: Session, *, workspace, user: User, trading_session, option_contract
) -> Position:
    config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name=f"orb-{uuid.uuid4().hex[:6]}"
    )
    db.add(config)
    db.flush()

    run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.IN_POSITION,
        started_at=trading_session.started_at,
        started_by_user_id=user.id,
    )
    db.add(run)
    db.flush()

    signal = Signal(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        strategy_config_id=config.id,
        strategy_run_id=run.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        side=SignalSide.BUY,
        entry_price=80.0,
        stop_price=60.0,
        target_price=120.0,
        qty_lots=1,
        generated_at=trading_session.started_at,
    )
    db.add(signal)
    db.flush()

    intent = TradeIntent(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        signal_id=signal.id,
        strategy_run_id=run.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        idempotency_key=f"test-{uuid.uuid4()}",
        side=SignalSide.BUY,
        qty_lots=1,
        entry_price=80.0,
        stop_price=60.0,
        target_price=120.0,
        status=TradeIntentStatus.DISPATCHED,
        created_at=trading_session.started_at,
        dispatched_at=trading_session.started_at,
    )
    db.add(intent)
    db.flush()

    opening_order = Order(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=intent.id,
        idempotency_key=f"test-open-{uuid.uuid4()}",
        mode=OrderMode.PAPER,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        qty=25,
        status=OrderStatus.FILLED,
        filled_qty=25,
        avg_fill_price=80.0,
        submitted_at=trading_session.started_at,
        updated_at=trading_session.started_at,
    )
    db.add(opening_order)
    db.flush()

    position = Position(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=intent.id,
        opening_order_id=opening_order.id,
        side=OrderSide.BUY,
        qty=25,
        entry_price=80.0,
        status=PositionStatus.OPEN,
        opened_at=trading_session.started_at,
    )
    db.add(position)
    db.flush()
    return position


def test_empty_stale_session_is_auto_closed(db: Session, workspace, broker_account, user):
    stale = _yesterday_session(db, workspace=workspace, broker_account=broker_account, user=user)

    run_daily_bootstrap(session_factory=_same_session(db))

    db.refresh(stale)
    assert stale.status == TradingSessionStatus.ENDED
    assert stale.ended_at is not None


def test_stale_session_with_open_position_is_not_closed_and_alerts(
    db: Session, workspace, broker_account, user, instrument, option_contract
):
    stale = _yesterday_session(db, workspace=workspace, broker_account=broker_account, user=user)
    _seed_open_position(
        db, workspace=workspace, user=user, trading_session=stale, option_contract=option_contract
    )

    run_daily_bootstrap(session_factory=_same_session(db))

    db.refresh(stale)
    assert stale.status == TradingSessionStatus.ACTIVE  # untouched, not force-closed

    alerts = (
        db.query(SystemAlert).filter(SystemAlert.trading_session_id == stale.id).all()
    )
    assert len(alerts) == 1
    assert alerts[0].category == "stale_session_not_closed"


def test_stale_session_with_live_run_but_no_position_is_not_closed(
    db: Session, workspace, broker_account, user
):
    stale = _yesterday_session(db, workspace=workspace, broker_account=broker_account, user=user)
    config = StrategyConfig(id=uuid.uuid4(), workspace_id=workspace.id, name="orb-live-run")
    db.add(config)
    db.flush()
    db.add(
        StrategyRun(
            id=uuid.uuid4(),
            strategy_config_id=config.id,
            trading_session_id=stale.id,
            execution_mode=ExecutionMode.AUTO,
            status=StrategyRunStatus.SCANNING,
            started_at=stale.started_at,
            started_by_user_id=user.id,
        )
    )
    db.flush()

    run_daily_bootstrap(session_factory=_same_session(db))

    db.refresh(stale)
    assert stale.status == TradingSessionStatus.ACTIVE


def test_creates_todays_session_continuing_from_most_recent(
    db: Session, workspace, broker_account, user
):
    previous = _yesterday_session(
        db,
        workspace=workspace,
        broker_account=broker_account,
        user=user,
        status=TradingSessionStatus.ENDED,
    )

    run_daily_bootstrap(session_factory=_same_session(db))

    todays = (
        db.query(TradingSession)
        .filter(
            TradingSession.workspace_id == workspace.id,
            TradingSession.id != previous.id,
        )
        .all()
    )
    assert len(todays) == 1
    new_session = todays[0]
    assert new_session.status == TradingSessionStatus.ACTIVE
    assert new_session.broker_account_id == broker_account.id
    assert new_session.started_by_user_id == user.id
    assert new_session.funding_mode == FundingMode.MTF  # carried from previous session


def test_does_not_create_a_second_session_if_todays_already_exists(
    db: Session, workspace, broker_account, user
):
    todays_start = datetime(2026, 8, 18, 4, 0, tzinfo=UTC)  # 09:30 IST today
    existing = TradingSession(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_account_id=broker_account.id,
        started_by_user_id=user.id,
        mode=SafeMode.PAPER_ONLY,
        started_at=todays_start,
        budget_amount=50_000,
        daily_target_profit=5_000,
        daily_loss_cap=5_000,
        funding_mode=FundingMode.CASH,
    )
    db.add(existing)
    db.flush()

    run_daily_bootstrap(session_factory=_same_session(db))

    all_sessions = (
        db.query(TradingSession).filter(TradingSession.workspace_id == workspace.id).all()
    )
    assert len(all_sessions) == 1
    assert all_sessions[0].id == existing.id


def test_does_not_spawn_strategies_against_a_non_active_todays_session(
    db: Session, workspace, broker_account, user, monkeypatch
):
    """A human already ended today's session (kill_switch/manual end) before
    a restart re-ran this same-day bootstrap tick -- todays_session exists
    but isn't ACTIVE. Must not attach a fresh StrategyRun to it: _resume_
    strategy_runners only ever resumes runs on an ACTIVE session, so that
    run would be a zombie no runner thread ever picks up.
    """
    spawn_calls: list[None] = []
    monkeypatch.setattr(
        bootstrapper_module,
        "spawn_enabled_strategies",
        lambda *a, **kw: spawn_calls.append(None),
    )

    todays_start = datetime(2026, 8, 18, 4, 0, tzinfo=UTC)  # 09:30 IST today
    existing = TradingSession(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_account_id=broker_account.id,
        started_by_user_id=user.id,
        mode=SafeMode.PAPER_ONLY,
        status=TradingSessionStatus.ENDED,
        started_at=todays_start,
        ended_at=todays_start,
        budget_amount=50_000,
        daily_target_profit=5_000,
        daily_loss_cap=5_000,
        funding_mode=FundingMode.CASH,
    )
    db.add(existing)
    db.flush()

    run_daily_bootstrap(session_factory=_same_session(db))

    assert spawn_calls == []


def test_no_prior_session_skips_creation_without_error(db: Session, workspace):
    # A workspace with zero trading_session history ever -- run_daily_bootstrap
    # iterates distinct workspace_ids *from* trading_sessions, so this
    # workspace (created by the fixture but with no session rows) never even
    # enters the loop. Asserting this doesn't raise is the actual point.
    run_daily_bootstrap(session_factory=_same_session(db))

    assert db.query(TradingSession).filter(TradingSession.workspace_id == workspace.id).count() == 0


def test_resume_strategy_runners_is_called(
    db: Session, workspace, broker_account, user, _no_real_resume
):
    _yesterday_session(
        db,
        workspace=workspace,
        broker_account=broker_account,
        user=user,
        status=TradingSessionStatus.ENDED,
    )

    run_daily_bootstrap(session_factory=_same_session(db))

    assert len(_no_real_resume) == 1


def test_auto_spawner_runs_against_todays_freshly_created_session(
    db: Session, workspace, broker_account, user, instrument, monkeypatch
):
    """Ops-Hardening Phase 6: run_daily_bootstrap must hand the auto-spawner
    the *same* TradingSession it just created, not a stale/None reference --
    this is the actual bridge Phase 6 exists to build.
    """
    import app.modules.strategy_engine.auto_spawner as auto_spawner_module

    monkeypatch.setattr(auto_spawner_module, "is_shoonya_market_data_ready", lambda: True)
    monkeypatch.setattr(
        auto_spawner_module, "record_option_chain_snapshot", lambda *a, **kw: None
    )
    monkeypatch.setattr(auto_spawner_module, "get_broker", lambda: object())
    # auto_spawner has its own `now_ist` module binding, separate from
    # bootstrapper_module's (each did its own `from app.core.clock import
    # now_ist`) -- the _fixed_today fixture above only covers the latter,
    # so _spawn_one's own TRADE_WINDOW_END (15:09 IST) check needs its own
    # freeze too, or this test is only deterministic before 15:09 real time.
    monkeypatch.setattr(
        auto_spawner_module, "now_ist", lambda: datetime(2026, 8, 18, 11, 0, tzinfo=IST)
    )

    _yesterday_session(
        db,
        workspace=workspace,
        broker_account=broker_account,
        user=user,
        status=TradingSessionStatus.ENDED,
    )
    option_contract_fixture = OptionContract(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        expiry_date=date(2026, 8, 20),
        strike=24000,
        option_type=OptionType.CE,
        symbol=f"NIFTY-{uuid.uuid4().hex[:6]}",
    )
    db.add(option_contract_fixture)
    config = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        name="orb-nifty",
        strategy_type="orb",
        is_enabled=True,
        underlying_symbol="NIFTY",
    )
    db.add(config)
    db.flush()

    run_daily_bootstrap(session_factory=_same_session(db))

    new_session = (
        db.query(TradingSession)
        .filter(
            TradingSession.workspace_id == workspace.id,
            TradingSession.status == TradingSessionStatus.ACTIVE,
        )
        .one()
    )
    run = db.query(StrategyRun).filter(StrategyRun.strategy_config_id == config.id).one()
    assert run.trading_session_id == new_session.id
    assert run.expiry_date == date(2026, 8, 20)
