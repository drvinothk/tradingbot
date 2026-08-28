"""ReconciliationLockRecoveryScheduler — the periodic timer loop that
auto-recovers a session stuck in `reconciliation_lock`, including back to a
live `prior_mode`, once `run_full_reconciliation` comes back clean enough
consecutive times. Requires real Postgres, same reasoning as
`test_health_check_scheduler.py` (mode transitions use the same
advisory-lock-backed `transition_mode`/`_write_transition`).

`composition.reset_for_tests()` runs autouse before/after every test (see
`conftest.py`), so `get_execution_mock()` always starts empty here — a
locked session with zero local positions and a freshly-reset mock broker
reconciles clean by construction, with no fixture setup needed to prove the
"clean" case; the "dirty" case injects a stray fill the same way
`test_api_auth_and_sessions.py`'s own reconciliation-lock tests already do.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.core.modes.state_machine import transition_mode
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.session.models import (
    FundingMode,
    SafeMode,
    SessionModeTransition,
    TradingSession,
    TransitionTriggerType,
)
from app.modules.broker_adapter import composition
from app.modules.broker_adapter.base.contracts import OrderRequest, OrderSide, OrderType
from app.modules.scheduler.reconciliation_lock_recovery import ReconciliationLockRecoveryScheduler


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="recon-lock-recovery-test-account",
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


def _session_factory_for(db: Session):
    @contextmanager
    def _factory():
        yield db

    return _factory


def _scheduler_for(
    db: Session, *, clean_streak_threshold: int = 3
) -> ReconciliationLockRecoveryScheduler:
    return ReconciliationLockRecoveryScheduler(
        session_factory=_session_factory_for(db), clean_streak_threshold=clean_streak_threshold
    )


def _lock_session(db: Session, trading_session: TradingSession, authorized_user: User) -> None:
    """Same reachable-path reasoning every other reconciliation_lock test in
    this codebase uses -- promote to live_enabled first, since the lock is
    never reachable directly from paper_only. The promotion itself needs a
    real permissioned actor (`livetrade.execute`); the lock entry itself
    (RECONCILIATION trigger) does not."""
    transition_mode(
        db,
        trading_session,
        SafeMode.LIVE_ENABLED,
        TransitionTriggerType.MANUAL,
        actor_user=authorized_user,
    )
    transition_mode(
        db,
        trading_session,
        SafeMode.RECONCILIATION_LOCK,
        TransitionTriggerType.RECONCILIATION,
        reason="simulated broker mismatch",
    )
    db.flush()
    assert trading_session.mode == SafeMode.RECONCILIATION_LOCK
    assert trading_session.prior_mode == SafeMode.LIVE_ENABLED
    assert trading_session.reconciliation_lock_clean_streak == 0


def _inject_stray_mismatch() -> None:
    """Same injection pattern `test_api_auth_and_sessions.py`'s own
    reconciliation-lock tests use -- a stray fill against the persistent
    execution mock with no matching local position."""
    composition.get_execution_mock().place_order(
        OrderRequest(
            idempotency_key=f"manual-injection-{uuid.uuid4()}",
            contract_symbol="NIFTY26JUL22000CE-SCHEDULERTEST",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            qty=25,
        )
    )


def test_run_once_increments_clean_streak_when_reconciliation_is_clean(
    db: Session, trading_session, authorized_user
):
    _lock_session(db, trading_session, authorized_user)

    _scheduler_for(db).run_once()

    db.refresh(trading_session)
    assert trading_session.reconciliation_lock_clean_streak == 1
    assert trading_session.mode == SafeMode.RECONCILIATION_LOCK


def test_dirty_check_resets_the_streak(db: Session, trading_session, authorized_user):
    _lock_session(db, trading_session, authorized_user)
    scheduler = _scheduler_for(db)
    scheduler.run_once()
    db.refresh(trading_session)
    assert trading_session.reconciliation_lock_clean_streak == 1

    _inject_stray_mismatch()
    scheduler.run_once()

    db.refresh(trading_session)
    assert trading_session.reconciliation_lock_clean_streak == 0
    assert trading_session.mode == SafeMode.RECONCILIATION_LOCK


def test_auto_recovers_after_reaching_the_clean_streak_threshold(
    db: Session, trading_session, authorized_user
):
    _lock_session(db, trading_session, authorized_user)
    scheduler = _scheduler_for(db, clean_streak_threshold=2)

    scheduler.run_once()
    db.refresh(trading_session)
    assert trading_session.mode == SafeMode.RECONCILIATION_LOCK

    scheduler.run_once()

    db.refresh(trading_session)
    assert trading_session.mode == SafeMode.LIVE_ENABLED
    assert (
        db.query(SessionModeTransition)
        .filter(
            SessionModeTransition.trading_session_id == trading_session.id,
            SessionModeTransition.to_mode == SafeMode.LIVE_ENABLED,
            SessionModeTransition.trigger_type == TransitionTriggerType.RECONCILIATION,
        )
        .count()
        == 1
    )


def test_auto_recovery_to_live_prior_mode_needs_zero_manual_actor(
    db: Session, trading_session, authorized_user
):
    """The core new capability this scheduler exists for: unattended
    recovery all the way back to a live-adjacent prior_mode, with no
    `actor_user` involved in the *recovery* itself (only the earlier,
    ordinary promotion to live needs one, same as any other session)."""
    transition_mode(
        db,
        trading_session,
        SafeMode.LIVE_ENABLED,
        TransitionTriggerType.MANUAL,
        actor_user=authorized_user,
    )
    transition_mode(
        db,
        trading_session,
        SafeMode.RECONCILIATION_LOCK,
        TransitionTriggerType.RECONCILIATION,
        reason="simulated broker mismatch",
    )
    db.flush()
    assert trading_session.prior_mode == SafeMode.LIVE_ENABLED

    scheduler = _scheduler_for(db, clean_streak_threshold=1)
    scheduler.run_once()

    db.refresh(trading_session)
    assert trading_session.mode == SafeMode.LIVE_ENABLED


def test_run_once_does_not_touch_sessions_not_in_reconciliation_lock(
    db: Session, trading_session
):
    assert trading_session.mode == SafeMode.PAPER_ONLY

    _scheduler_for(db).run_once()  # must not raise

    db.refresh(trading_session)
    assert trading_session.mode == SafeMode.PAPER_ONLY
    assert trading_session.reconciliation_lock_clean_streak == 0
