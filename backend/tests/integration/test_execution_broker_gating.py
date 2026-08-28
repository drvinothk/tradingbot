"""get_execution_broker's per-strategy (FORCE_PAPER inside a live_enabled
session) and position-aware (opened-live exits bypass SafeMode) gating --
the branches that need real DB rows (StrategyConfig.runtime_mode / Order.mode
via object_session), unlike the mode-only branches already covered in
tests/unit/test_broker_composition.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

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
from app.domain.ops.models import InstrumentFirewallConfig
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
from app.modules.broker_adapter import composition
from app.modules.broker_adapter.base.errors import ConfigurationError
from app.modules.broker_adapter.mock.adapter import MockBrokerAdapter
from tests.unit.test_broker_composition import _FakeRealBroker

EXPIRY_DATE = date(2026, 8, 18)


@pytest.fixture(autouse=True)
def _reset_broker():
    composition.reset_for_tests()
    yield
    composition.reset_for_tests()


def _allow_real_money(monkeypatch, value: bool) -> None:
    from app.config.settings import get_settings

    monkeypatch.setattr(get_settings().app, "allow_real_money_dispatch", value)


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="exec-gating-test-account",
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
        budget_amount=100_000,
        daily_target_profit=5_000,
        daily_loss_cap=5_000,
        funding_mode=FundingMode.CASH,
    )
    db.add(ts)
    db.flush()
    return ts


def _strategy_run(
    db: Session,
    *,
    workspace,
    trading_session,
    user,
    runtime_mode: StrategyRuntimeMode | None = None,
    instrument_id: uuid.UUID | None = None,
) -> StrategyRun:
    config = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        name=f"orb-{uuid.uuid4().hex[:6]}",
        runtime_mode=runtime_mode,
    )
    db.add(config)
    db.flush()
    run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=trading_session.started_at,
        started_by_user_id=user.id,
        instrument_id=instrument_id,
    )
    db.add(run)
    db.flush()
    return run


# -- live_enabled + per-strategy FORCE_PAPER gating --------------------


def test_live_session_plain_strategy_and_flag_returns_real(
    db: Session, workspace, trading_session, user, monkeypatch
):
    _allow_real_money(monkeypatch, True)
    composition.set_broker(_FakeRealBroker())
    run = _strategy_run(db, workspace=workspace, trading_session=trading_session, user=user)

    broker = composition.get_execution_broker(trading_session, run)

    assert not isinstance(broker, MockBrokerAdapter)


def test_live_session_plain_strategy_but_no_flag_raises(
    db: Session, workspace, trading_session, user, monkeypatch
):
    _allow_real_money(monkeypatch, False)
    composition.set_broker(_FakeRealBroker())
    run = _strategy_run(db, workspace=workspace, trading_session=trading_session, user=user)

    with pytest.raises(ConfigurationError):
        composition.get_execution_broker(trading_session, run)


def test_live_session_with_force_paper_strategy_returns_mock(
    db: Session, workspace, trading_session, user, monkeypatch
):
    """`runtime_mode.FORCE_PAPER` holds a single strategy on paper even in a
    `live_enabled` session, with the flag on and a real broker connected --
    the "restricts, never expands" contract. This is now the *only*
    per-strategy live/paper lever (the `StrategyStatus` graduation ladder
    was retired 2026-08-28).
    """
    _allow_real_money(monkeypatch, True)
    composition.set_broker(_FakeRealBroker())
    run = _strategy_run(
        db,
        workspace=workspace,
        trading_session=trading_session,
        user=user,
        runtime_mode=StrategyRuntimeMode.FORCE_PAPER,
    )

    broker = composition.get_execution_broker(trading_session, run)

    assert isinstance(broker, MockBrokerAdapter)


# -- Ops-Hardening Phase 7: instrument firewall gating -------------------


@pytest.fixture
def banknifty(db: Session) -> Instrument:
    inst = Instrument(
        id=uuid.uuid4(), symbol="BANKNIFTY", exchange="NFO", lot_size=15, tick_size=0.05
    )
    db.add(inst)
    db.flush()
    return inst


def test_live_session_with_instrument_on_firewall_returns_real(
    db: Session, workspace, trading_session, user, monkeypatch
):
    _allow_real_money(monkeypatch, True)
    composition.set_broker(_FakeRealBroker())
    nifty = Instrument(id=uuid.uuid4(), symbol="NIFTY", exchange="NFO", lot_size=25, tick_size=0.05)
    db.add(nifty)
    db.flush()
    db.add(InstrumentFirewallConfig(workspace_id=workspace.id, active_live_instruments=["NIFTY"]))
    db.flush()
    run = _strategy_run(
        db,
        workspace=workspace,
        trading_session=trading_session,
        user=user,
        instrument_id=nifty.id,
    )

    broker = composition.get_execution_broker(trading_session, run)

    assert not isinstance(broker, MockBrokerAdapter)


def test_live_session_with_instrument_not_on_firewall_raises(
    db: Session, workspace, trading_session, user, banknifty, monkeypatch
):
    _allow_real_money(monkeypatch, True)
    composition.set_broker(_FakeRealBroker())
    db.add(InstrumentFirewallConfig(workspace_id=workspace.id, active_live_instruments=["NIFTY"]))
    db.flush()
    run = _strategy_run(
        db,
        workspace=workspace,
        trading_session=trading_session,
        user=user,
        instrument_id=banknifty.id,
    )

    with pytest.raises(ConfigurationError, match="firewall"):
        composition.get_execution_broker(trading_session, run)


def test_live_session_with_no_firewall_row_defaults_to_nifty_only(
    db: Session, workspace, trading_session, user, banknifty, monkeypatch
):
    # No InstrumentFirewallConfig row seeded at all -- must fall back to
    # DEFAULT_ACTIVE_LIVE_INSTRUMENTS ("NIFTY",), never "everything allowed".
    _allow_real_money(monkeypatch, True)
    composition.set_broker(_FakeRealBroker())
    run = _strategy_run(
        db,
        workspace=workspace,
        trading_session=trading_session,
        user=user,
        instrument_id=banknifty.id,
    )

    with pytest.raises(ConfigurationError, match="firewall"):
        composition.get_execution_broker(trading_session, run)


def test_live_session_with_no_instrument_id_on_run_skips_firewall_check(
    db: Session, workspace, trading_session, user, monkeypatch
):
    # A strategy_run predating the instrument_id column (still NULL) --
    # nothing to check against, so the firewall doesn't block it (same
    # "skip, don't crash on legacy NULL data" reasoning as _resume_
    # strategy_runners's own instrument_id/expiry_date NULL check.
    _allow_real_money(monkeypatch, True)
    composition.set_broker(_FakeRealBroker())
    run = _strategy_run(
        db,
        workspace=workspace,
        trading_session=trading_session,
        user=user,
        instrument_id=None,
    )

    broker = composition.get_execution_broker(trading_session, run)

    assert not isinstance(broker, MockBrokerAdapter)


# -- position-aware exit gating ------------------------------------------


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
        expiry_date=EXPIRY_DATE,
        strike=24000,
        option_type=OptionType.CE,
        symbol="NIFTY18AUG26C24000",
    )
    db.add(contract)
    db.flush()
    return contract


def _position(
    db: Session, *, workspace, trading_session, option_contract, user, order_mode: OrderMode
) -> Position:
    """A minimal Signal -> TradeIntent -> Order(open) -> Position chain --
    only `opening_order.mode` is semantically exercised by these tests, but
    `orders.ck_order_exactly_one_of_intent_or_position` requires a real
    `trade_intent_id` on the opening order regardless.
    """
    run = _strategy_run(
        db,
        workspace=workspace,
        trading_session=trading_session,
        user=user,
    )
    signal = Signal(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        strategy_config_id=run.strategy_config_id,
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
        idempotency_key=f"intent-{uuid.uuid4()}",
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
        idempotency_key=f"open-{uuid.uuid4()}",
        mode=order_mode,
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


def _pending_order(
    db: Session, *, workspace, trading_session, option_contract, user, order_mode: OrderMode
) -> Order:
    """A LIVE/PAPER `Order` that hasn't resolved into a `Position` yet --
    the exact `reconcile_pending_live_orders` scenario (a real broker fill
    still PENDING locally) that the `order`-aware escape hatch below exists
    for. Deliberately stops one step short of `_position`'s own chain (no
    `Position` row at all), matching that a `Position` genuinely doesn't
    exist yet at this point in the real flow.
    """
    run = _strategy_run(
        db,
        workspace=workspace,
        trading_session=trading_session,
        user=user,
    )
    signal = Signal(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        strategy_config_id=run.strategy_config_id,
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
        idempotency_key=f"intent-{uuid.uuid4()}",
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

    order = Order(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=intent.id,
        idempotency_key=f"open-{uuid.uuid4()}",
        mode=order_mode,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        qty=25,
        status=OrderStatus.PENDING,
        broker_order_id="26082500242002",
        submitted_at=trading_session.started_at,
        updated_at=trading_session.started_at,
    )
    db.add(order)
    db.flush()
    return order


def test_reconciliation_lock_still_returns_real_for_a_live_dispatched_order(
    db: Session, workspace, trading_session, option_contract, user, monkeypatch
):
    """The 2026-08-25 live incident, reproduced directly: a real Shoonya fill
    landed while the session was `live_enabled`, but by the time
    `reconcile_pending_live_orders` got to it the session had already
    transitioned to `reconciliation_lock` (the reconciliation service's own
    correct response to the very same fill not yet being reflected
    locally). Without the `order`-aware escape hatch, this would resolve
    the *mock* adapter for a real `broker_order_id`, raising `KeyError` and
    permanently blocking the fill from ever finalizing into a `Position`
    with a protective stop.
    """
    _allow_real_money(monkeypatch, True)
    composition.set_broker(_FakeRealBroker())
    trading_session.mode = SafeMode.RECONCILIATION_LOCK
    order = _pending_order(
        db,
        workspace=workspace,
        trading_session=trading_session,
        option_contract=option_contract,
        user=user,
        order_mode=OrderMode.LIVE,
    )

    broker = composition.get_execution_broker(trading_session, order=order)

    assert not isinstance(broker, MockBrokerAdapter)


def test_reconciliation_lock_still_returns_mock_for_a_paper_dispatched_order(
    db: Session, workspace, trading_session, option_contract, user, monkeypatch
):
    _allow_real_money(monkeypatch, True)
    composition.set_broker(_FakeRealBroker())
    trading_session.mode = SafeMode.RECONCILIATION_LOCK
    order = _pending_order(
        db,
        workspace=workspace,
        trading_session=trading_session,
        option_contract=option_contract,
        user=user,
        order_mode=OrderMode.PAPER,
    )

    broker = composition.get_execution_broker(trading_session, order=order)

    assert isinstance(broker, MockBrokerAdapter)


def test_live_dispatched_order_without_flag_raises_not_silently_mocked(
    db: Session, workspace, trading_session, option_contract, user, monkeypatch
):
    _allow_real_money(monkeypatch, False)
    trading_session.mode = SafeMode.RECONCILIATION_LOCK
    order = _pending_order(
        db,
        workspace=workspace,
        trading_session=trading_session,
        option_contract=option_contract,
        user=user,
        order_mode=OrderMode.LIVE,
    )

    with pytest.raises(ConfigurationError):
        composition.get_execution_broker(trading_session, order=order)


def test_kill_switch_still_returns_mock_for_a_paper_opened_position(
    db: Session, workspace, trading_session, option_contract, user, monkeypatch
):
    _allow_real_money(monkeypatch, True)
    composition.set_broker(_FakeRealBroker())
    trading_session.mode = SafeMode.KILL_SWITCH
    position = _position(
        db,
        workspace=workspace,
        trading_session=trading_session,
        option_contract=option_contract,
        user=user,
        order_mode=OrderMode.PAPER,
    )

    broker = composition.get_execution_broker(trading_session, position=position)

    assert isinstance(broker, MockBrokerAdapter)


def test_kill_switch_still_returns_real_for_a_live_opened_position(
    db: Session, workspace, trading_session, option_contract, user, monkeypatch
):
    # The core safety fix: kill_switch must never strand a genuinely-live
    # position with no way to close it for real.
    _allow_real_money(monkeypatch, True)
    composition.set_broker(_FakeRealBroker())
    trading_session.mode = SafeMode.KILL_SWITCH
    position = _position(
        db,
        workspace=workspace,
        trading_session=trading_session,
        option_contract=option_contract,
        user=user,
        order_mode=OrderMode.LIVE,
    )

    broker = composition.get_execution_broker(trading_session, position=position)

    assert not isinstance(broker, MockBrokerAdapter)


def test_live_opened_position_without_flag_raises_not_silently_mocked(
    db: Session, workspace, trading_session, option_contract, user, monkeypatch
):
    _allow_real_money(monkeypatch, False)
    trading_session.mode = SafeMode.KILL_SWITCH
    position = _position(
        db,
        workspace=workspace,
        trading_session=trading_session,
        option_contract=option_contract,
        user=user,
        order_mode=OrderMode.LIVE,
    )

    with pytest.raises(ConfigurationError):
        composition.get_execution_broker(trading_session, position=position)
