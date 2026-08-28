"""The one choke-point through which every safe-mode transition must pass.

Nothing outside this module should ever assign `TradingSession.mode`
directly — every transition here writes `session_mode_transitions` and
`audit_events` in the same transaction, under the same advisory lock used
for the Execution singleton, so a mode change can never interleave with an
in-flight order dispatch, and every change is both traceable and
un-forgeable-after-the-fact via the audit hash chain.
"""

from __future__ import annotations

import uuid
from typing import Literal

from sqlalchemy.orm import Session

from app.core.db.base import utcnow as _utcnow
from app.core.locking import LOCK_EXECUTION_SINGLETON, advisory_lock
from app.core.modes.transitions import ALLOWED_TRANSITIONS
from app.core.security.rbac import get_user_permissions
from app.domain.audit.models import ActorType, EventCategory
from app.domain.identity.models import User
from app.domain.session.models import (
    SafeMode,
    SessionModeTransition,
    TradingSession,
    TransitionTriggerType,
)
from app.modules.audit_service.service import record_event


class ModeTransitionError(Exception):
    """Raised for illegal transitions or missing permission — callers at the
    API layer should turn this into a 409/403 as appropriate, not a 500."""




def _write_transition(
    db: Session,
    trading_session: TradingSession,
    *,
    from_mode: SafeMode,
    to_mode: SafeMode,
    trigger_type: TransitionTriggerType,
    actor_user: User | None,
    reason: str,
) -> TradingSession:
    with advisory_lock(db, LOCK_EXECUTION_SINGLETON):
        if to_mode in (SafeMode.DEGRADED_MODE, SafeMode.RECONCILIATION_LOCK):
            trading_session.prior_mode = from_mode

        if to_mode == SafeMode.RECONCILIATION_LOCK:
            # A fresh lock always starts a fresh streak -- see
            # ReconciliationLockRecoveryScheduler / recover_from_
            # reconciliation_lock's own docstrings for what drives this.
            trading_session.reconciliation_lock_clean_streak = 0

        trading_session.mode = to_mode
        db.add(trading_session)
        db.flush()

        db.add(
            SessionModeTransition(
                id=uuid.uuid4(),
                trading_session_id=trading_session.id,
                from_mode=from_mode,
                to_mode=to_mode,
                trigger_type=trigger_type,
                triggered_by_user_id=actor_user.id if actor_user else None,
                reason=reason,
                created_at=_utcnow(),
            )
        )
        db.flush()

        record_event(
            db,
            workspace_id=trading_session.workspace_id,
            actor_type=ActorType.USER if actor_user else ActorType.SYSTEM,
            actor_id=actor_user.id if actor_user else None,
            event_category=EventCategory.MODE_TRANSITION,
            event_type=f"mode_transition.{from_mode.value}_to_{to_mode.value}",
            entity_type="trading_session",
            entity_id=trading_session.id,
            trading_session_id=trading_session.id,
            broker_account_id=trading_session.broker_account_id,
            payload={
                "from_mode": from_mode.value,
                "to_mode": to_mode.value,
                "trigger_type": trigger_type.value,
                "reason": reason,
            },
        )

    return trading_session


def transition_mode(
    db: Session,
    trading_session: TradingSession,
    to_mode: SafeMode,
    trigger_type: TransitionTriggerType,
    *,
    actor_user: User | None = None,
    reason: str = "",
) -> TradingSession:
    """The generic guard for every static edge in ALLOWED_TRANSITIONS.
    degraded_mode's recovery edge is dynamic (target = prior_mode) and goes
    through recover_from_degraded instead.
    """
    from_mode = SafeMode(trading_session.mode)

    if from_mode == to_mode:
        raise ModeTransitionError(f"session is already in {to_mode.value}")

    rule = ALLOWED_TRANSITIONS.get(from_mode, {}).get(to_mode)
    if rule is None:
        raise ModeTransitionError(f"{from_mode.value} -> {to_mode.value} is not a legal transition")

    if trigger_type not in rule.allowed_triggers:
        raise ModeTransitionError(
            f"{from_mode.value} -> {to_mode.value} cannot be triggered by {trigger_type.value}"
        )

    if trigger_type == TransitionTriggerType.MANUAL:
        if actor_user is None:
            raise ModeTransitionError("manual transitions require an authenticated actor")
        if rule.required_permission and rule.required_permission not in get_user_permissions(
            db, actor_user
        ):
            raise ModeTransitionError(
                f"actor is missing required permission: {rule.required_permission}"
            )

    return _write_transition(
        db,
        trading_session,
        from_mode=from_mode,
        to_mode=to_mode,
        trigger_type=trigger_type,
        actor_user=actor_user,
        reason=reason,
    )


def enter_kill_switch(
    db: Session,
    trading_session: TradingSession,
    trigger_type: TransitionTriggerType,
    *,
    actor_user: User | None = None,
    reason: str = "",
) -> TradingSession:
    """Convenience wrapper — kill_switch is reachable from every mode, so
    callers (Risk Service on a loss-cap breach, an operator's panic button)
    don't need to know the current mode to invoke it."""
    return transition_mode(
        db,
        trading_session,
        SafeMode.KILL_SWITCH,
        trigger_type,
        actor_user=actor_user,
        reason=reason,
    )


def recover_from_degraded(
    db: Session,
    trading_session: TradingSession,
    trigger_type: TransitionTriggerType,
    *,
    actor_user: User | None = None,
    reason: str = "",
) -> TradingSession:
    """degraded_mode -> prior_mode. Resuming to paper_only can be automatic
    once health checks pass; resuming to anything above paper_only always
    requires a manual, permissioned confirm — even if a health check is what
    detected recovery — per rule 4 (automatic transitions only move down).
    """
    from_mode = SafeMode(trading_session.mode)
    if from_mode != SafeMode.DEGRADED_MODE:
        raise ModeTransitionError("session is not in degraded_mode")

    target = (
        SafeMode(trading_session.prior_mode) if trading_session.prior_mode else SafeMode.PAPER_ONLY
    )

    if target != SafeMode.PAPER_ONLY:
        if trigger_type != TransitionTriggerType.MANUAL or actor_user is None:
            raise ModeTransitionError(
                f"resuming to {target.value} requires a manual, permissioned confirmation"
            )
        if "livetrade.execute" not in get_user_permissions(db, actor_user):
            raise ModeTransitionError("actor is missing required permission: livetrade.execute")

    return _write_transition(
        db,
        trading_session,
        from_mode=from_mode,
        to_mode=target,
        trigger_type=trigger_type,
        actor_user=actor_user,
        reason=reason,
    )


def recover_from_reconciliation_lock(
    db: Session,
    trading_session: TradingSession,
    trigger_type: TransitionTriggerType,
    *,
    actor_user: User | None = None,
    reason: str = "",
) -> TradingSession:
    """reconciliation_lock -> prior_mode. Same dynamic-target shape as
    `recover_from_degraded`, with one deliberate, scoped exception to rule 4
    (automatic transitions only ever move down in privilege) -- confirmed
    explicitly with the user, 2026-08-25: unlike degraded_mode/kill_switch,
    which always require a manual, permissioned confirm to resume above
    paper_only, reconciliation_lock may also auto-recover all the way back
    to a live prior_mode via `TransitionTriggerType.RECONCILIATION` --
    reserved exclusively for `ReconciliationLockRecoveryScheduler`, which
    only fires this after N consecutive clean `run_full_reconciliation`
    checks (never a bare timer, never a single check). The reasoning: this
    lock exists to catch a *technical* local-vs-broker divergence, not a
    judgment call the way a loss-cap breach or a degraded health check is --
    once the broker's own book is repeatedly confirmed to match local state
    again, the thing that justified the lock is gone. A bare `SYSTEM`
    trigger still may not resume above paper_only -- only `MANUAL` (with
    `livetrade.execute`) or `RECONCILIATION` can, keeping the exception as
    narrow as what actually verifies the lock's own root cause is cleared.
    """
    from_mode = SafeMode(trading_session.mode)
    if from_mode != SafeMode.RECONCILIATION_LOCK:
        raise ModeTransitionError("session is not in reconciliation_lock")

    target = (
        SafeMode(trading_session.prior_mode) if trading_session.prior_mode else SafeMode.PAPER_ONLY
    )

    if target != SafeMode.PAPER_ONLY:
        if trigger_type == TransitionTriggerType.MANUAL:
            if actor_user is None:
                raise ModeTransitionError("manual transitions require an authenticated actor")
            if "livetrade.execute" not in get_user_permissions(db, actor_user):
                raise ModeTransitionError(
                    "actor is missing required permission: livetrade.execute"
                )
        elif trigger_type != TransitionTriggerType.RECONCILIATION:
            raise ModeTransitionError(
                f"resuming to {target.value} requires a manual, permissioned "
                "confirmation or a reconciliation-verified auto-recovery"
            )

    return _write_transition(
        db,
        trading_session,
        from_mode=from_mode,
        to_mode=target,
        trigger_type=trigger_type,
        actor_user=actor_user,
        reason=reason,
    )


_MASTER_MODE_LADDER: dict[Literal["paper", "live"], list[SafeMode]] = {
    "live": [SafeMode.PAPER_ONLY, SafeMode.LIVE_ENABLED],
    "paper": [SafeMode.LIVE_ENABLED, SafeMode.PAPER_ONLY],
}


def set_master_trading_mode(
    db: Session,
    trading_session: TradingSession,
    target: Literal["paper", "live"],
    trigger_type: TransitionTriggerType,
    *,
    actor_user: User | None = None,
    reason: str = "",
) -> TradingSession:
    """The friendly, two-value "master switch" (Paper/Live) a human actually
    wants. `paper_only <-> live_enabled` is a direct edge in `transitions.py`
    (since 2026-08-28, when the `paper_plus_guarded_live` intermediate tier
    was retired), so this is a single `transition_mode` call — the ladder
    structure is kept only for its "already at target = no-op" and "session
    is in an unexpected mode = reject" guards.

    Deliberately refuses -- rather than walking through -- when the session
    is currently in one of the three emergency states (`kill_switch`,
    `degraded_mode`, `reconciliation_lock`). Those have their own
    dedicated, higher-bar recovery endpoints
    (`recover_from_kill_switch`/`recover_from_degraded`) and must never be
    bypassed by a mode convenience wrapper. A session already at the target
    mode is a no-op, not an error -- unlike `transition_mode` itself, which
    treats "already there" as illegal, this wrapper is meant to be safe to
    click repeatedly from a UI.
    """
    from_mode = SafeMode(trading_session.mode)
    emergency_modes = (SafeMode.KILL_SWITCH, SafeMode.DEGRADED_MODE, SafeMode.RECONCILIATION_LOCK)
    if from_mode in emergency_modes:
        raise ModeTransitionError(
            f"session is in {from_mode.value} -- use its dedicated recovery flow, "
            "not the master switch"
        )

    ladder = _MASTER_MODE_LADDER[target]
    if from_mode not in ladder:
        raise ModeTransitionError(
            f"session mode {from_mode.value} is not on the master-mode ladder"
        )

    hops = ladder[ladder.index(from_mode) + 1 :]

    result = trading_session
    for hop in hops:
        result = transition_mode(
            db, result, hop, trigger_type, actor_user=actor_user, reason=reason
        )
    return result
