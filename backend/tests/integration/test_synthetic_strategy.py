"""Full Signal -> TradeIntent -> RiskDecision -> audit loop, driven by the
Phase 2 synthetic strategy stub — this is the "done when" scenario from the
build plan's Phase 2 section: synthetic TradeIntents approved/rejected per
configurable limits, every decision carrying a pre-trade analytics snapshot,
every decision audited, and a breached limit visibly blocking further
approvals plus raising an alert.
"""

from __future__ import annotations

import random
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.domain.audit.models import AuditEvent
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.market.models import Instrument, OptionChainSnapshot, OptionContract, OptionType
from app.domain.market.models import QuoteTick as QuoteTickRow
from app.domain.ops.models import SystemAlert
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import (
    ExecutionMode,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    SyntheticTradeOutcome,
    TradeIntent,
    TradeIntentStatus,
)
from app.modules.audit_service.service import verify_chain
from app.modules.risk_engine.service import create_new_risk_limit_config_version
from app.modules.strategy_engine.strategies.synthetic import (
    SyntheticStrategy,
    SyntheticStrategyRunner,
)

EXPIRY = date(2026, 7, 30)


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="synthetic-test-account",
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
        daily_target_profit=1_000_000,  # high enough that the target trigger doesn't interfere
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
        symbol="NIFTY26JUL22000CE",
    )
    db.add(contract)
    db.flush()
    return contract


@pytest.fixture
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(id=uuid.uuid4(), workspace_id=workspace.id, name="synthetic")
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


def _seed_market_data(
    db: Session, instrument: Instrument, option_contract: OptionContract, *, spot: float = 22000.0
) -> None:
    now = datetime.now(UTC)
    db.add(
        QuoteTickRow(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            ltp=spot,
            bid=spot - 1,
            ask=spot + 1,
            volume=10000,
            oi=None,
            ts=now,
        )
    )
    db.add(
        OptionChainSnapshot(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            expiry_date=EXPIRY,
            ts=now,
            chain_data=[
                {
                    "contract_symbol": option_contract.symbol,
                    "strike": float(option_contract.strike),
                    "option_type": OptionType(option_contract.option_type).value,
                    "ltp": 80.0,
                    "bid": 79.5,
                    "ask": 80.5,
                    "volume": 5000,
                    "oi": 20000,
                }
            ],
        )
    )
    db.flush()


def test_run_cycle_with_no_market_data_yields_nothing(
    db: Session, instrument: Instrument, strategy_run, trading_session, strategy_config
):
    strategy = SyntheticStrategy(instrument_id=instrument.id, expiry_date=EXPIRY)
    decision = strategy.run_cycle(db, strategy_run, trading_session, strategy_config)
    assert decision is None


def test_run_cycle_dispatches_and_closes_and_audits_full_loop(
    db: Session, instrument, option_contract, strategy_run, trading_session, strategy_config
):
    _seed_market_data(db, instrument, option_contract)

    strategy = SyntheticStrategy(
        instrument_id=instrument.id, expiry_date=EXPIRY, rng=random.Random(42)
    )
    decision = strategy.run_cycle(db, strategy_run, trading_session, strategy_config)

    assert decision is not None
    assert decision.decision == "approved"
    assert decision.capital_required == pytest.approx(80.0 * 25 * 1)

    trade_intent = db.get(TradeIntent, decision.trade_intent_id)
    assert trade_intent is not None
    assert trade_intent.status == TradeIntentStatus.DISPATCHED

    outcome = (
        db.query(SyntheticTradeOutcome)
        .filter(SyntheticTradeOutcome.trade_intent_id == trade_intent.id)
        .one_or_none()
    )
    assert outcome is not None, "run_cycle should synthetically close a dispatched intent"

    events = (
        db.query(AuditEvent).filter(AuditEvent.trading_session_id == trading_session.id).all()
    )
    event_types = {e.event_type for e in events}
    assert "signal.generated" in event_types
    assert "risk_decision.approved.dispatched" in event_types
    assert "synthetic_trade_outcome.recorded" in event_types

    ok, broken_id = verify_chain(db)
    assert ok, f"audit chain broken at {broken_id}"


def test_repeated_cycles_hit_max_trades_per_day_and_alert_fires(
    db: Session, workspace, authorized_user, instrument, option_contract,
    strategy_run, trading_session, strategy_config,
):
    _seed_market_data(db, instrument, option_contract)
    create_new_risk_limit_config_version(
        db,
        workspace.id,
        actor_user=authorized_user,
        max_trades_per_day=1,
        max_concurrent_positions=5,
        consecutive_loss_pause_threshold=100,
    )

    strategy = SyntheticStrategy(
        instrument_id=instrument.id, expiry_date=EXPIRY, rng=random.Random(7)
    )

    first = strategy.run_cycle(db, strategy_run, trading_session, strategy_config)
    assert first is not None
    assert first.decision == "approved"

    second = strategy.run_cycle(db, strategy_run, trading_session, strategy_config)
    assert second is not None
    assert second.decision == "rejected"
    assert "max_trades_per_day_reached" in second.reasons

    alerts = (
        db.query(SystemAlert)
        .filter(
            SystemAlert.trading_session_id == trading_session.id,
            SystemAlert.category == "risk_limit_breach",
        )
        .all()
    )
    assert len(alerts) == 1

    ok, broken_id = verify_chain(db)
    assert ok, f"audit chain broken at {broken_id}"


@pytest.fixture
def real_commit_factory(engine):
    """Mirrors core.db.session.session_scope's contract but bound to the
    isolated test engine — SyntheticStrategyRunner runs its loop on a
    background thread, which needs its own real-commit session (the `db`
    fixture's rolled-back, single-connection transaction is invisible to any
    other connection, including a background thread's), same reasoning as
    test_market_data_ingestion.py's `test_session_factory`.
    """
    session_factory = sessionmaker(bind=engine, future=True)

    @contextmanager
    def _scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _scope


def test_runner_executes_on_a_timer_and_stops_cleanly(real_commit_factory):
    """The actual "on a timer" mechanism the Phase 2 build plan bullet
    describes — everything else in this file exercises `run_cycle` directly,
    never the background-thread runner itself. Uses its own fully
    real-committed fixture data (not the shared `db`/`workspace`/`user`
    fixtures, which are rolled back and invisible to the runner's separate
    connection) and cleans up explicitly, in FK-safe order, in a
    try/finally — the exact trap this project's QC process has hit before.
    """
    ids: dict[str, uuid.UUID] = {}
    try:
        with real_commit_factory() as db:
            from app.core.security.passwords import hash_password
            from app.domain.identity.models import BrokerAccount as BrokerAccountRow
            from app.domain.identity.models import User as UserRow
            from app.domain.identity.models import Workspace as WorkspaceRow

            workspace = WorkspaceRow(id=uuid.uuid4(), name=f"runner-test-{uuid.uuid4().hex[:8]}")
            db.add(workspace)
            db.flush()
            ids["workspace_id"] = workspace.id

            user = UserRow(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                email=f"runner-{uuid.uuid4().hex[:8]}@example.com",
                password_hash=hash_password("correct horse battery staple"),
                display_name="Runner Test User",
                is_active=True,
            )
            db.add(user)
            db.flush()
            ids["user_id"] = user.id

            broker_account = BrokerAccountRow(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                broker_type=BrokerType.SHOONYA,
                label="runner-test-account",
                credentials_ref="config/credentials/shoonya.env",
                status=BrokerAccountStatus.ACTIVE,
            )
            db.add(broker_account)
            db.flush()
            ids["broker_account_id"] = broker_account.id

            trading_session = TradingSession(
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
            db.add(trading_session)
            db.flush()
            ids["trading_session_id"] = trading_session.id

            instrument = Instrument(
                id=uuid.uuid4(), symbol="NIFTY-RUNNER", exchange="NFO", lot_size=25, tick_size=0.05
            )
            db.add(instrument)
            db.flush()
            ids["instrument_id"] = instrument.id

            option_contract = OptionContract(
                id=uuid.uuid4(),
                instrument_id=instrument.id,
                expiry_date=EXPIRY,
                strike=22000,
                option_type=OptionType.CE,
                symbol="NIFTY-RUNNER-26JUL22000CE",
            )
            db.add(option_contract)
            db.flush()

            strategy_config = StrategyConfig(
                id=uuid.uuid4(), workspace_id=workspace.id, name="runner-test-strategy"
            )
            db.add(strategy_config)
            db.flush()
            ids["strategy_config_id"] = strategy_config.id

            strategy_run = StrategyRun(
                id=uuid.uuid4(),
                strategy_config_id=strategy_config.id,
                trading_session_id=trading_session.id,
                execution_mode=ExecutionMode.AUTO,
                status=StrategyRunStatus.SCANNING,
                started_at=datetime.now(UTC),
                started_by_user_id=user.id,
            )
            db.add(strategy_run)
            db.flush()
            ids["strategy_run_id"] = strategy_run.id

            _seed_market_data(db, instrument, option_contract)

        strategy = SyntheticStrategy(
            instrument_id=ids["instrument_id"], expiry_date=EXPIRY, rng=random.Random(11)
        )
        runner = SyntheticStrategyRunner(
            strategy,
            ids["strategy_run_id"],
            interval_seconds=0.05,
            session_factory=real_commit_factory,
        )
        runner.start()
        time.sleep(0.4)
        runner.stop()

        with real_commit_factory() as verify_db:
            intents = (
                verify_db.query(TradeIntent)
                .filter(TradeIntent.strategy_run_id == ids["strategy_run_id"])
                .all()
            )
            assert len(intents) >= 1, "runner should have produced at least one TradeIntent"
            assert all(i.status == TradeIntentStatus.DISPATCHED for i in intents)

            events = (
                verify_db.query(AuditEvent)
                .filter(AuditEvent.trading_session_id == ids["trading_session_id"])
                .all()
            )
            assert any(e.event_type == "signal.generated" for e in events)
    finally:
        with real_commit_factory() as cleanup_db:
            from app.domain.identity.models import BrokerAccount as BrokerAccountRow
            from app.domain.identity.models import User as UserRow
            from app.domain.identity.models import Workspace as WorkspaceRow
            from app.domain.risk.models import RiskDecision

            trade_intent_ids = cleanup_db.query(TradeIntent.id).filter(
                TradeIntent.strategy_run_id == ids.get("strategy_run_id")
            )
            cleanup_db.query(SyntheticTradeOutcome).filter(
                SyntheticTradeOutcome.trade_intent_id.in_(trade_intent_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(RiskDecision).filter(
                RiskDecision.trade_intent_id.in_(trade_intent_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(TradeIntent).filter(
                TradeIntent.strategy_run_id == ids.get("strategy_run_id")
            ).delete()
            if "trading_session_id" in ids:
                from app.domain.strategy.models import Signal

                cleanup_db.query(Signal).filter(
                    Signal.trading_session_id == ids["trading_session_id"]
                ).delete()
            if "strategy_run_id" in ids:
                cleanup_db.query(StrategyRun).filter(
                    StrategyRun.id == ids["strategy_run_id"]
                ).delete()
            if "strategy_config_id" in ids:
                cleanup_db.query(StrategyConfig).filter(
                    StrategyConfig.id == ids["strategy_config_id"]
                ).delete()
            if "instrument_id" in ids:
                cleanup_db.query(OptionContract).filter(
                    OptionContract.instrument_id == ids["instrument_id"]
                ).delete()
                cleanup_db.query(QuoteTickRow).filter(
                    QuoteTickRow.instrument_id == ids["instrument_id"]
                ).delete()
                cleanup_db.query(OptionChainSnapshot).filter(
                    OptionChainSnapshot.instrument_id == ids["instrument_id"]
                ).delete()
            if "trading_session_id" in ids:
                from app.domain.ops.models import SystemAlert as SystemAlertRow

                cleanup_db.query(SystemAlertRow).filter(
                    SystemAlertRow.trading_session_id == ids["trading_session_id"]
                ).delete()
                cleanup_db.query(AuditEvent).filter(
                    AuditEvent.trading_session_id == ids["trading_session_id"]
                ).delete()
                cleanup_db.query(TradingSession).filter(
                    TradingSession.id == ids["trading_session_id"]
                ).delete()
            if "instrument_id" in ids:
                cleanup_db.query(Instrument).filter(
                    Instrument.id == ids["instrument_id"]
                ).delete()
            if "broker_account_id" in ids:
                cleanup_db.query(BrokerAccountRow).filter(
                    BrokerAccountRow.id == ids["broker_account_id"]
                ).delete()
            if "user_id" in ids:
                cleanup_db.query(UserRow).filter(UserRow.id == ids["user_id"]).delete()
            if "workspace_id" in ids:
                from app.domain.risk.models import RiskLimitConfig

                # get_active_risk_limit_config lazily seeds one for a
                # workspace on first use (see risk_engine.service) — the
                # run_cycle calls above will have created exactly this for
                # a brand-new workspace, so it must be cleared before the
                # workspace itself can be deleted.
                cleanup_db.query(RiskLimitConfig).filter(
                    RiskLimitConfig.workspace_id == ids["workspace_id"]
                ).delete()
                cleanup_db.query(WorkspaceRow).filter(
                    WorkspaceRow.id == ids["workspace_id"]
                ).delete()
