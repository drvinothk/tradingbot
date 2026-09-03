"""Ops-Hardening Phase 2: strategy_engine.runner._check_runner_watchdog --
alerts when a run with an open position hasn't evaluated a fresh bar
recently. Exercises the function directly (not the full run_cycle/evaluate
pipeline) since it's a self-contained check with its own clear inputs.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

import app.modules.strategy_engine.runner as runner_module
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.market.models import Instrument, PriceBar
from app.domain.ops.models import SystemAlert
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import ExecutionMode, StrategyConfig, StrategyRun, StrategyRunStatus
from app.modules.strategy_engine.runner import _check_runner_watchdog


def _same_session_factory(db: Session):
    """Wraps the test's own isolated `db` fixture in the `SessionFactory`
    shape `_check_runner_watchdog` expects, so its alert write lands in the
    same rolled-back-at-teardown transaction as everything else in the test
    -- the exact same `_same_session` pattern `run_cycle` itself already
    uses internally for its option-chain refresh. Without this, the
    function's own default (`session_scope`) would open a second, real
    connection straight to the engine, committing for real and invisible to
    this test's own `db`-scoped assertions — precisely the trap this
    project's own CLAUDE.md already documents hitting once before
    (PositionManager's live-subscribe call defaulting to production
    `session_scope` inside a test).
    """

    @contextmanager
    def _factory() -> Generator[Session, None, None]:
        yield db

    return _factory


@pytest.fixture(autouse=True)
def _reset_throttle_state():
    """`_last_stall_alert_at` is a module-level dict, keyed by strategy_run
    id -- fresh uuids per test mean no cross-test collision in practice, but
    clearing it anyway keeps this file's tests order-independent and the
    intent explicit.
    """
    runner_module._last_stall_alert_at.clear()
    yield
    runner_module._last_stall_alert_at.clear()


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="watchdog-test-account",
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
    config = StrategyConfig(id=uuid.uuid4(), workspace_id=workspace.id, name="orb-watchdog-test")
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
        status=StrategyRunStatus.IN_POSITION,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
    )
    db.add(run)
    db.flush()
    return run


def _bar(instrument: Instrument, *, age_seconds: float) -> PriceBar:
    return PriceBar(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        timeframe="60s",
        bucket_start=datetime.now(UTC) - timedelta(seconds=age_seconds),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1000,
    )


def _alerts_for(db: Session, trading_session: TradingSession) -> list[SystemAlert]:
    return db.query(SystemAlert).filter(SystemAlert.trading_session_id == trading_session.id).all()


def test_no_alert_without_an_open_position(
    db: Session, strategy_run, trading_session, instrument, monkeypatch
):
    monkeypatch.setattr(runner_module, "is_within_market_hours", lambda: True)
    stale_bar = _bar(instrument, age_seconds=400)

    _check_runner_watchdog(
        strategy_run,
        trading_session,
        stale_bar,
        has_open_position=False,
        session_factory=_same_session_factory(db),
    )

    assert _alerts_for(db, trading_session) == []


def test_no_alert_outside_market_hours(
    db: Session, strategy_run, trading_session, instrument, monkeypatch
):
    monkeypatch.setattr(runner_module, "is_within_market_hours", lambda: False)
    stale_bar = _bar(instrument, age_seconds=400)

    _check_runner_watchdog(
        strategy_run,
        trading_session,
        stale_bar,
        has_open_position=True,
        session_factory=_same_session_factory(db),
    )

    assert _alerts_for(db, trading_session) == []


def test_no_alert_when_bar_is_fresh(
    db: Session, strategy_run, trading_session, instrument, monkeypatch
):
    monkeypatch.setattr(runner_module, "is_within_market_hours", lambda: True)
    fresh_bar = _bar(instrument, age_seconds=5)

    _check_runner_watchdog(
        strategy_run,
        trading_session,
        fresh_bar,
        has_open_position=True,
        session_factory=_same_session_factory(db),
    )

    assert _alerts_for(db, trading_session) == []


def test_alerts_when_bar_is_stale(
    db: Session, strategy_run, trading_session, instrument, monkeypatch
):
    monkeypatch.setattr(runner_module, "is_within_market_hours", lambda: True)
    stale_bar = _bar(instrument, age_seconds=200)  # > 180s stale_after_seconds

    _check_runner_watchdog(
        strategy_run,
        trading_session,
        stale_bar,
        has_open_position=True,
        session_factory=_same_session_factory(db),
    )

    alerts = _alerts_for(db, trading_session)
    assert len(alerts) == 1
    assert alerts[0].category == "strategy_run_stalled"
    assert str(strategy_run.id) in alerts[0].message


def test_alerts_immediately_when_no_bar_ever_recorded(
    db: Session, strategy_run, trading_session, monkeypatch
):
    # The specific regression this test pins: window_ts's own now_ist()
    # fallback would read as "perfectly fresh" for this exact case --
    # latest_bar=None must NOT be silently treated as fine.
    monkeypatch.setattr(runner_module, "is_within_market_hours", lambda: True)

    _check_runner_watchdog(
        strategy_run,
        trading_session,
        None,
        has_open_position=True,
        session_factory=_same_session_factory(db),
    )

    alerts = _alerts_for(db, trading_session)
    assert len(alerts) == 1
    assert "no bar ever recorded" in alerts[0].message


def test_repeated_stale_cycles_are_throttled(
    db: Session, strategy_run, trading_session, instrument, monkeypatch
):
    monkeypatch.setattr(runner_module, "is_within_market_hours", lambda: True)
    stale_bar = _bar(instrument, age_seconds=200)

    _check_runner_watchdog(
        strategy_run,
        trading_session,
        stale_bar,
        has_open_position=True,
        session_factory=_same_session_factory(db),
    )
    _check_runner_watchdog(
        strategy_run,
        trading_session,
        stale_bar,
        has_open_position=True,
        session_factory=_same_session_factory(db),
    )
    _check_runner_watchdog(
        strategy_run,
        trading_session,
        stale_bar,
        has_open_position=True,
        session_factory=_same_session_factory(db),
    )

    assert len(_alerts_for(db, trading_session)) == 1


def test_realerts_once_throttle_window_has_elapsed(
    db: Session, strategy_run, trading_session, instrument, monkeypatch
):
    monkeypatch.setattr(runner_module, "is_within_market_hours", lambda: True)
    stale_bar = _bar(instrument, age_seconds=200)

    _check_runner_watchdog(
        strategy_run,
        trading_session,
        stale_bar,
        has_open_position=True,
        session_factory=_same_session_factory(db),
    )
    assert len(_alerts_for(db, trading_session)) == 1

    # Simulate the throttle window having elapsed, rather than sleeping the
    # test for 300 real seconds.
    runner_module._last_stall_alert_at[strategy_run.id] = datetime.now(UTC) - timedelta(
        seconds=runner_module.RUNNER_STALL_ALERT_THROTTLE_SECONDS + 1
    )

    _check_runner_watchdog(
        strategy_run,
        trading_session,
        stale_bar,
        has_open_position=True,
        session_factory=_same_session_factory(db),
    )

    # 2026-09-03: send_alert now collapses a recurring same-dedup_key alert
    # into one row (occurrence_count++) instead of inserting a new row --
    # this second call, made well within the 24h collapse window, lands on
    # the same row rather than creating a second one. occurrence_count is
    # the new observable that the second alert genuinely happened.
    alerts = _alerts_for(db, trading_session)
    assert len(alerts) == 1
    assert alerts[0].occurrence_count == 2


def test_separate_runs_are_throttled_independently(
    db: Session, strategy_run, trading_session, strategy_config, instrument, user, monkeypatch
):
    monkeypatch.setattr(runner_module, "is_within_market_hours", lambda: True)
    stale_bar = _bar(instrument, age_seconds=200)

    other_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=strategy_config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.IN_POSITION,
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
    )
    db.add(other_run)
    db.flush()

    _check_runner_watchdog(
        strategy_run,
        trading_session,
        stale_bar,
        has_open_position=True,
        session_factory=_same_session_factory(db),
    )
    _check_runner_watchdog(
        other_run,
        trading_session,
        stale_bar,
        has_open_position=True,
        session_factory=_same_session_factory(db),
    )

    assert len(_alerts_for(db, trading_session)) == 2
