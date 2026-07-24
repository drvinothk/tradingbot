"""Exercises the state machine against a real Postgres — advisory locks and
the audit hash chain aren't meaningfully testable against SQLite, so these
require the DB_* env vars to point at a live Postgres (see conftest.py and
the CI workflow's Postgres service container).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.core.modes import (
    ModeTransitionError,
    enter_kill_switch,
    recover_from_degraded,
    transition_mode,
)
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.session.models import SafeMode, TradingSession, TransitionTriggerType
from app.modules.audit_service.service import verify_chain


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="test-account",
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
        budget_amount=10000,
        daily_target_profit=2000,
        daily_loss_cap=1000,
    )
    db.add(ts)
    db.flush()
    return ts


def test_default_mode_is_paper_only(trading_session: TradingSession):
    assert trading_session.mode == SafeMode.PAPER_ONLY


def test_kill_switch_reachable_from_paper_only(db, trading_session, authorized_user):
    enter_kill_switch(
        db, trading_session, TransitionTriggerType.MANUAL, actor_user=authorized_user, reason="test"
    )
    assert trading_session.mode == SafeMode.KILL_SWITCH


def test_kill_switch_cannot_go_directly_to_live(db, trading_session, authorized_user):
    enter_kill_switch(
        db, trading_session, TransitionTriggerType.MANUAL, actor_user=authorized_user, reason="t"
    )
    with pytest.raises(ModeTransitionError):
        transition_mode(
            db,
            trading_session,
            SafeMode.LIVE_ENABLED,
            TransitionTriggerType.MANUAL,
            actor_user=authorized_user,
        )


def test_manual_transition_without_actor_rejected(db, trading_session):
    with pytest.raises(ModeTransitionError):
        transition_mode(
            db,
            trading_session,
            SafeMode.PAPER_PLUS_GUARDED_LIVE,
            TransitionTriggerType.MANUAL,
            actor_user=None,
        )


def test_promotion_requires_livetrade_permission(db, trading_session, user):
    # `user` fixture has no roles assigned, so no permissions at all.
    with pytest.raises(ModeTransitionError, match="livetrade.execute"):
        transition_mode(
            db,
            trading_session,
            SafeMode.PAPER_PLUS_GUARDED_LIVE,
            TransitionTriggerType.MANUAL,
            actor_user=user,
        )


def test_degraded_mode_remembers_prior_mode_and_recovers(db, trading_session, authorized_user):
    # degraded_mode is only reachable from paper_plus_guarded_live/live_enabled
    # (never directly from paper_only) — promote for real first, matching the
    # actual reachable path rather than asserting an edge that doesn't exist.
    transition_mode(
        db,
        trading_session,
        SafeMode.PAPER_PLUS_GUARDED_LIVE,
        TransitionTriggerType.MANUAL,
        actor_user=authorized_user,
    )

    transition_mode(
        db,
        trading_session,
        SafeMode.DEGRADED_MODE,
        TransitionTriggerType.SYSTEM,
        reason="simulated WS drop",
    )
    assert trading_session.mode == SafeMode.DEGRADED_MODE
    assert trading_session.prior_mode == SafeMode.PAPER_PLUS_GUARDED_LIVE

    # Resuming to anything above paper_only always requires a manual,
    # permissioned confirm, even though a health check is what detected recovery.
    recover_from_degraded(
        db,
        trading_session,
        TransitionTriggerType.MANUAL,
        actor_user=authorized_user,
        reason="health ok, admin confirmed",
    )
    assert trading_session.mode == SafeMode.PAPER_PLUS_GUARDED_LIVE


def test_every_transition_is_captured_in_the_audit_chain(db, trading_session, authorized_user):
    enter_kill_switch(
        db, trading_session, TransitionTriggerType.MANUAL, actor_user=authorized_user, reason="t"
    )
    ok, broken_id = verify_chain(db)
    assert ok, f"audit chain broken at {broken_id}"
