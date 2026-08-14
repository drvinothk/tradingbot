"""Risk Service tests against a real Postgres — advisory locks (the same
LOCK_RISK_EVALUATION_QUEUE serialization used in production) aren't
meaningfully testable against SQLite, matching test_state_machine.py's own
reasoning for why these require a live DB.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.v1.strategies import _get_pending_approval_or_404
from app.domain.audit.models import AuditEvent
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.identity.models import Workspace as WorkspaceRow
from app.domain.market.models import Instrument, OptionContract, OptionType, QuoteTick
from app.domain.ops.models import SystemAlert
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import (
    ApprovalStatus,
    ExecutionMode,
    PendingTradeApproval,
    Signal,
    SignalSide,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    TradeIntent,
    TradeIntentStatus,
)
from app.modules.audit_service.service import verify_chain
from app.modules.broker_adapter.base.contracts import MarginInfo
from app.modules.broker_adapter.mock.adapter import MockBrokerAdapter
from app.modules.execution_engine.paper.service import dispatch_trade_intent
from app.modules.risk_engine.service import (
    compute_pre_trade_analytics,
    create_new_risk_limit_config_version,
    evaluate_trade_intent,
    get_active_risk_limit_config,
    record_trade_outcome_effects,
)
from app.modules.strategy_engine.service import expire_stale_pending_approvals


@pytest.fixture
def broker() -> MockBrokerAdapter:
    return MockBrokerAdapter()


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="risk-test-account",
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
        budget_amount=100_000,
        daily_target_profit=2_000,
        daily_loss_cap=1_000,
        funding_mode=FundingMode.CASH,
    )
    db.add(ts)
    db.flush()
    return ts


@pytest.fixture
def instrument(db: Session) -> Instrument:
    inst = Instrument(
        id=uuid.uuid4(), symbol="NIFTY", exchange="NFO", lot_size=25, tick_size=0.05
    )
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
        broker_token="12345",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(id=uuid.uuid4(), workspace_id=workspace.id, name="test-strategy")
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
        side=SignalSide.BUY,
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
        side=SignalSide.BUY,
        qty_lots=qty_lots,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        status=TradeIntentStatus.PENDING_RISK,
        created_at=now,
    )
    db.add(trade_intent)
    db.flush()
    return trade_intent


def _dispatch(
    db: Session,
    trading_session: TradingSession,
    strategy_run: StrategyRun,
    option_contract: OptionContract,
    broker: MockBrokerAdapter,
    **kwargs,
) -> TradeIntent:
    """Convenience: build + evaluate + dispatch a trade intent (the same two
    calls `strategy_engine.service.submit_signal` makes for the AUTO path),
    asserting it goes all the way to an open Position — used by tests that
    need an *open position* as setup, not as the thing under test."""
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract, **kwargs)
    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)
    assert decision.decision == "approved", decision.reasons
    assert trade_intent.status == TradeIntentStatus.DISPATCHED
    dispatch_trade_intent(db, trading_session, trade_intent, broker=broker)
    return trade_intent


# -- risk_limit_configs versioning -----------------------------------------


def test_get_active_risk_limit_config_lazily_seeds_from_defaults(db: Session, workspace):
    from app.config.settings import get_settings

    config = get_active_risk_limit_config(db, workspace.id)
    defaults = get_settings().risk_defaults

    assert config.version == 1
    assert config.is_active is True
    assert config.max_concurrent_positions == defaults.max_concurrent_positions
    assert config.max_trades_per_day == defaults.max_trades_per_day

    again = get_active_risk_limit_config(db, workspace.id)
    assert again.id == config.id


def test_create_new_risk_limit_config_version_deactivates_previous(
    db: Session, workspace, authorized_user
):
    v1 = get_active_risk_limit_config(db, workspace.id)
    v2 = create_new_risk_limit_config_version(
        db, workspace.id, actor_user=authorized_user, reason="tightening", max_trades_per_day=1
    )

    assert v2.version == v1.version + 1
    assert v2.max_trades_per_day == 1
    db.refresh(v1)
    assert v1.is_active is False
    assert get_active_risk_limit_config(db, workspace.id).id == v2.id


# -- pre-trade analytics ----------------------------------------------------


def test_compute_pre_trade_analytics_ce_capital_breakeven_and_pnl(
    db: Session, option_contract: OptionContract
):
    analytics = compute_pre_trade_analytics(
        db,
        option_contract,
        side=SignalSide.BUY,
        qty_lots=2,
        entry_price=80.0,
        stop_price=72.0,
        target_price=92.0,
        funding_mode=FundingMode.CASH,
    )

    assert analytics.capital_required == pytest.approx(80.0 * 25 * 2)
    assert analytics.breakeven_price == pytest.approx(22000 + 80.0)
    assert analytics.pnl_scenarios["at_stop"] == pytest.approx((72.0 - 80.0) * 25 * 2)
    assert analytics.pnl_scenarios["at_breakeven"] == pytest.approx(0.0)
    assert analytics.pnl_scenarios["at_target"] == pytest.approx((92.0 - 80.0) * 25 * 2)
    assert analytics.pnl_scenarios["stretch"] == pytest.approx((104.0 - 80.0) * 25 * 2)


def test_compute_pre_trade_analytics_pe_breakeven_is_strike_minus_premium(
    db: Session, instrument: Instrument
):
    pe_contract = OptionContract(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        expiry_date=date(2026, 7, 30),
        strike=22000,
        option_type=OptionType.PE,
        symbol="NIFTY26JUL22000PE",
    )
    db.add(pe_contract)
    db.flush()

    analytics = compute_pre_trade_analytics(
        db,
        pe_contract,
        side=SignalSide.BUY,
        qty_lots=1,
        entry_price=80.0,
        stop_price=72.0,
        target_price=92.0,
        funding_mode=FundingMode.CASH,
    )

    assert analytics.breakeven_price == pytest.approx(22000 - 80.0)


def test_compute_pre_trade_analytics_mtf_reduces_capital_required(
    db: Session, option_contract: OptionContract
):
    cash = compute_pre_trade_analytics(
        db, option_contract, side=SignalSide.BUY, qty_lots=1, entry_price=80.0,
        stop_price=72.0, target_price=92.0, funding_mode=FundingMode.CASH,
    )
    mtf = compute_pre_trade_analytics(
        db, option_contract, side=SignalSide.BUY, qty_lots=1, entry_price=80.0,
        stop_price=72.0, target_price=92.0, funding_mode=FundingMode.MTF,
    )

    assert mtf.capital_required < cash.capital_required
    assert mtf.capital_required == pytest.approx(cash.capital_required / 5)


# -- evaluate_trade_intent: individual limit checks --------------------------


def test_evaluate_trade_intent_approves_and_dispatches_in_auto_mode(
    db: Session, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert decision.decision == "approved"
    assert decision.reasons == []
    assert trade_intent.status == TradeIntentStatus.DISPATCHED
    assert trade_intent.dispatched_at is not None


def test_evaluate_trade_intent_rejects_when_mode_blocks_new_entries(
    db: Session, trading_session, strategy_run, option_contract
):
    trading_session.mode = SafeMode.KILL_SWITCH
    db.add(trading_session)
    db.flush()

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert any(r.startswith("mode_blocks_new_entries") for r in decision.reasons)
    assert trade_intent.status == TradeIntentStatus.RISK_REJECTED


def test_evaluate_trade_intent_same_strike_locked(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    _dispatch(db, trading_session, strategy_run, option_contract, broker)

    second = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    decision = evaluate_trade_intent(db, second, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert "same_strike_locked" in decision.reasons


def test_evaluate_trade_intent_max_concurrent_positions(
    db: Session, broker, trading_session, strategy_run, instrument
):
    contracts = []
    for i in range(3):
        c = OptionContract(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            expiry_date=date(2026, 7, 30),
            strike=22000 + i * 50,
            option_type=OptionType.CE,
            symbol=f"NIFTY26JUL{22000 + i * 50}CE",
        )
        db.add(c)
        db.flush()
        contracts.append(c)

    # Default max_concurrent_positions is 2 (RiskDefaults) — first two open
    # cleanly, the third (a different strike, so not same-strike-locked)
    # should trip the concurrency cap instead.
    _dispatch(db, trading_session, strategy_run, contracts[0], broker)
    _dispatch(db, trading_session, strategy_run, contracts[1], broker)

    third = _make_trade_intent(db, trading_session, strategy_run, contracts[2])
    decision = evaluate_trade_intent(db, third, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert "max_concurrent_positions_reached" in decision.reasons


def test_evaluate_trade_intent_max_trades_per_day(
    db: Session, broker, workspace, authorized_user, trading_session, strategy_run, instrument
):
    """2026-08-12: the cap only ever protects real-money exposure now — see
    this test's sibling `test_paper_only_session_has_no_daily_trade_cap`
    below for why `paper_only` is exempt. Set to a live-capable mode here
    so this test still exercises the cap itself. `paper_plus_guarded_live`,
    not `live_enabled`: the cap condition is any `mode != PAPER_ONLY`, and
    with no strategy_run marked LIVE-graduated, Ops-Hardening Phase 5's
    get_execution_broker still safely resolves to the paper mock here.
    """
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    db.add(trading_session)
    db.flush()

    create_new_risk_limit_config_version(
        db,
        workspace.id,
        actor_user=authorized_user,
        max_trades_per_day=1,
        max_concurrent_positions=5,
    )

    c1 = OptionContract(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=date(2026, 7, 30),
        strike=22000, option_type=OptionType.CE, symbol="NIFTY26JUL22000CE-A",
    )
    c2 = OptionContract(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=date(2026, 7, 30),
        strike=22050, option_type=OptionType.CE, symbol="NIFTY26JUL22050CE-B",
    )
    db.add_all([c1, c2])
    db.flush()

    _dispatch(db, trading_session, strategy_run, c1, broker)

    second = _make_trade_intent(db, trading_session, strategy_run, c2)
    decision = evaluate_trade_intent(db, second, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert "max_trades_per_day_reached" in decision.reasons


def test_paper_only_session_has_no_daily_trade_cap(
    db: Session, broker, workspace, authorized_user, trading_session, strategy_run, instrument
):
    """2026-08-12: real gap found live — the cap used to count DISPATCHED
    trade_intents across the whole session with no paper/live distinction,
    so 5 trades from an earlier, unrelated, since-stopped batch of
    strategies silently blocked every signal from a completely different
    set of currently-running strategies for the rest of the day, on a
    paper_only session where no capital was ever actually at risk. A paper
    session's whole point is proving entry logic actually fires; capping
    it defeats that. `trading_session` defaults to PAPER_ONLY (see its own
    fixture).
    """
    create_new_risk_limit_config_version(
        db,
        workspace.id,
        actor_user=authorized_user,
        max_trades_per_day=1,
        max_concurrent_positions=5,
    )

    c1 = OptionContract(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=date(2026, 7, 30),
        strike=22000, option_type=OptionType.CE, symbol="NIFTY26JUL22000CE-A",
    )
    c2 = OptionContract(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=date(2026, 7, 30),
        strike=22050, option_type=OptionType.CE, symbol="NIFTY26JUL22050CE-B",
    )
    db.add_all([c1, c2])
    db.flush()

    _dispatch(db, trading_session, strategy_run, c1, broker)

    second = _make_trade_intent(db, trading_session, strategy_run, c2)
    decision = evaluate_trade_intent(db, second, trading_session, strategy_run)

    assert decision.decision == "approved"
    assert "max_trades_per_day_reached" not in decision.reasons


def test_max_trades_per_day_is_scoped_per_strategy_not_per_session(
    db: Session, broker, workspace, authorized_user, trading_session, strategy_run,
    strategy_config, instrument, user,
):
    """2026-08-12: the cap used to count DISPATCHED trade_intents across the
    whole session regardless of which strategy dispatched them, so one
    strategy hitting its own daily cap would silently block every other
    strategy running in the same session too. Now scoped by
    strategy_config_id (via strategy_run) — a second, unrelated strategy in
    the same session, same day, must not be blocked by the first one's cap.
    `paper_plus_guarded_live`, not `live_enabled` -- see the identical note
    in `test_evaluate_trade_intent_max_trades_per_day` above.
    """
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    db.add(trading_session)
    db.flush()

    create_new_risk_limit_config_version(
        db, workspace.id, actor_user=authorized_user,
        max_trades_per_day=1, max_concurrent_positions=5,
    )

    other_config = StrategyConfig(id=uuid.uuid4(), workspace_id=workspace.id, name="other-strategy")
    db.add(other_config)
    db.flush()
    other_run = StrategyRun(
        id=uuid.uuid4(), strategy_config_id=other_config.id,
        trading_session_id=trading_session.id, execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING, started_at=datetime.now(UTC),
        started_by_user_id=user.id,
    )
    db.add(other_run)
    db.flush()

    c1 = OptionContract(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=date(2026, 7, 30),
        strike=22000, option_type=OptionType.CE, symbol="NIFTY26JUL22000CE-A",
    )
    c2 = OptionContract(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=date(2026, 7, 30),
        strike=22050, option_type=OptionType.CE, symbol="NIFTY26JUL22050CE-B",
    )
    db.add_all([c1, c2])
    db.flush()

    # Strategy #1 dispatches its one allowed trade, hitting its own cap.
    _dispatch(db, trading_session, strategy_run, c1, broker)

    # Strategy #2 (different strategy_config, same session, same day) has
    # dispatched nothing yet -- must still be approved.
    second = _make_trade_intent(db, trading_session, other_run, c2)
    decision = evaluate_trade_intent(db, second, trading_session, other_run)

    assert decision.decision == "approved"
    assert "max_trades_per_day_reached" not in decision.reasons


def test_evaluate_trade_intent_consecutive_loss_pause(
    db: Session, trading_session, strategy_run, option_contract
):
    trading_session.consecutive_losses = 2  # RiskDefaults threshold is 2
    db.add(trading_session)
    db.flush()

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert "consecutive_loss_pause_active" in decision.reasons


def test_evaluate_trade_intent_per_trade_lot_cap_exceeded(
    db: Session, trading_session, strategy_run, option_contract
):
    # RiskDefaults.per_trade_lot_cap is 1.
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract, qty_lots=2
    )
    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert "per_trade_lot_cap_exceeded" in decision.reasons


def test_evaluate_trade_intent_budget_exceeded(
    db: Session, trading_session, strategy_run, option_contract
):
    trading_session.budget_amount = 1000  # 1 lot @ premium 80 * lot_size 25 = 2000 > 1000
    db.add(trading_session)
    db.flush()

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert "budget_exceeded" in decision.reasons


def test_evaluate_trade_intent_margin_check_failed(
    db: Session, trading_session, strategy_run, option_contract, monkeypatch
):
    """Proves `_check_margin` is a real broker-backed check now, not the old
    `capital_required > 0` stub — a broker reporting insufficient available
    margin rejects the trade even though every other check would pass.
    """

    class _LowMarginBroker(MockBrokerAdapter):
        def get_margin(self):
            return MarginInfo(
                available_margin=1.0, used_margin=0.0, total_margin=1.0, ts=datetime.now(UTC)
            )

    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker",
        lambda trading_session: _LowMarginBroker(),
    )

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert "margin_check_failed" in decision.reasons


def test_evaluate_trade_intent_tick_size_violation(
    db: Session, trading_session, strategy_run, option_contract
):
    # instrument fixture's tick_size is 0.05 — 80.03 isn't a multiple of it.
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract, entry_price=80.03
    )

    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert "tick_size_violation:entry" in decision.reasons


def test_evaluate_trade_intent_tick_aligned_price_is_not_rejected(
    db: Session, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract, entry_price=80.05
    )

    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert not any(r.startswith("tick_size_violation") for r in decision.reasons)


def test_evaluate_trade_intent_freeze_qty_exceeded(
    db: Session, trading_session, strategy_run, option_contract, instrument
):
    instrument.freeze_qty = 20  # lot_size=25 * qty_lots=1 = 25 > 20
    db.add(instrument)
    db.flush()
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)

    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert "freeze_qty_exceeded" in decision.reasons


def test_evaluate_trade_intent_freeze_qty_none_is_a_noop(
    db: Session, trading_session, strategy_run, option_contract, instrument
):
    assert instrument.freeze_qty is None  # default — no operator value set
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)

    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert "freeze_qty_exceeded" not in decision.reasons


def test_evaluate_trade_intent_price_drift_exceeded(
    db: Session, trading_session, strategy_run, option_contract
):
    """AUTO-mode equivalent of approve_trade_approval's manual price-drift
    re-check — closes the asymmetry a human's Approve click was protected by
    but an AUTO-dispatched intent never was.
    """
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract, entry_price=80.0
    )
    db.add(
        QuoteTick(
            id=uuid.uuid4(),
            option_contract_id=option_contract.id,
            ltp=95.0,  # ~19% away from entry_price=80.0, past the 3% tolerance
            bid=94.9,
            ask=95.1,
            volume=100,
            oi=1000,
            ts=datetime.now(UTC),
        )
    )
    db.flush()

    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert "price_drift_exceeded" in decision.reasons


def test_evaluate_trade_intent_no_drift_rejection_when_tick_within_tolerance(
    db: Session, trading_session, strategy_run, option_contract
):
    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract, entry_price=80.0
    )
    db.add(
        QuoteTick(
            id=uuid.uuid4(),
            option_contract_id=option_contract.id,
            ltp=81.0,  # ~1.25% away, within the 3% tolerance
            bid=80.9,
            ask=81.1,
            volume=100,
            oi=1000,
            ts=datetime.now(UTC),
        )
    )
    db.flush()

    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert "price_drift_exceeded" not in decision.reasons


def test_evaluate_trade_intent_rejection_raises_system_alert(
    db: Session, trading_session, strategy_run, option_contract
):
    trading_session.mode = SafeMode.KILL_SWITCH
    db.add(trading_session)
    db.flush()

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    alerts = (
        db.query(SystemAlert).filter(SystemAlert.trading_session_id == trading_session.id).all()
    )
    assert len(alerts) == 1
    assert alerts[0].category == "risk_limit_breach"


def test_evaluate_trade_intent_approval_required_creates_pending_approval(
    db: Session, trading_session, strategy_config, user: User, option_contract
):
    approval_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.APPROVAL_REQUIRED,
        status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
    )
    db.add(approval_run)
    db.flush()

    trade_intent = _make_trade_intent(db, trading_session, approval_run, option_contract)
    decision = evaluate_trade_intent(db, trade_intent, trading_session, approval_run)

    assert decision.decision == "approved"
    assert trade_intent.status == TradeIntentStatus.PENDING_APPROVAL
    pending = (
        db.query(PendingTradeApproval)
        .filter(PendingTradeApproval.trade_intent_id == trade_intent.id)
        .one()
    )
    assert pending.capital_required == pytest.approx(float(decision.capital_required))


# -- expire_stale_pending_approvals: proactive expiry -------------------------


def test_expire_stale_pending_approvals_expires_past_window(
    db: Session, trading_session, strategy_config, user: User, option_contract
):
    approval_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.APPROVAL_REQUIRED,
        status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
    )
    db.add(approval_run)
    db.flush()

    trade_intent = _make_trade_intent(db, trading_session, approval_run, option_contract)
    evaluate_trade_intent(db, trade_intent, trading_session, approval_run)
    pending = (
        db.query(PendingTradeApproval)
        .filter(PendingTradeApproval.trade_intent_id == trade_intent.id)
        .one()
    )
    pending.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.add(pending)
    db.flush()

    expired = expire_stale_pending_approvals(db, trading_session)

    assert len(expired) == 1
    assert expired[0].id == pending.id
    db.refresh(pending)
    db.refresh(trade_intent)
    assert pending.status == ApprovalStatus.EXPIRED
    assert trade_intent.status == TradeIntentStatus.EXPIRED


def test_expire_stale_pending_approvals_leaves_fresh_ones_alone(
    db: Session, trading_session, strategy_config, user: User, option_contract
):
    approval_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.APPROVAL_REQUIRED,
        status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
    )
    db.add(approval_run)
    db.flush()

    trade_intent = _make_trade_intent(db, trading_session, approval_run, option_contract)
    evaluate_trade_intent(db, trade_intent, trading_session, approval_run)

    expired = expire_stale_pending_approvals(db, trading_session)

    assert expired == []
    pending = (
        db.query(PendingTradeApproval)
        .filter(PendingTradeApproval.trade_intent_id == trade_intent.id)
        .one()
    )
    assert pending.status == ApprovalStatus.PENDING
    assert trade_intent.status == TradeIntentStatus.PENDING_APPROVAL


# -- record_trade_outcome_effects: P&L-driven triggers -----------------------
#
# Phase 3 replaces record_synthetic_outcome with record_trade_outcome_effects
# (see risk_engine.service's module docstring) — it no longer creates its own
# outcome row (execution_engine.paper.service.close_position writes the real
# TradeOutcome now) or takes a TradeIntent at all, just the trading_session
# and a realized_pnl, so these tests no longer need a dispatched position as
# setup. Coverage of the full dispatch -> close -> effects chain lives in
# tests/integration/test_execution_paper_service.py.


def test_record_trade_outcome_effects_updates_running_totals(db: Session, trading_session):
    record_trade_outcome_effects(db, trading_session, realized_pnl=150.0)

    assert float(trading_session.cumulative_realized_pnl) == pytest.approx(150.0)
    assert trading_session.consecutive_losses == 0


def test_record_trade_outcome_effects_increments_consecutive_losses(db: Session, trading_session):
    record_trade_outcome_effects(db, trading_session, realized_pnl=-50.0)

    assert trading_session.consecutive_losses == 1


def test_record_trade_outcome_effects_breaching_loss_cap_triggers_kill_switch(
    db: Session, trading_session
):
    # daily_loss_cap is 1000 on the fixture session.
    record_trade_outcome_effects(db, trading_session, realized_pnl=-1500.0)

    assert trading_session.mode == SafeMode.KILL_SWITCH
    alerts = db.query(SystemAlert).filter(
        SystemAlert.trading_session_id == trading_session.id,
        SystemAlert.category == "daily_loss_cap_breached",
    ).all()
    assert len(alerts) == 1


def test_record_trade_outcome_effects_hitting_target_sets_entries_paused(
    db: Session, trading_session
):
    # daily_target_profit is 2000 on the fixture session.
    record_trade_outcome_effects(db, trading_session, realized_pnl=2500.0)

    assert trading_session.entries_paused_reason == "daily_target_reached"
    # Not a mode transition — reaching a target is a goal, not a fault.
    assert trading_session.mode == SafeMode.PAPER_ONLY


def test_entries_paused_blocks_further_trade_intents(
    db: Session, trading_session, strategy_run, option_contract, instrument
):
    record_trade_outcome_effects(db, trading_session, realized_pnl=2500.0)
    assert trading_session.entries_paused_reason == "daily_target_reached"

    other_contract = OptionContract(
        id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=date(2026, 7, 30),
        strike=22100, option_type=OptionType.CE, symbol="NIFTY26JUL22100CE",
    )
    db.add(other_contract)
    db.flush()

    next_intent = _make_trade_intent(db, trading_session, strategy_run, other_contract)
    decision = evaluate_trade_intent(db, next_intent, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert any(r.startswith("entries_paused") for r in decision.reasons)


# -- audit trail integrity ----------------------------------------------------


def test_risk_decisions_are_fully_audited_and_chain_stays_intact(
    db: Session, broker, trading_session, strategy_run, option_contract
):
    _dispatch(db, trading_session, strategy_run, option_contract, broker)

    events = (
        db.query(AuditEvent)
        .filter(AuditEvent.trading_session_id == trading_session.id)
        .all()
    )
    event_types = {e.event_type for e in events}
    assert "signal.generated" not in event_types  # submit_signal not used by _dispatch directly
    assert "risk_decision.approved.dispatched" in event_types

    ok, broken_id = verify_chain(db)
    assert ok, f"audit chain broken at {broken_id}"


# -- trade-approval lookup: workspace scoping (IDOR regression) --------------


def test_pending_approval_lookup_denies_cross_workspace_access(
    db: Session, workspace, user, trading_session, strategy_run, option_contract
):
    """PendingTradeApproval has no workspace_id column of its own — the
    lookup backing approve/reject must scope through TradeIntent instead.
    Without it, any authenticated user could approve/reject another
    workspace's pending trade just by knowing its UUID, unlike every other
    lookup helper in app.api.v1.strategies, which all filter by
    `user.workspace_id`.
    """
    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    trade_intent.status = TradeIntentStatus.PENDING_APPROVAL
    db.add(trade_intent)
    db.flush()

    approval = PendingTradeApproval(
        id=uuid.uuid4(),
        trade_intent_id=trade_intent.id,
        strategy_run_id=strategy_run.id,
        status=ApprovalStatus.PENDING,
        capital_required=2000.0,
        breakeven_price=22080.0,
        pnl_scenarios={
            "at_stop": -400.0,
            "at_breakeven": 0.0,
            "at_target": 600.0,
            "stretch": 1200.0,
        },
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db.add(approval)
    db.flush()

    other_workspace = WorkspaceRow(id=uuid.uuid4(), name=f"other-{uuid.uuid4().hex[:8]}")
    db.add(other_workspace)
    db.flush()
    other_user = User(
        id=uuid.uuid4(),
        workspace_id=other_workspace.id,
        email=f"other-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="not-checked-by-this-helper",
        display_name="Other Workspace User",
        is_active=True,
    )
    db.add(other_user)
    db.flush()

    with pytest.raises(HTTPException) as exc_info:
        _get_pending_approval_or_404(db, other_user, approval.id)
    assert exc_info.value.status_code == 404

    # Same-workspace access still works — this isn't just "always deny".
    found = _get_pending_approval_or_404(db, user, approval.id)
    assert found.id == approval.id
