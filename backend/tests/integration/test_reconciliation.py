"""Reconciliation Service — diffs local `positions` against
`broker.get_positions()`. Requires real Postgres (`run_reconciliation` calls
`transition_mode`, which runs under `LOCK_EXECUTION_SINGLETON`, same
reasoning as the rest of this suite's DB-backed tests).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from app.domain.broker.models import BrokerSyncState, ReconciliationRun, ReconciliationTrigger
from app.domain.execution.models import ExitReason, Position, PositionStatus, TradeOutcome
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.market.models import Instrument, OptionContract, OptionType
from app.domain.ops.models import SystemAlert
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
from app.modules.broker_adapter.base.contracts import (
    BrokerOrderStatus,
    OrderRequest,
    OrderSide,
    OrderType,
    TradeFill,
)
from app.modules.broker_adapter.base.contracts import Position as BrokerPosition
from app.modules.broker_adapter.mock.adapter import FillScenario, MockBrokerAdapter
from app.modules.execution_engine.paper.service import dispatch_trade_intent
from app.modules.reconciliation.service import run_full_reconciliation, run_reconciliation

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
        label="recon-test-account",
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
        symbol="NIFTY26JUL22000CE-RC",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(id=uuid.uuid4(), workspace_id=workspace.id, name="recon-test-strategy")
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


def _dispatch_position(
    db: Session,
    trading_session: TradingSession,
    strategy_run: StrategyRun,
    option_contract: OptionContract,
    broker: MockBrokerAdapter,
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
        entry_price=80.0,
        stop_price=72.0,
        target_price=92.0,
        status=TradeIntentStatus.DISPATCHED,
        created_at=now,
        dispatched_at=now,
    )
    db.add(trade_intent)
    db.flush()

    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    return db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()


def _dispatch_live_position(
    db: Session,
    trading_session: TradingSession,
    strategy_run: StrategyRun,
    option_contract: OptionContract,
) -> Position:
    """Like `_dispatch_position`, but deliberately omits `broker=` so
    `dispatch_trade_intent` resolves via `get_execution_broker` -- the
    caller must already have that (and `run_preflight_checks`) monkeypatched
    to read as genuinely live, same pattern as `test_execution_paper_service
    .py`'s `_FakeLiveBroker` tests.
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
        entry_price=80.0,
        stop_price=72.0,
        target_price=92.0,
        status=TradeIntentStatus.DISPATCHED,
        created_at=now,
        dispatched_at=now,
    )
    db.add(trade_intent)
    db.flush()

    dispatch_trade_intent(db, trading_session, trade_intent)
    return db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()


def test_run_reconciliation_clean_when_local_matches_broker(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    _dispatch_position(db, trading_session, strategy_run, option_contract, broker)

    run = run_reconciliation(db, broker, trading_session, ReconciliationTrigger.EVENT)

    assert run.mismatches_found == 0
    assert run.action_taken == "none"
    assert (
        db.query(SystemAlert)
        .filter(
            SystemAlert.trading_session_id == trading_session.id,
            SystemAlert.category == "reconciliation_mismatch",
        )
        .count()
        == 0
    )

    sync_state = (
        db.query(BrokerSyncState)
        .filter(
            BrokerSyncState.trading_session_id == trading_session.id,
            BrokerSyncState.option_contract_id == option_contract.id,
        )
        .one()
    )
    assert sync_state.local_qty == sync_state.broker_qty == 25
    assert sync_state.is_mismatched is False


def test_run_reconciliation_flags_an_injected_mismatch(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    _dispatch_position(db, trading_session, strategy_run, option_contract, broker)

    # Inject an inconsistency directly against the broker's own book — e.g.
    # a real broker fill the local system never recorded — independent of
    # execution_engine.paper.service, which is exactly what "an injected
    # inconsistency" means for this done-when criterion.
    broker.place_order(
        OrderRequest(
            idempotency_key=f"manual-injection-{uuid.uuid4()}",
            contract_symbol=option_contract.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            qty=25,
        )
    )

    run = run_reconciliation(db, broker, trading_session, ReconciliationTrigger.EVENT)

    assert run.mismatches_found == 1
    assert run.action_taken == "alert_raised"  # paper_only: flagged, not blocked

    alerts = db.query(SystemAlert).filter(
        SystemAlert.trading_session_id == trading_session.id,
        SystemAlert.category == "reconciliation_mismatch",
    ).all()
    assert len(alerts) == 1

    sync_state = (
        db.query(BrokerSyncState)
        .filter(
            BrokerSyncState.trading_session_id == trading_session.id,
            BrokerSyncState.option_contract_id == option_contract.id,
        )
        .one()
    )
    assert sync_state.is_mismatched is True
    assert sync_state.local_qty == 25
    assert sync_state.broker_qty == 50

    # paper_only has no live money at risk — flagged, but not mode-blocked.
    assert trading_session.mode == SafeMode.PAPER_ONLY


def test_run_reconciliation_enters_reconciliation_lock_from_guarded_live_on_live_mismatch(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """A *live-book* mismatch on a live-eligible session locks the session."""
    trading_session.mode = SafeMode.LIVE_ENABLED
    db.add(trading_session)
    db.flush()

    class _StrayLiveBroker:
        def get_positions(self) -> list[BrokerPosition]:
            return [BrokerPosition(contract_symbol=option_contract.symbol, qty=10, avg_price=80.0)]

    run = run_reconciliation(
        db, _StrayLiveBroker(), trading_session, ReconciliationTrigger.POLL  # type: ignore[arg-type]
    )

    assert run.mismatches_found == 1
    assert run.action_taken == "reconciliation_lock_entered"
    assert trading_session.mode == SafeMode.RECONCILIATION_LOCK


class _FakeLiveDelegatingBroker:
    """Reads as live (`not isinstance(_, MockBrokerAdapter)`) while
    delegating real behavior to an inner mock -- same trick
    `test_execution_paper_service.py`'s own `_FakeLiveBroker` uses.
    """

    def __init__(self, inner: MockBrokerAdapter) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_run_reconciliation_auto_repairs_a_local_open_broker_flat_live_position(
    db: Session, trading_session, strategy_run, option_contract, monkeypatch
):
    """Live incident 2026-09-02: two real live positions got stuck OPEN
    locally after being squared off directly in the broker's own app,
    tripping reconciliation_lock -- fixed by hand via a one-off SQL
    transaction at the time. This pins the generalized fix: reconciliation
    itself should recover the real fill from the broker's own order history
    and close the position, never even reaching an alert/lock.
    """
    trading_session.mode = SafeMode.LIVE_ENABLED
    db.add(trading_session)
    db.flush()

    inner = MockBrokerAdapter()
    entry_broker = _FakeLiveDelegatingBroker(inner)
    # dispatch_trade_intent's own LIVE-mode preflight gate is orthogonal to
    # this test (it's about pre-trade margin/option-chain freshness, not
    # anything this test exercises) -- neutralized the same way every other
    # "genuinely live" dispatch in this suite already does.
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service.run_preflight_checks",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service._raise_if_option_chain_stale",
        lambda *args, **kwargs: None,
    )
    # A LIVE entry fill also places a real protective SL-LMT
    # (place_protective_stop) -- the mock adapter always fills synchronously
    # with no concept of a genuinely resting order, which would otherwise
    # trip that function's own defensive "filled at placement" branch and
    # finalize the position as STOP right here, before this test ever gets
    # to reconciliation. Queue the entry's own fill explicitly, then a
    # PENDING ack for the stop placement right after it, so the position
    # stays genuinely OPEN -- exactly the real-incident shape (a resting
    # stop that hasn't resolved yet).
    inner.queue_fill_scenario(
        option_contract.symbol,
        FillScenario(status=BrokerOrderStatus.FILLED, avg_fill_price=80.0),
    )
    inner.queue_fill_scenario(
        option_contract.symbol, FillScenario(status=BrokerOrderStatus.PENDING)
    )
    position = _dispatch_position(
        db, trading_session, strategy_run, option_contract, entry_broker  # type: ignore[arg-type]
    )

    fill = TradeFill(
        broker_order_id="SHOONYA-MANUAL-1",
        contract_symbol=option_contract.symbol,
        side=OrderSide.SELL,
        qty=position.qty,
        avg_price=100.30,
        ts=datetime.now(UTC),
    )

    class _RepairBroker(_FakeLiveDelegatingBroker):
        def get_positions(self) -> list[BrokerPosition]:
            return []  # broker shows flat -- the human squared off directly there

        def get_recent_trades(self, contract_symbol: str) -> list[TradeFill]:
            return [fill] if contract_symbol == option_contract.symbol else []

    run = run_reconciliation(
        db, _RepairBroker(inner), trading_session, ReconciliationTrigger.EVENT  # type: ignore[arg-type]
    )

    assert run.mismatches_found == 0
    assert run.action_taken == "none"
    db.refresh(position)
    assert position.status == PositionStatus.CLOSED

    outcome = db.query(TradeOutcome).filter(TradeOutcome.position_id == position.id).one()
    assert outcome.exit_reason == ExitReason.RECONCILED
    assert float(outcome.exit_price) == pytest.approx(100.30)

    # Never even alerted/escalated -- the repair happened before that check.
    assert (
        db.query(SystemAlert)
        .filter(
            SystemAlert.trading_session_id == trading_session.id,
            SystemAlert.category == "reconciliation_mismatch",
        )
        .count()
        == 0
    )
    assert trading_session.mode == SafeMode.LIVE_ENABLED


def test_run_reconciliation_does_not_auto_repair_a_paper_mismatch(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """Auto-repair is LIVE-only -- nothing outside this codebase can ever
    generate a real fill for the mock's own book, so a paper-side
    local-open/broker-flat mismatch (the mock's process-memory book losing
    state across a restart, a different already-understood bug) must still
    take the normal alert path, not attempt a repair.
    """
    _dispatch_position(db, trading_session, strategy_run, option_contract, broker)
    broker.seed_position(option_contract.symbol, 0, 0.0)  # simulate the mock's book going flat

    run = run_reconciliation(db, broker, trading_session, ReconciliationTrigger.EVENT)

    assert run.mismatches_found == 1
    assert run.action_taken == "alert_raised"
    assert (
        db.query(SystemAlert)
        .filter(
            SystemAlert.trading_session_id == trading_session.id,
            SystemAlert.category == "reconciliation_mismatch",
        )
        .count()
        == 1
    )


def test_paper_book_mismatch_never_locks_a_live_active_session(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """A mock/paper-book discrepancy has no real-money meaning and must not
    halt live execution -- even on a live_enabled session. It is
    still alerted + audited, just not mode-blocked.
    """
    trading_session.mode = SafeMode.LIVE_ENABLED
    db.add(trading_session)
    db.flush()

    _dispatch_position(db, trading_session, strategy_run, option_contract, broker)
    broker.place_order(
        OrderRequest(
            idempotency_key=f"manual-injection-{uuid.uuid4()}",
            contract_symbol=option_contract.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            qty=10,
        )
    )

    run = run_reconciliation(db, broker, trading_session, ReconciliationTrigger.POLL)

    assert run.mismatches_found == 1
    assert run.action_taken == "alert_raised"
    assert trading_session.mode == SafeMode.LIVE_ENABLED
    assert (
        db.query(SystemAlert)
        .filter(
            SystemAlert.trading_session_id == trading_session.id,
            SystemAlert.category == "reconciliation_mismatch",
        )
        .count()
        == 1
    )


def test_paper_book_mismatch_on_live_active_session_overrides_paper_telegram_suppression(
    db: Session, broker, trading_session, strategy_run, option_contract, monkeypatch
):
    """The paper-book mismatch on a live-active session still can't lock,
    but it *does* reach Telegram (override_paper_mode_suppression=True) so
    the user investigates a broken paper book while real money is in play.
    """
    trading_session.mode = SafeMode.LIVE_ENABLED
    db.add(trading_session)
    db.flush()

    _dispatch_position(db, trading_session, strategy_run, option_contract, broker)
    broker.place_order(
        OrderRequest(
            idempotency_key=f"manual-injection-{uuid.uuid4()}",
            contract_symbol=option_contract.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            qty=10,
        )
    )

    captured: dict[str, object] = {}

    def _spy_send_alert(*args, **kwargs):
        captured.update(kwargs)
        from app.modules.alerting.manager import send_alert as real

        return real(*args, **kwargs)

    monkeypatch.setattr("app.modules.reconciliation.service.send_alert", _spy_send_alert)

    run = run_reconciliation(db, broker, trading_session, ReconciliationTrigger.POLL)

    assert run.action_taken == "alert_raised"
    assert trading_session.mode == SafeMode.LIVE_ENABLED
    assert captured["override_paper_mode_suppression"] is True


def test_paper_book_mismatch_on_paper_only_session_does_not_override_suppression(
    db: Session, broker, trading_session, strategy_run, option_contract, monkeypatch
):
    _dispatch_position(db, trading_session, strategy_run, option_contract, broker)
    broker.place_order(
        OrderRequest(
            idempotency_key=f"manual-injection-{uuid.uuid4()}",
            contract_symbol=option_contract.symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            qty=10,
        )
    )

    from app.modules.alerting.manager import send_alert as _real_alert

    captured: dict[str, object] = {}

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return _real_alert(*args, **kwargs)

    monkeypatch.setattr("app.modules.reconciliation.service.send_alert", _spy)

    run_reconciliation(db, broker, trading_session, ReconciliationTrigger.POLL)

    assert captured["override_paper_mode_suppression"] is False


def test_run_reconciliation_records_a_run_row_even_when_clean(
    db: Session, broker, trading_session
):
    run_reconciliation(db, broker, trading_session, ReconciliationTrigger.POLL)

    stored = (
        db.query(ReconciliationRun)
        .filter(ReconciliationRun.trading_session_id == trading_session.id)
        .one()
    )
    assert stored.trigger_type == ReconciliationTrigger.POLL
    assert stored.mismatches_found == 0


def test_paper_position_not_flagged_against_a_fake_live_broker(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    """2026-08-19 fix: `_local_net_qty_by_symbol` is now scoped to the
    comparison broker's own live/paper mode. A paper position dispatched
    against the mock must not show up as a phantom mismatch when reconciled
    against an (empty) genuinely-live broker -- before this fix, comparing
    *every* local position against a single broker meant the other mode's
    positions always looked like local-only holdings.
    """
    _dispatch_position(db, trading_session, strategy_run, option_contract, broker)

    class _EmptyLiveBroker:
        def get_positions(self) -> list[BrokerPosition]:
            return []

    run = run_reconciliation(
        db, _EmptyLiveBroker(), trading_session, ReconciliationTrigger.EVENT  # type: ignore[arg-type]
    )

    assert run.mismatches_found == 0


def test_live_position_not_flagged_against_a_fresh_paper_mock(
    db: Session, broker, trading_session, strategy_run, strategy_config, option_contract,
    monkeypatch,
):
    """Symmetric case: a genuinely live position must not be flagged when
    reconciled against a fresh, unrelated paper mock.
    """
    trading_session.mode = SafeMode.LIVE_ENABLED
    db.add(trading_session)
    db.add(strategy_config)
    db.flush()

    class _FakeLiveBroker:
        def __init__(self, inner: MockBrokerAdapter) -> None:
            self._inner = inner

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    fake_live_broker = _FakeLiveBroker(broker)
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service.get_execution_broker",
        lambda trading_session, strategy_run=None, **kwargs: fake_live_broker,
    )
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service.run_preflight_checks",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.modules.execution_engine.paper.service._raise_if_option_chain_stale",
        lambda *args, **kwargs: None,
    )

    _dispatch_live_position(db, trading_session, strategy_run, option_contract)

    run = run_reconciliation(db, MockBrokerAdapter(), trading_session, ReconciliationTrigger.EVENT)

    assert run.mismatches_found == 0


def test_run_full_reconciliation_checks_both_books(
    db: Session, broker, trading_session, strategy_run, option_contract, monkeypatch
):
    """`run_full_reconciliation` always runs the paper pass and, once a
    real broker is connected, also runs the live pass -- proving the
    2026-08-19 coverage-gap fix: the account-wide call sites (periodic
    poll, manual endpoint, startup recovery) used to resolve a single,
    session-level broker that -- in live_enabled -- never
    touched the real broker's book at all.
    """
    _dispatch_position(db, trading_session, strategy_run, option_contract, broker)

    class _StrayLiveBroker:
        def get_positions(self) -> list[BrokerPosition]:
            return [BrokerPosition(contract_symbol="NIFTY26JUL99999CE", qty=25, avg_price=80.0)]

    monkeypatch.setattr(
        "app.modules.reconciliation.service.is_execution_broker_connected", lambda: True
    )
    monkeypatch.setattr("app.modules.reconciliation.service.get_execution_mock", lambda: broker)
    monkeypatch.setattr("app.modules.reconciliation.service.get_broker", lambda: _StrayLiveBroker())

    runs = run_full_reconciliation(db, trading_session, ReconciliationTrigger.POLL)

    assert len(runs) == 2
    paper_run, live_run = runs
    assert paper_run.mismatches_found == 0
    assert live_run.mismatches_found == 1
    assert live_run.detail["mismatches"][0]["symbol"] == "NIFTY26JUL99999CE"


def test_run_full_reconciliation_skips_live_pass_when_shoonya_not_configured(
    db: Session, trading_session, monkeypatch
):
    monkeypatch.setattr(
        "app.modules.reconciliation.service.is_execution_broker_connected", lambda: False
    )

    runs = run_full_reconciliation(db, trading_session, ReconciliationTrigger.POLL)

    assert len(runs) == 1


def test_reconciliation_nets_same_symbol_across_two_strategies_of_the_same_mode(
    db: Session, broker, workspace, user, trading_session, strategy_run, option_contract
):
    """Two different strategies holding paper positions on the identical
    contract must net together correctly against the broker's own combined
    position -- not falsely flagged. Directly answers the "different
    strategies, same strike" half of the 2026-08-19 same-strike rescoping
    (`risk_engine.service._same_strike_locked`).
    """
    _dispatch_position(db, trading_session, strategy_run, option_contract, broker)

    other_config = StrategyConfig(id=uuid.uuid4(), workspace_id=workspace.id, name="other-strategy")
    db.add(other_config)
    db.flush()
    other_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=other_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
    )
    db.add(other_run)
    db.flush()
    _dispatch_position(db, trading_session, other_run, option_contract, broker)

    run = run_reconciliation(db, broker, trading_session, ReconciliationTrigger.EVENT)

    assert run.mismatches_found == 0
    sync_state = (
        db.query(BrokerSyncState)
        .filter(
            BrokerSyncState.trading_session_id == trading_session.id,
            BrokerSyncState.option_contract_id == option_contract.id,
        )
        .one()
    )
    assert sync_state.local_qty == sync_state.broker_qty == 50
