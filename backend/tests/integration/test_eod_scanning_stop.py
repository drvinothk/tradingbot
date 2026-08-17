"""EOD-stop for scanning-only strategy runs, 2026-08-17:
strategy_engine.runner._maybe_stop_for_eod -- self-stops a still-SCANNING
run (zero open positions) once wall-clock passes EOD_SCANNING_STOP_TIME
(15:10 IST). Exercises the function directly, same reasoning as
test_runner_watchdog.py's own intro comment -- a self-contained check with
clear inputs, no need to drive the full run_cycle/evaluate pipeline.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

import app.modules.strategy_engine.runner as runner_module
from app.domain.audit.models import AuditEvent
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.market.models import Instrument
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import ExecutionMode, StrategyConfig, StrategyRun, StrategyRunStatus
from app.modules.strategy_engine.runner import StrategyRunner, _maybe_stop_for_eod
from app.modules.strategy_engine.strategies.synthetic import SyntheticStrategy


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="eod-stop-test-account",
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
def strategy_config(db: Session, workspace) -> StrategyConfig:
    config = StrategyConfig(id=uuid.uuid4(), workspace_id=workspace.id, name="orb-eod-stop-test")
    db.add(config)
    db.flush()
    return config


@pytest.fixture
def strategy_run(
    db: Session, strategy_config: StrategyConfig, trading_session, instrument, user: User
) -> StrategyRun:
    run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
        instrument_id=instrument.id,
        expiry_date=None,
        interval_seconds=30.0,
    )
    db.add(run)
    db.flush()
    return run


def _eod_events_for(db: Session, strategy_run: StrategyRun) -> list[AuditEvent]:
    return (
        db.query(AuditEvent)
        .filter(
            AuditEvent.entity_id == strategy_run.id,
            AuditEvent.event_type == "strategy_run.eod_stopped",
        )
        .all()
    )


def test_no_stop_before_eod_scanning_stop_time(
    db: Session, strategy_run, trading_session, instrument, monkeypatch
):
    monkeypatch.setattr(runner_module, "is_past_eod_scanning_stop", lambda ts: False)

    _maybe_stop_for_eod(db, strategy_run, trading_session, instrument.id)

    assert strategy_run.status == StrategyRunStatus.SCANNING
    assert strategy_run.stopped_at is None
    assert _eod_events_for(db, strategy_run) == []


def test_stops_run_with_zero_positions_past_eod_time(
    db: Session, strategy_run, trading_session, instrument, monkeypatch
):
    monkeypatch.setattr(runner_module, "is_past_eod_scanning_stop", lambda ts: True)

    _maybe_stop_for_eod(db, strategy_run, trading_session, instrument.id)

    assert strategy_run.status == StrategyRunStatus.STOPPED
    assert strategy_run.stopped_at is not None

    events = _eod_events_for(db, strategy_run)
    assert len(events) == 1
    assert events[0].actor_type == "system"
    assert events[0].actor_id is None


def test_unsubscribes_when_no_other_active_run_on_the_instrument(
    db: Session, strategy_run, trading_session, instrument, monkeypatch
):
    monkeypatch.setattr(runner_module, "is_past_eod_scanning_stop", lambda ts: True)
    unsubscribed: list[str] = []
    monkeypatch.setattr(runner_module, "unsubscribe_symbol", unsubscribed.append)

    _maybe_stop_for_eod(db, strategy_run, trading_session, instrument.id)

    assert unsubscribed == ["NIFTY"]


def test_does_not_unsubscribe_while_a_sibling_run_is_still_active_on_the_instrument(
    db: Session, strategy_run, trading_session, strategy_config, instrument, user: User, monkeypatch
):
    """The real reason this check exists: market_data.registry subscribes by
    *underlying* symbol, shared across every concurrent run on it (see that
    module's own docstring) -- unsubscribing here without checking for a
    still-active sibling would silently kill ticks for it, including a
    sibling that's IN_POSITION and depends on that live feed for pricing.
    """
    other_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.IN_POSITION,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
        instrument_id=instrument.id,
    )
    db.add(other_run)
    db.flush()

    monkeypatch.setattr(runner_module, "is_past_eod_scanning_stop", lambda ts: True)
    unsubscribed: list[str] = []
    monkeypatch.setattr(runner_module, "unsubscribe_symbol", unsubscribed.append)

    _maybe_stop_for_eod(db, strategy_run, trading_session, instrument.id)

    # This run itself must still stop -- only the shared-resource
    # unsubscribe is what defers to the sibling.
    assert strategy_run.status == StrategyRunStatus.STOPPED
    assert unsubscribed == []


def test_unsubscribes_once_the_last_sibling_is_already_stopped(
    db: Session, strategy_run, trading_session, strategy_config, instrument, user: User, monkeypatch
):
    other_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.STOPPED,
        started_at=datetime.now(UTC),
        stopped_at=datetime.now(UTC),
        started_by_user_id=user.id,
        instrument_id=instrument.id,
    )
    db.add(other_run)
    db.flush()

    monkeypatch.setattr(runner_module, "is_past_eod_scanning_stop", lambda ts: True)
    unsubscribed: list[str] = []
    monkeypatch.setattr(runner_module, "unsubscribe_symbol", unsubscribed.append)

    _maybe_stop_for_eod(db, strategy_run, trading_session, instrument.id)

    assert unsubscribed == ["NIFTY"]


def test_does_not_unsubscribe_for_a_different_instrument(
    db: Session, strategy_run, trading_session, strategy_config, instrument, user: User, monkeypatch
):
    other_instrument = Instrument(
        id=uuid.uuid4(), symbol="BANKNIFTY", exchange="NFO", lot_size=15, tick_size=0.05
    )
    db.add(other_instrument)
    db.flush()
    other_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
        instrument_id=other_instrument.id,
    )
    db.add(other_run)
    db.flush()

    monkeypatch.setattr(runner_module, "is_past_eod_scanning_stop", lambda ts: True)
    unsubscribed: list[str] = []
    monkeypatch.setattr(runner_module, "unsubscribe_symbol", unsubscribed.append)

    _maybe_stop_for_eod(db, strategy_run, trading_session, instrument.id)

    # A still-active run on an unrelated instrument must not block this
    # instrument's own unsubscribe.
    assert unsubscribed == ["NIFTY"]


@pytest.fixture
def real_commit_factory(engine):
    """Same shape as test_synthetic_strategy.py's own fixture of the same
    name -- StrategyRunner runs on a background thread, which needs a real-
    commit session bound to the isolated test engine, not the `db` fixture's
    single-connection rolled-back transaction (invisible to any other
    connection).
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


def test_runner_self_stops_and_fires_on_self_stop_callback(real_commit_factory, monkeypatch):
    """The actual background-thread path, not just _maybe_stop_for_eod in
    isolation: is_past_eod_scanning_stop patched True from the first cycle,
    so a freshly-started SCANNING run (zero positions, nothing to evaluate)
    should self-stop within a couple of cycles and the runner's
    on_self_stop callback (StrategyRunner's `_RUNNERS.pop` equivalent, see
    api.v1.strategies.start_strategy) should fire exactly once.
    """
    monkeypatch.setattr(runner_module, "is_past_eod_scanning_stop", lambda ts: True)
    # Also keep the evaluate/submit block from ever firing a real signal --
    # this test only cares about the self-stop mechanism, not real dispatch
    # timing, and a genuinely dispatched trade would open a position and
    # (correctly) suppress EOD-stop entirely, which would make this test
    # flaky rather than deterministic.
    monkeypatch.setattr(runner_module, "is_within_global_trading_window", lambda ts: False)

    ids: dict[str, uuid.UUID] = {}
    try:
        with real_commit_factory() as db:
            from app.core.security.passwords import hash_password
            from app.domain.identity.models import BrokerAccount as BrokerAccountRow
            from app.domain.identity.models import User as UserRow
            from app.domain.identity.models import Workspace as WorkspaceRow

            workspace = WorkspaceRow(
                id=uuid.uuid4(), name=f"eod-runner-test-{uuid.uuid4().hex[:8]}"
            )
            db.add(workspace)
            db.flush()
            ids["workspace_id"] = workspace.id

            user = UserRow(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                email=f"eod-runner-{uuid.uuid4().hex[:8]}@example.com",
                password_hash=hash_password("correct horse battery staple"),
                display_name="EOD Runner Test User",
                is_active=True,
            )
            db.add(user)
            db.flush()
            ids["user_id"] = user.id

            broker_account = BrokerAccountRow(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                broker_type=BrokerType.SHOONYA,
                label="eod-runner-test-account",
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
                id=uuid.uuid4(),
                symbol="NIFTY-EOD-RUNNER",
                exchange="NFO",
                lot_size=25,
                tick_size=0.05,
            )
            db.add(instrument)
            db.flush()
            ids["instrument_id"] = instrument.id

            strategy_config = StrategyConfig(
                id=uuid.uuid4(), workspace_id=workspace.id, name="eod-runner-test-strategy"
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
                instrument_id=instrument.id,
            )
            db.add(strategy_run)
            db.flush()
            ids["strategy_run_id"] = strategy_run.id

        from datetime import date

        strategy = SyntheticStrategy(
            instrument_id=ids["instrument_id"], expiry_date=date(2099, 1, 1)
        )
        self_stop_calls: list[uuid.UUID] = []
        runner = StrategyRunner(
            strategy,
            ids["strategy_run_id"],
            interval_seconds=0.05,
            session_factory=real_commit_factory,
            on_self_stop=lambda: self_stop_calls.append(ids["strategy_run_id"]),
        )
        runner.start()
        time.sleep(0.6)

        assert self_stop_calls == [ids["strategy_run_id"]]

        with real_commit_factory() as verify_db:
            refreshed = verify_db.get(StrategyRun, ids["strategy_run_id"])
            assert refreshed.status == StrategyRunStatus.STOPPED
            assert refreshed.stopped_at is not None
    finally:
        with real_commit_factory() as cleanup_db:
            from app.domain.identity.models import BrokerAccount as BrokerAccountRow
            from app.domain.identity.models import User as UserRow
            from app.domain.identity.models import Workspace as WorkspaceRow

            cleanup_db.query(AuditEvent).filter(
                AuditEvent.workspace_id == ids.get("workspace_id")
            ).delete()
            cleanup_db.query(StrategyRun).filter(
                StrategyRun.id == ids.get("strategy_run_id")
            ).delete()
            cleanup_db.query(StrategyConfig).filter(
                StrategyConfig.id == ids.get("strategy_config_id")
            ).delete()
            cleanup_db.query(TradingSession).filter(
                TradingSession.id == ids.get("trading_session_id")
            ).delete()
            cleanup_db.query(Instrument).filter(Instrument.id == ids.get("instrument_id")).delete()
            cleanup_db.query(BrokerAccountRow).filter(
                BrokerAccountRow.id == ids.get("broker_account_id")
            ).delete()
            cleanup_db.query(UserRow).filter(UserRow.id == ids.get("user_id")).delete()
            cleanup_db.query(WorkspaceRow).filter(
                WorkspaceRow.id == ids.get("workspace_id")
            ).delete()
            cleanup_db.commit()
