"""GET /strategies/running's `is_live` field — whether a strategy run would
actually resolve to the real broker right now, per the shared
`is_strategy_routed_live` predicate (broker_adapter.composition), not just
"session is live_enabled". Added for the Control Room UI's "Today's
Activity" paper/live scope toggle; getting this distinction wrong caused two
real, opposite-direction production incidents on 2026-08-19 (see that
predicate's own docstring) so this exercises the same three cases directly
against the endpoint function, not just the predicate in isolation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.api.v1.strategies import RunningStrategyOut, list_running_strategies
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import (
    ExecutionMode,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    StrategyRuntimeMode,
)


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="is-live-test-account",
        credentials_ref="config/credentials/shoonya.env",
        status=BrokerAccountStatus.ACTIVE,
    )
    db.add(account)
    db.flush()
    return account


def _make_session(db: Session, workspace, broker_account, user: User, *, mode: SafeMode):
    ts = TradingSession(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_account_id=broker_account.id,
        started_by_user_id=user.id,
        mode=mode,
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
    db: Session,
    workspace,
    trading_session: TradingSession,
    user: User,
    *,
    runtime_mode: StrategyRuntimeMode | None = None,
):
    config = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        name=f"is-live-test-{uuid.uuid4().hex[:6]}",
        strategy_type="orb",
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
        started_at=datetime.now(UTC),
        started_by_user_id=user.id,
    )
    db.add(run)
    db.flush()
    return run


def _row_for(rows: list[RunningStrategyOut], run_id: uuid.UUID) -> RunningStrategyOut:
    return next(r for r in rows if r.strategy_run_id == run_id)


def test_is_live_true_for_normal_strategy_in_live_enabled_session(
    db: Session, workspace, broker_account, user: User
):
    session = _make_session(db, workspace, broker_account, user, mode=SafeMode.LIVE_ENABLED)
    run = _make_run(db, workspace, session, user)

    rows = list_running_strategies(db=db, user=user)

    assert _row_for(rows, run.id).is_live is True


def test_is_live_false_for_force_paper_strategy_in_live_enabled_session(
    db: Session, workspace, broker_account, user: User
):
    session = _make_session(db, workspace, broker_account, user, mode=SafeMode.LIVE_ENABLED)
    run = _make_run(
        db, workspace, session, user, runtime_mode=StrategyRuntimeMode.FORCE_PAPER
    )

    rows = list_running_strategies(db=db, user=user)

    assert _row_for(rows, run.id).is_live is False


def test_is_live_false_for_normal_strategy_in_paper_only_session(
    db: Session, workspace, broker_account, user: User
):
    session = _make_session(db, workspace, broker_account, user, mode=SafeMode.PAPER_ONLY)
    run = _make_run(db, workspace, session, user)

    rows = list_running_strategies(db=db, user=user)

    assert _row_for(rows, run.id).is_live is False
