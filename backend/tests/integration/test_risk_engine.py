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
from app.domain.execution.models import Order, OrderMode
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
    StrategyStatus,
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


def test_compute_pre_trade_analytics_routes_pnl_scenarios_through_shared_signed_pnl(
    db: Session, option_contract: OptionContract, monkeypatch
):
    """QC fix #1: `compute_pre_trade_analytics`'s `_pnl_at` used to hand-
    derive the sign convention independently of `execution_engine.paper
    .service`/`api.v1.execution`'s identical formulas (see `app.core.pnl`'s
    own module docstring for the three-way duplication this closed). Pins
    that it now genuinely calls the shared `app.core.pnl.signed_pnl` --
    once per pnl_scenarios entry that isn't the trivial `at_breakeven: 0.0`
    special case -- rather than a formula that merely still happens to
    match it. Against the pre-fix code this assertion fails outright, since
    `signed_pnl` was never imported or called there at all.
    """
    import app.modules.risk_engine.service as risk_engine_module

    calls: list[tuple] = []
    real_signed_pnl = risk_engine_module.signed_pnl

    def _spy(entry_price, other_price, qty, side):
        calls.append((entry_price, other_price, qty, side))
        return real_signed_pnl(entry_price, other_price, qty, side)

    monkeypatch.setattr(risk_engine_module, "signed_pnl", _spy)

    compute_pre_trade_analytics(
        db,
        option_contract,
        side=SignalSide.BUY,
        qty_lots=2,
        entry_price=80.0,
        stop_price=72.0,
        target_price=92.0,
        funding_mode=FundingMode.CASH,
    )

    # at_stop, at_target, stretch -- three _pnl_at() calls; at_breakeven is
    # a hardcoded 0.0 and never calls signed_pnl at all.
    assert len(calls) == 3


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


def test_same_strike_lock_does_not_cross_strategies(
    db: Session, broker, workspace, user, trading_session, strategy_run, option_contract,
):
    """2026-08-19 regression: `_same_strike_locked` used to be session-wide
    — any other strategy's pending/open trade_intent on the exact same
    contract locked a new one out, live-confirmed against two real paper
    strategies independently proposing the same strike the same day. Now
    scoped per (strategy_config_id, option_contract_id) -- a different
    strategy must be free to trade the identical contract.
    """
    _dispatch(db, trading_session, strategy_run, option_contract, broker)

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

    second = _make_trade_intent(db, trading_session, other_run, option_contract)
    decision = evaluate_trade_intent(db, second, trading_session, other_run)

    assert "same_strike_locked" not in decision.reasons


def test_same_strike_locked_blocks_a_pending_approval_duplicate(
    db: Session, broker, trading_session, strategy_config, user: User, option_contract, monkeypatch
):
    """2026-08-19: `_same_strike_locked` checks both PENDING_APPROVAL and
    DISPATCHED, but every existing test only ever exercised the DISPATCHED
    case (via `_dispatch`). Proves the other half of "block same strategy
    same order if already pending or open" -- a strategy whose first
    trade_intent on a contract is still awaiting manual approval must not
    be able to propose a second one on the identical contract.

    2026-08-21: needs genuine live-routing now that paper trades always
    auto-dispatch regardless of execution_mode (see risk_engine.service's
    approval-required branch) -- a PENDING_APPROVAL state can only actually
    occur for a live-routed strategy any more.
    """
    trading_session.mode = SafeMode.LIVE_ENABLED
    db.add(trading_session)
    strategy_config.status = StrategyStatus.LIVE
    db.add(strategy_config)
    db.flush()
    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker",
        lambda trading_session, strategy_run=None: broker,
    )

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

    first = _make_trade_intent(db, trading_session, approval_run, option_contract)
    first_decision = evaluate_trade_intent(db, first, trading_session, approval_run)
    assert first_decision.decision == "approved"
    assert first.status == TradeIntentStatus.PENDING_APPROVAL

    second = _make_trade_intent(db, trading_session, approval_run, option_contract)
    decision = evaluate_trade_intent(db, second, trading_session, approval_run)

    assert decision.decision == "rejected"
    assert "same_strike_locked" in decision.reasons


def test_evaluate_trade_intent_max_concurrent_positions(
    db: Session, broker, workspace, authorized_user, trading_session, strategy_run,
    strategy_config, instrument, monkeypatch,
):
    """2026-08-19: max_concurrent_positions is now live-only (a busy paper
    session must never count toward it — see `_open_trade_intents_query`'s
    own docstring), so this needs a genuinely live-routed strategy and its
    open positions actually marked live, same setup pattern as `test_
    evaluate_trade_intent_max_trades_per_day`.
    """
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    db.add(trading_session)
    strategy_config.status = StrategyStatus.LIVE
    db.add(strategy_config)
    db.flush()
    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker",
        lambda trading_session, strategy_run=None: broker,
    )

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
    # should trip the concurrency cap instead. Both dispatches happen
    # *before* either is marked live: `dispatch_trade_intent` runs its own
    # event-triggered reconciliation internally against `broker` (the mock),
    # so marking the first live before the second dispatch would make that
    # second dispatch's own reconciliation see a real mismatch (the mock
    # still has the first position, but it's no longer counted as a local
    # paper position) — a test-harness artifact of `_mark_last_order_live`
    # being a raw DB flag flip, not an actual different-broker dispatch, not
    # a real bug in the 2026-08-19 mode-scoped reconciliation fix.
    first = _dispatch(db, trading_session, strategy_run, contracts[0], broker)
    second = _dispatch(db, trading_session, strategy_run, contracts[1], broker)
    _mark_last_order_live(db, first)
    _mark_last_order_live(db, second)

    third = _make_trade_intent(db, trading_session, strategy_run, contracts[2])
    decision = evaluate_trade_intent(db, third, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert "max_concurrent_positions_reached" in decision.reasons


def test_max_concurrent_positions_does_not_block_a_paper_routed_strategy(
    db: Session, broker, workspace, trading_session, strategy_run, strategy_config, instrument,
):
    """The symmetric case: a force_paper strategy holding many open paper
    positions (well past the default cap of 2) must never get
    max_concurrent_positions_reached — the cap only counts live-routed
    trade_intents (see `_open_trade_intents_query`'s own docstring)."""
    strategy_config.runtime_mode = "force_paper"
    db.add(strategy_config)
    db.flush()

    contracts = []
    for i in range(4):
        c = OptionContract(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            expiry_date=date(2026, 7, 30),
            strike=23000 + i * 50,
            option_type=OptionType.CE,
            symbol=f"NIFTY26JUL{23000 + i * 50}CE",
        )
        db.add(c)
        db.flush()
        contracts.append(c)

    for c in contracts[:3]:
        _dispatch(db, trading_session, strategy_run, c, broker)

    fourth = _make_trade_intent(db, trading_session, strategy_run, contracts[3])
    decision = evaluate_trade_intent(db, fourth, trading_session, strategy_run)

    assert "max_concurrent_positions_reached" not in decision.reasons


def _mark_last_order_live(db: Session, trade_intent: TradeIntent) -> None:
    """Test helper: `_dispatch` always passes this file's `broker` fixture
    (a real `MockBrokerAdapter`) explicitly, so `dispatch_trade_intent`'s
    `is_execution_broker_live` check correctly reads it as paper and the
    resulting `Order.mode` is always `PAPER`, regardless of session/strategy
    state. The `max_trades_per_day` cap now counts only genuinely `LIVE`
    orders (2026-08-19 fix), so tests that need to simulate "this strategy's
    earlier dispatch actually went out live" flip it directly here rather
    than threading real broker-resolution plumbing (a broker that isn't a
    `MockBrokerAdapter`, plus a monkeypatched `get_execution_broker`) through
    every risk-engine test in this file.
    """
    order = db.query(Order).filter(Order.trade_intent_id == trade_intent.id).one()
    order.mode = OrderMode.LIVE
    db.add(order)
    db.flush()


def test_evaluate_trade_intent_max_trades_per_day(
    db: Session,
    broker,
    workspace,
    authorized_user,
    trading_session,
    strategy_run,
    strategy_config,
    instrument,
    monkeypatch,
):
    """2026-08-12: the cap only ever protects real-money exposure now — see
    this test's sibling `test_paper_only_session_has_no_daily_trade_cap`
    below for why `paper_only` is exempt. 2026-08-19: the cap must also
    apply only when *this strategy* is actually routed live, and only count
    its own genuinely-live dispatches — see `test_force_paper_strategy_
    exempt_from_daily_trade_cap_even_when_session_is_live` and
    `test_daily_trade_cap_ignores_a_strategys_earlier_paper_era_dispatches`
    below for those two directions. This test exercises the cap still
    working at all: strategy graduated to LIVE, session guarded-live, and
    its one dispatch actually marked live. `get_execution_broker` is
    monkeypatched to the mock -- graduating the strategy to LIVE also makes
    `_check_margin` (this morning's own fix) try to resolve a real broker,
    which would otherwise raise `ConfigurationError` with no
    `ALLOW_REAL_MONEY_DISPATCH` in test settings; this test is about the
    daily cap, not margin, same reasoning `test_evaluate_trade_intent_force
    _paper_strategy_ignores_real_margin_shortfall` already established.
    """
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    db.add(trading_session)
    strategy_config.status = StrategyStatus.LIVE
    db.add(strategy_config)
    db.flush()
    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker",
        lambda trading_session, strategy_run=None: broker,
    )

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

    first = _dispatch(db, trading_session, strategy_run, c1, broker)
    _mark_last_order_live(db, first)

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
    strategy_config, instrument, user, monkeypatch,
):
    """2026-08-12: the cap used to count DISPATCHED trade_intents across the
    whole session regardless of which strategy dispatched them, so one
    strategy hitting its own daily cap would silently block every other
    strategy running in the same session too. Now scoped by
    strategy_config_id (via strategy_run) — a second, unrelated strategy in
    the same session, same day, must not be blocked by the first one's cap.
    Both strategies graduated to LIVE and their dispatches marked live
    (2026-08-19 fix) so this test actually exercises per-strategy scoping
    of a real cap, not just two strategies the cap skips entirely.
    `get_execution_broker` monkeypatched to the mock -- same reasoning as
    `test_evaluate_trade_intent_max_trades_per_day` above.
    """
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    db.add(trading_session)
    strategy_config.status = StrategyStatus.LIVE
    db.add(strategy_config)
    db.flush()
    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker",
        lambda trading_session, strategy_run=None: broker,
    )

    create_new_risk_limit_config_version(
        db, workspace.id, actor_user=authorized_user,
        max_trades_per_day=1, max_concurrent_positions=5,
    )

    other_config = StrategyConfig(
        id=uuid.uuid4(), workspace_id=workspace.id, name="other-strategy",
        status=StrategyStatus.LIVE,
    )
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
    first = _dispatch(db, trading_session, strategy_run, c1, broker)
    _mark_last_order_live(db, first)

    # Strategy #2 (different strategy_config, same session, same day) has
    # dispatched nothing yet -- must still be approved.
    second = _make_trade_intent(db, trading_session, other_run, c2)
    decision = evaluate_trade_intent(db, second, trading_session, other_run)

    assert decision.decision == "approved"
    assert "max_trades_per_day_reached" not in decision.reasons


def test_force_paper_strategy_exempt_from_daily_trade_cap_even_when_session_is_live(
    db: Session, broker, workspace, authorized_user, trading_session, strategy_run,
    strategy_config, option_contract,
):
    """2026-08-19 regression (direction 1, over-restrictive): a `force_paper`
    strategy must never be capped by `max_trades_per_day` just because the
    *session* reached a live-capable mode -- those trades never touched
    real money regardless of session mode, so the cap (whose whole purpose
    is capping real-money exposure) has nothing to protect here. Live
    incident: exactly this happened to a real force_paper strategy the same
    day this was fixed.
    """
    trading_session.mode = SafeMode.LIVE_ENABLED
    db.add(trading_session)
    strategy_config.runtime_mode = "force_paper"
    db.add(strategy_config)
    db.flush()

    create_new_risk_limit_config_version(
        db, workspace.id, actor_user=authorized_user,
        max_trades_per_day=1, max_concurrent_positions=5,
    )

    # Dispatch (as paper, since force_paper) well past the cap -- must never
    # matter, since is_strategy_routed_live is False for this strategy the
    # whole time. Strikes start at 22100, clear of the option_contract
    # fixture's own 22000 (uq_option_contract_identity).
    for i in range(3):
        strike = 22100 + i * 50
        contract = OptionContract(
            id=uuid.uuid4(), instrument_id=option_contract.instrument_id,
            expiry_date=date(2026, 7, 30), strike=strike,
            option_type=OptionType.CE, symbol=f"NIFTY26JUL2{strike}CE-FP",
        )
        db.add(contract)
        db.flush()
        _dispatch(db, trading_session, strategy_run, contract, broker)

    final_contract = OptionContract(
        id=uuid.uuid4(), instrument_id=option_contract.instrument_id,
        expiry_date=date(2026, 7, 30), strike=22999,
        option_type=OptionType.CE, symbol="NIFTY26JUL22999CE-FP",
    )
    db.add(final_contract)
    db.flush()
    fourth = _make_trade_intent(db, trading_session, strategy_run, final_contract)
    decision = evaluate_trade_intent(db, fourth, trading_session, strategy_run)

    assert "max_trades_per_day_reached" not in decision.reasons


def test_daily_trade_cap_ignores_a_strategys_earlier_paper_era_dispatches(
    db: Session, broker, workspace, authorized_user, trading_session, strategy_run,
    strategy_config, option_contract, monkeypatch,
):
    """2026-08-19 regression (direction 2, under-restrictive protection): a
    strategy that dispatched several genuinely-paper trades earlier today
    (session was still `paper_only`, or the strategy itself was still
    `force_paper`) must not have those count against its cap once it
    becomes genuinely live -- the cap exists to protect real capital, and a
    strategy's first-ever live signal being blocked by trades that never
    touched real money is the cap doing the opposite of its job. Live
    incident: exactly this blocked a real strategy's first live signal the
    same day this was fixed, immediately after its force_paper override was
    removed.
    """
    # Earlier in the day: session paper_only, strategy trades freely as
    # paper (uncapped, same as test_paper_only_session_has_no_daily_trade_cap).
    create_new_risk_limit_config_version(
        db, workspace.id, actor_user=authorized_user,
        max_trades_per_day=1, max_concurrent_positions=5,
    )
    for _ in range(3):
        contract = OptionContract(
            id=uuid.uuid4(), instrument_id=option_contract.instrument_id,
            expiry_date=date(2026, 7, 30), strike=23000 + _ * 50,
            option_type=OptionType.CE, symbol=f"NIFTY26JUL2{23000 + _ * 50}CE-PE",
        )
        db.add(contract)
        db.flush()
        _dispatch(db, trading_session, strategy_run, contract, broker)

    # Later: strategy graduates to live, session goes live_enabled -- its
    # very first genuinely-live signal must still be evaluated on its own
    # (zero prior *live* dispatches), not blocked by the 3 paper ones above.
    trading_session.mode = SafeMode.LIVE_ENABLED
    db.add(trading_session)
    strategy_config.status = StrategyStatus.LIVE
    db.add(strategy_config)
    db.flush()
    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker",
        lambda trading_session, strategy_run=None: broker,
    )

    live_contract = OptionContract(
        id=uuid.uuid4(), instrument_id=option_contract.instrument_id,
        expiry_date=date(2026, 7, 30), strike=23999,
        option_type=OptionType.CE, symbol="NIFTY26JUL23999CE-LIVE",
    )
    db.add(live_contract)
    db.flush()
    live_intent = _make_trade_intent(db, trading_session, strategy_run, live_contract)
    decision = evaluate_trade_intent(db, live_intent, trading_session, strategy_run)

    assert "max_trades_per_day_reached" not in decision.reasons


def test_evaluate_trade_intent_consecutive_loss_pause(
    db: Session, broker, workspace, authorized_user, trading_session, strategy_run,
    strategy_config, option_contract, monkeypatch,
):
    """2026-08-19: gated on is_strategy_routed_live -- see this check's own
    comment in evaluate_trade_intent. Needs a genuinely live-routed
    strategy, same setup as the other rescoped checks in this file.
    """
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    db.add(trading_session)
    strategy_config.status = StrategyStatus.LIVE
    trading_session.consecutive_losses = 2  # RiskDefaults threshold is 2
    db.add(strategy_config)
    db.add(trading_session)
    db.flush()
    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker",
        lambda trading_session, strategy_run=None: broker,
    )

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert "consecutive_loss_pause_active" in decision.reasons


def test_consecutive_loss_pause_does_not_block_a_paper_routed_strategy(
    db: Session, workspace, authorized_user, trading_session, strategy_run, strategy_config,
    option_contract, monkeypatch,
):
    """The symmetric case: the live side having hit its consecutive-loss
    threshold must not stop a force_paper strategy from continuing.
    """
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    db.add(trading_session)
    strategy_config.runtime_mode = "force_paper"
    trading_session.consecutive_losses = 2
    db.add(strategy_config)
    db.add(trading_session)
    db.flush()
    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker",
        lambda trading_session, strategy_run=None: MockBrokerAdapter(),
    )

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert "consecutive_loss_pause_active" not in decision.reasons


def test_evaluate_trade_intent_per_trade_lot_cap_exceeded(
    db: Session, broker, workspace, authorized_user, trading_session, strategy_run,
    strategy_config, option_contract, monkeypatch,
):
    """2026-08-26: gated on is_strategy_routed_live -- see this check's own
    comment in evaluate_trade_intent. Needs a genuinely live-routed
    strategy, same setup as the other rescoped checks in this file.
    RiskDefaults.per_trade_lot_cap is 1.
    """
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    db.add(trading_session)
    strategy_config.status = StrategyStatus.LIVE
    db.add(strategy_config)
    db.flush()
    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker",
        lambda trading_session, strategy_run=None: broker,
    )

    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract, qty_lots=2
    )
    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert "per_trade_lot_cap_exceeded" in decision.reasons


def test_per_trade_lot_cap_does_not_block_a_paper_routed_strategy(
    db: Session, workspace, authorized_user, trading_session, strategy_run, strategy_config,
    option_contract, monkeypatch,
):
    """The bug this gate fixes: a FORCE_PAPER strategy's mode-aware default
    of 10 lots (`api.v1.strategies._DEFAULT_QTY_LOTS_PAPER`) must not be
    rejected against the live-safety `per_trade_lot_cap` of 1 -- confirmed
    live on 2026-08-26, every paper trade_intent across 5 running
    strategies was risk_rejected for this exact reason before this fix.
    """
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    db.add(trading_session)
    strategy_config.runtime_mode = "force_paper"
    db.add(strategy_config)
    db.flush()
    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker",
        lambda trading_session, strategy_run=None: MockBrokerAdapter(),
    )

    trade_intent = _make_trade_intent(
        db, trading_session, strategy_run, option_contract, qty_lots=10
    )
    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert "per_trade_lot_cap_exceeded" not in decision.reasons


def test_evaluate_trade_intent_budget_exceeded(
    db: Session, broker, workspace, authorized_user, trading_session, strategy_run,
    strategy_config, option_contract, monkeypatch,
):
    """2026-08-19: gated on is_strategy_routed_live -- a paper intent's own
    capital_required must never be checked against the real budget at all.
    """
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    trading_session.budget_amount = 1000  # 1 lot @ premium 80 * lot_size 25 = 2000 > 1000
    db.add(trading_session)
    strategy_config.status = StrategyStatus.LIVE
    db.add(strategy_config)
    db.flush()
    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker",
        lambda trading_session, strategy_run=None: broker,
    )

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert "budget_exceeded" in decision.reasons


def test_budget_exceeded_does_not_block_a_paper_routed_strategy(
    db: Session, trading_session, strategy_run, strategy_config, option_contract,
):
    """A force_paper strategy's own capital_required must never be checked
    against `budget_amount`, even when the budget is set impossibly low.
    """
    trading_session.budget_amount = 1  # would reject any real trade
    db.add(trading_session)
    strategy_config.runtime_mode = "force_paper"
    db.add(strategy_config)
    db.flush()

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert "budget_exceeded" not in decision.reasons


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
        lambda trading_session, strategy_run=None: _LowMarginBroker(),
    )

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert decision.decision == "rejected"
    assert "margin_check_failed" in decision.reasons


def test_evaluate_trade_intent_force_paper_strategy_ignores_real_margin_shortfall(
    db: Session, trading_session, strategy_run, strategy_config, option_contract, monkeypatch
):
    """Live bug fixed 2026-08-18: `_check_margin` used to call
    `get_execution_broker(trading_session)` without `strategy_run`, so a
    `force_paper` strategy's margin was checked against whatever
    `get_execution_broker` would resolve with no strategy context at all —
    the real broker, once the session itself reached `live_enabled`, per
    that function's own routing table. A strategy explicitly held back to
    paper should never be blocked by the real account's real cash shortfall
    — `get_execution_broker` already knows this (its `FORCE_PAPER` branch),
    `_check_margin` just wasn't telling it which strategy was asking.
    """
    strategy_config.runtime_mode = "force_paper"
    db.add(strategy_config)
    db.flush()

    class _LowMarginBroker(MockBrokerAdapter):
        def get_margin(self):
            return MarginInfo(
                available_margin=1.0, used_margin=0.0, total_margin=1.0, ts=datetime.now(UTC)
            )

    def _fake_get_execution_broker(trading_session, strategy_run=None):
        # A force_paper strategy must never even reach the low-margin real
        # broker stand-in — if it does, this test should fail loudly rather
        # than passing for the wrong reason.
        if strategy_run is not None:
            config = db.get(StrategyConfig, strategy_run.strategy_config_id)
            if config is not None and config.runtime_mode == "force_paper":
                return MockBrokerAdapter()
        return _LowMarginBroker()

    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker", _fake_get_execution_broker
    )

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    assert "margin_check_failed" not in decision.reasons


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


def test_evaluate_trade_intent_rejection_alert_message_has_friendly_context(
    db: Session, trading_session, strategy_run, option_contract
):
    """The alert message must carry readable context (strategy type,
    instrument, timestamp) alongside the raw TradeIntent id -- not just a
    bare UUID an operator would have to look up. The UUID itself must stay
    present too, unchanged, since it's still the real identifier.
    """
    trading_session.mode = SafeMode.KILL_SWITCH
    db.add(trading_session)
    db.flush()

    trade_intent = _make_trade_intent(db, trading_session, strategy_run, option_contract)
    evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    alert = (
        db.query(SystemAlert).filter(SystemAlert.trading_session_id == trading_session.id).one()
    )
    # strategy_config fixture defaults strategy_type to "synthetic";
    # instrument fixture is "NIFTY".
    assert "SYNTHETIC NIFTY" in alert.message
    assert str(trade_intent.id) in alert.message
    assert "TradeIntent " + str(trade_intent.id) + " rejected" not in alert.message


def test_evaluate_trade_intent_approval_required_creates_pending_approval(
    db: Session, broker, trading_session, strategy_config, user: User, option_contract, monkeypatch
):
    """Approval-required only actually gates a *live* trade (2026-08-21:
    paper trades always auto-dispatch regardless of execution_mode, see the
    sibling test below) -- must set up genuine live-routing here
    (trading_session.mode + strategy_config.status), same pattern the
    is_strategy_routed_live regression tests above already use, or this
    test would exercise the paper-auto-dispatch path instead. The
    get_execution_broker monkeypatch is the same one those tests use --
    is_strategy_routed_live itself never resolves a broker, but this
    module's actual dispatch path does, and would otherwise hit the real
    ALLOW_REAL_MONEY_DISPATCH gate in a test environment that doesn't set it.
    """
    trading_session.mode = SafeMode.LIVE_ENABLED
    db.add(trading_session)
    strategy_config.status = StrategyStatus.LIVE
    db.add(strategy_config)
    db.flush()
    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker",
        lambda trading_session, strategy_run=None: broker,
    )

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


def test_evaluate_trade_intent_approval_required_still_auto_dispatches_when_paper(
    db: Session, trading_session, strategy_config, user: User, option_contract
):
    """2026-08-21: a strategy set to Approval-required but actually routed
    paper (the default trading_session/strategy_config fixtures here are
    paper_only/not-force-live, i.e. is_strategy_routed_live is False) must
    still auto-dispatch -- approval-required exists to gate real-money
    risk, and a paper trade carries none. This is the actual new behavior;
    the sibling test above proves the untouched live case still gates."""
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
    assert trade_intent.status == TradeIntentStatus.DISPATCHED
    assert (
        db.query(PendingTradeApproval)
        .filter(PendingTradeApproval.trade_intent_id == trade_intent.id)
        .one_or_none()
        is None
    )


# -- expire_stale_pending_approvals: proactive expiry -------------------------


def test_expire_stale_pending_approvals_expires_past_window(
    db: Session, broker, trading_session, strategy_config, user: User, option_contract, monkeypatch
):
    """2026-08-21: needs genuine live-routing now that paper trades always
    auto-dispatch regardless of execution_mode -- a PENDING_APPROVAL state
    can only actually occur for a live-routed strategy any more."""
    trading_session.mode = SafeMode.LIVE_ENABLED
    db.add(trading_session)
    strategy_config.status = StrategyStatus.LIVE
    db.add(strategy_config)
    db.flush()
    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker",
        lambda trading_session, strategy_run=None: broker,
    )

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
    db: Session, broker, trading_session, strategy_config, user: User, option_contract, monkeypatch
):
    """2026-08-21: needs genuine live-routing, same reasoning as the sibling
    expiry test above."""
    trading_session.mode = SafeMode.LIVE_ENABLED
    db.add(trading_session)
    strategy_config.status = StrategyStatus.LIVE
    db.add(strategy_config)
    db.flush()
    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker",
        lambda trading_session, strategy_run=None: broker,
    )

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
    record_trade_outcome_effects(db, trading_session, realized_pnl=150.0, is_live=True)

    assert float(trading_session.cumulative_realized_pnl) == pytest.approx(150.0)
    assert trading_session.consecutive_losses == 0


def test_record_trade_outcome_effects_increments_consecutive_losses(db: Session, trading_session):
    record_trade_outcome_effects(db, trading_session, realized_pnl=-50.0, is_live=True)

    assert trading_session.consecutive_losses == 1


def test_record_trade_outcome_effects_breaching_loss_cap_triggers_kill_switch(
    db: Session, trading_session
):
    # daily_loss_cap is 1000 on the fixture session.
    record_trade_outcome_effects(db, trading_session, realized_pnl=-1500.0, is_live=True)

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
    record_trade_outcome_effects(db, trading_session, realized_pnl=2500.0, is_live=True)

    assert trading_session.entries_paused_reason == "daily_target_reached"
    # Not a mode transition — reaching a target is a goal, not a fault.
    assert trading_session.mode == SafeMode.PAPER_ONLY


def test_record_trade_outcome_effects_is_a_noop_for_paper(db: Session, trading_session):
    """2026-08-19 regression: the most severe gap from that day's audit —
    this function used to run unconditionally for every closed position,
    paper or live. A losing streak of pure paper trades could trip a real
    kill_switch or pause live entries via daily_target_profit, over a
    "loss"/"profit" that never touched real money. `is_live=False` must
    return before touching anything, not just before the loss-cap/target
    triggers -- proven here with a paper loss well past daily_loss_cap.
    """
    record_trade_outcome_effects(db, trading_session, realized_pnl=-1500.0, is_live=False)

    assert float(trading_session.cumulative_realized_pnl) == 0.0
    assert trading_session.consecutive_losses == 0
    assert trading_session.mode == SafeMode.PAPER_ONLY
    assert trading_session.entries_paused_reason is None


def test_entries_paused_blocks_further_trade_intents(
    db: Session, workspace, authorized_user, trading_session, strategy_run, strategy_config,
    option_contract, instrument, broker, monkeypatch,
):
    """`entries_paused_reason` here is `DAILY_TARGET_REACHED` — P&L-driven,
    live-only after 2026-08-19 (see `record_trade_outcome_effects`'s own
    docstring) — so this must exercise a genuinely live-routed strategy_run
    to still prove the block, same setup pattern as `test_evaluate_trade_
    intent_max_trades_per_day`.
    """
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    db.add(trading_session)
    strategy_config.status = StrategyStatus.LIVE
    db.add(strategy_config)
    db.flush()
    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker",
        lambda trading_session, strategy_run=None: broker,
    )

    record_trade_outcome_effects(db, trading_session, realized_pnl=2500.0, is_live=True)
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


def test_daily_target_entries_paused_does_not_block_a_paper_routed_strategy(
    db: Session, workspace, authorized_user, trading_session, strategy_run, strategy_config,
    option_contract, instrument, monkeypatch,
):
    """2026-08-19 regression: the live side hitting its daily profit target
    must not stop a `force_paper` strategy from continuing to test, on a
    session that's otherwise live-capable (a mixed day) -- entries_paused_
    reason=DAILY_TARGET_REACHED is P&L-driven, so it's gated the same way
    as max_concurrent_positions/budget_exceeded/consecutive_loss_pause.
    """
    trading_session.mode = SafeMode.PAPER_PLUS_GUARDED_LIVE
    db.add(trading_session)
    strategy_config.runtime_mode = "force_paper"
    db.add(strategy_config)
    db.flush()

    # Simulate the live side having already hit its target (session-level
    # state -- doesn't matter which strategy's live close produced it).
    trading_session.entries_paused_reason = "daily_target_reached"
    db.add(trading_session)
    db.flush()

    monkeypatch.setattr(
        "app.modules.risk_engine.service.get_execution_broker",
        lambda trading_session, strategy_run=None: MockBrokerAdapter(),
    )

    decision = evaluate_trade_intent(
        db,
        _make_trade_intent(db, trading_session, strategy_run, option_contract),
        trading_session,
        strategy_run,
    )

    assert not any(r.startswith("entries_paused") for r in decision.reasons)


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
