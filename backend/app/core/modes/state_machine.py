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
from datetime import UTC, datetime

from sqlalchemy.orm import Session

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


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
        if to_mode == SafeMode.DEGRADED_MODE:
            trading_session.prior_mode = from_mode

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
