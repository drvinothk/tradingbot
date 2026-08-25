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
    recover_from_reconciliation_lock,
    set_master_trading_mode,
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


def _lock_session_from_guarded_live(db, trading_session, authorized_user) -> None:
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
        SafeMode.RECONCILIATION_LOCK,
        TransitionTriggerType.RECONCILIATION,
        reason="simulated broker mismatch",
    )
    assert trading_session.mode == SafeMode.RECONCILIATION_LOCK
    assert trading_session.prior_mode == SafeMode.PAPER_PLUS_GUARDED_LIVE


def test_reconciliation_lock_remembers_prior_mode_and_recovers_manually(
    db, trading_session, authorized_user
):
    _lock_session_from_guarded_live(db, trading_session, authorized_user)

    recover_from_reconciliation_lock(
        db,
        trading_session,
        TransitionTriggerType.MANUAL,
        actor_user=authorized_user,
        reason="fresh reconciliation confirmed clean",
    )
    assert trading_session.mode == SafeMode.PAPER_PLUS_GUARDED_LIVE


def test_reconciliation_lock_recovery_via_manual_trigger_without_permission_rejected(
    db, trading_session, authorized_user, user
):
    # `user` has no roles/permissions at all (see test_promotion_requires_
    # livetrade_permission above) -- resuming to a live-adjacent prior_mode
    # must reject it exactly like recover_from_degraded already does.
    _lock_session_from_guarded_live(db, trading_session, authorized_user)

    with pytest.raises(ModeTransitionError, match="livetrade.execute"):
        recover_from_reconciliation_lock(
            db, trading_session, TransitionTriggerType.MANUAL, actor_user=user
        )
    assert trading_session.mode == SafeMode.RECONCILIATION_LOCK


def test_reconciliation_lock_recovery_via_reconciliation_trigger_succeeds_to_live(
    db, trading_session, authorized_user
):
    """The one deliberate, scoped exception to rule 4 in this codebase: a
    RECONCILIATION-triggered recovery may restore a live-adjacent prior_mode
    with zero manual actor involved — reserved for
    ReconciliationLockRecoveryScheduler after N consecutive clean checks.
    """
    _lock_session_from_guarded_live(db, trading_session, authorized_user)

    recover_from_reconciliation_lock(
        db,
        trading_session,
        TransitionTriggerType.RECONCILIATION,
        reason="auto-recovered after 3 consecutive clean reconciliation checks",
    )
    assert trading_session.mode == SafeMode.PAPER_PLUS_GUARDED_LIVE


def test_reconciliation_lock_recovery_via_bare_system_trigger_to_live_rejected(
    db, trading_session, authorized_user
):
    """Pins the exception's narrowness: only RECONCILIATION (never a bare
    SYSTEM trigger) may resume a locked session to a live-adjacent
    prior_mode unattended."""
    _lock_session_from_guarded_live(db, trading_session, authorized_user)

    with pytest.raises(ModeTransitionError):
        recover_from_reconciliation_lock(db, trading_session, TransitionTriggerType.SYSTEM)
    assert trading_session.mode == SafeMode.RECONCILIATION_LOCK


def test_reconciliation_lock_recovery_to_paper_only_needs_no_permission(
    db, trading_session, authorized_user
):
    """Unlike a live-adjacent target, recovering to paper_only (no
    prior_mode recorded) works via any trigger, even a bare SYSTEM one with
    no actor -- matches recover_from_degraded's own "resuming to paper_only
    can be automatic" rule. `RECONCILIATION_LOCK` is only reachable from
    `paper_plus_guarded_live`/`live_enabled` (never directly from
    `paper_only`), so this locks the session via the real reachable path
    (same helper every other test above uses) and then manually clears
    `prior_mode` to isolate the no-recorded-prior-mode fallback itself.
    """
    _lock_session_from_guarded_live(db, trading_session, authorized_user)
    trading_session.prior_mode = None
    db.add(trading_session)
    db.flush()

    recover_from_reconciliation_lock(db, trading_session, TransitionTriggerType.SYSTEM)
    assert trading_session.mode == SafeMode.PAPER_ONLY


def test_every_transition_is_captured_in_the_audit_chain(db, trading_session, authorized_user):
    enter_kill_switch(
        db, trading_session, TransitionTriggerType.MANUAL, actor_user=authorized_user, reason="t"
    )
    ok, broken_id = verify_chain(db)
    assert ok, f"broken chain at {broken_id}"


# -- set_master_trading_mode ("master switch") ---------------------------


def test_master_switch_live_from_paper_only_walks_both_hops(db, trading_session, authorized_user):
    set_master_trading_mode(
        db, trading_session, "live", TransitionTriggerType.MANUAL, actor_user=authorized_user
    )
    assert trading_session.mode == SafeMode.LIVE_ENABLED


def test_master_switch_live_from_guarded_live_is_a_single_hop(db, trading_session, authorized_user):
    transition_mode(
        db,
        trading_session,
        SafeMode.PAPER_PLUS_GUARDED_LIVE,
        TransitionTriggerType.MANUAL,
        actor_user=authorized_user,
    )
    set_master_trading_mode(
        db, trading_session, "live", TransitionTriggerType.MANUAL, actor_user=authorized_user
    )
    assert trading_session.mode == SafeMode.LIVE_ENABLED


def test_master_switch_live_is_a_noop_when_already_live(db, trading_session, authorized_user):
    set_master_trading_mode(
        db, trading_session, "live", TransitionTriggerType.MANUAL, actor_user=authorized_user
    )
    # Must not raise "session is already in live_enabled" the way a raw
    # transition_mode(..., LIVE_ENABLED) call would -- repeat clicks from
    # the UI must be safe.
    set_master_trading_mode(
        db, trading_session, "live", TransitionTriggerType.MANUAL, actor_user=authorized_user
    )
    assert trading_session.mode == SafeMode.LIVE_ENABLED


def test_master_switch_paper_from_live_enabled_walks_both_hops(
    db, trading_session, authorized_user
):
    set_master_trading_mode(
        db, trading_session, "live", TransitionTriggerType.MANUAL, actor_user=authorized_user
    )
    set_master_trading_mode(
        db, trading_session, "paper", TransitionTriggerType.MANUAL, actor_user=authorized_user
    )
    assert trading_session.mode == SafeMode.PAPER_ONLY


def test_master_switch_paper_is_a_noop_when_already_paper_only(
    db, trading_session, authorized_user
):
    set_master_trading_mode(
        db, trading_session, "paper", TransitionTriggerType.MANUAL, actor_user=authorized_user
    )
    assert trading_session.mode == SafeMode.PAPER_ONLY


def test_master_switch_refuses_from_every_emergency_mode(db, trading_session, authorized_user):
    for emergency_mode in (
        SafeMode.KILL_SWITCH,
        SafeMode.DEGRADED_MODE,
        SafeMode.RECONCILIATION_LOCK,
    ):
        trading_session.mode = emergency_mode
        db.flush()
        for target in ("live", "paper"):
            with pytest.raises(ModeTransitionError, match="dedicated recovery flow"):
                set_master_trading_mode(
                    db,
                    trading_session,
                    target,
                    TransitionTriggerType.MANUAL,
                    actor_user=authorized_user,
                )
        # And the refusal must not have silently moved the session anyway.
        assert trading_session.mode == emergency_mode


def test_master_switch_live_requires_livetrade_permission(db, trading_session, user):
    # `user` fixture has no roles/permissions assigned.
    with pytest.raises(ModeTransitionError, match="livetrade.execute"):
        set_master_trading_mode(
            db, trading_session, "live", TransitionTriggerType.MANUAL, actor_user=user
        )
    assert trading_session.mode == SafeMode.PAPER_ONLY


def test_master_switch_paper_requires_session_stop_permission(
    db, trading_session, authorized_user, workspace
):
    set_master_trading_mode(
        db, trading_session, "live", TransitionTriggerType.MANUAL, actor_user=authorized_user
    )

    # A user with livetrade.execute but not session.stop cannot step down --
    # build one directly rather than stripping authorized_user's roles
    # mid-test. Reuses the "livetrade.execute" Permission row authorized_user
    # already created (Permission.code is unique, so a second row with the
    # same code would violate that constraint).
    from app.core.security.passwords import hash_password
    from app.domain.identity.models import Permission, Role, RolePermission, User, UserRole

    limited_user = User(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("correct horse battery staple"),
        display_name="Limited Test User",
        is_active=True,
    )
    db.add(limited_user)
    db.flush()

    role = Role(id=uuid.uuid4(), name=f"live-only-{uuid.uuid4().hex[:8]}")
    db.add(role)
    db.flush()

    livetrade_permission = db.query(Permission).filter(Permission.code == "livetrade.execute").one()
    db.add(RolePermission(role_id=role.id, permission_id=livetrade_permission.id))
    db.add(UserRole(user_id=limited_user.id, role_id=role.id, workspace_id=workspace.id))
    db.flush()

    with pytest.raises(ModeTransitionError, match="session.stop"):
        set_master_trading_mode(
            db, trading_session, "paper", TransitionTriggerType.MANUAL, actor_user=limited_user
        )
    assert trading_session.mode == SafeMode.LIVE_ENABLED


def test_master_switch_transitions_are_captured_in_the_audit_chain(
    db, trading_session, authorized_user
):
    set_master_trading_mode(
        db, trading_session, "live", TransitionTriggerType.MANUAL, actor_user=authorized_user
    )
    set_master_trading_mode(
        db, trading_session, "paper", TransitionTriggerType.MANUAL, actor_user=authorized_user
    )
    ok, broken_id = verify_chain(db)
    assert ok, f"broken chain at {broken_id}"
