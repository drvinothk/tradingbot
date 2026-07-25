"""The legal-transition table for the safe operating mode state machine.
This is data, not behavior — app.core.modes.state_machine is what actually
enforces it. Keeping the table here, standalone, means the full set of legal
moves is readable in one place without wading through lock/audit plumbing.

Rule 4 (automatic transitions only ever move down in privilege) is encoded
directly in which trigger types each edge allows — there is no separate
"privilege level" concept to keep in sync; the table itself is the source of
truth for what's reachable and by whom.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.session.models import SafeMode
from app.domain.session.models import TransitionTriggerType as Trigger


@dataclass(frozen=True)
class TransitionRule:
    allowed_triggers: frozenset[Trigger]
    required_permission: str | None = None


# fmt: off
ALLOWED_TRANSITIONS: dict[SafeMode, dict[SafeMode, TransitionRule]] = {
    SafeMode.PAPER_ONLY: {
        SafeMode.PAPER_PLUS_GUARDED_LIVE: TransitionRule(
            allowed_triggers=frozenset({Trigger.MANUAL}),
            required_permission="livetrade.execute",
        ),
        SafeMode.KILL_SWITCH: TransitionRule(
            # RISK alongside SYSTEM: a daily-loss-cap breach escalates
            # straight to kill_switch regardless of safe-mode (see Phase 2's
            # Risk Service) — paper_only is not exempt just because no real
            # money is at stake; the discipline of the safety flow applies
            # the same way it already does from the two live-adjacent modes
            # below.
            allowed_triggers=frozenset({Trigger.MANUAL, Trigger.SYSTEM, Trigger.RISK}),
            required_permission="session.stop",
        ),
    },
    SafeMode.PAPER_PLUS_GUARDED_LIVE: {
        SafeMode.LIVE_ENABLED: TransitionRule(
            allowed_triggers=frozenset({Trigger.MANUAL}),
            required_permission="livetrade.execute",
        ),
        SafeMode.PAPER_ONLY: TransitionRule(
            allowed_triggers=frozenset({Trigger.MANUAL, Trigger.RISK}),
            required_permission="session.stop",
        ),
        SafeMode.DEGRADED_MODE: TransitionRule(
            allowed_triggers=frozenset({Trigger.SYSTEM}),
        ),
        SafeMode.RECONCILIATION_LOCK: TransitionRule(
            allowed_triggers=frozenset({Trigger.SYSTEM, Trigger.RECONCILIATION}),
        ),
        SafeMode.KILL_SWITCH: TransitionRule(
            allowed_triggers=frozenset({Trigger.MANUAL, Trigger.RISK}),
            required_permission="session.stop",
        ),
    },
    SafeMode.LIVE_ENABLED: {
        SafeMode.PAPER_PLUS_GUARDED_LIVE: TransitionRule(
            # Manual only — a daily loss cap breach goes straight to
            # kill_switch, never this soft step-down, even automatically.
            allowed_triggers=frozenset({Trigger.MANUAL}),
            required_permission="session.stop",
        ),
        SafeMode.DEGRADED_MODE: TransitionRule(
            allowed_triggers=frozenset({Trigger.SYSTEM}),
        ),
        SafeMode.RECONCILIATION_LOCK: TransitionRule(
            allowed_triggers=frozenset({Trigger.SYSTEM, Trigger.RECONCILIATION}),
        ),
        SafeMode.KILL_SWITCH: TransitionRule(
            allowed_triggers=frozenset({Trigger.MANUAL, Trigger.RISK}),
            required_permission="session.stop",
        ),
    },
    SafeMode.DEGRADED_MODE: {
        # degraded_mode -> prior_mode is handled by state_machine.recover_from_degraded,
        # not this table, because the target is dynamic. Only degraded -> kill_switch
        # and degraded -> reconciliation_lock (fault escalation) are static edges.
        SafeMode.RECONCILIATION_LOCK: TransitionRule(
            allowed_triggers=frozenset({Trigger.SYSTEM}),
        ),
        SafeMode.KILL_SWITCH: TransitionRule(
            allowed_triggers=frozenset({Trigger.MANUAL, Trigger.SYSTEM}),
            required_permission="session.stop",
        ),
    },
    SafeMode.RECONCILIATION_LOCK: {
        SafeMode.PAPER_ONLY: TransitionRule(
            allowed_triggers=frozenset({Trigger.MANUAL}),
            required_permission="risk.override",
        ),
        SafeMode.KILL_SWITCH: TransitionRule(
            allowed_triggers=frozenset({Trigger.MANUAL, Trigger.SYSTEM}),
            required_permission="risk.override",
        ),
    },
    SafeMode.KILL_SWITCH: {
        SafeMode.PAPER_ONLY: TransitionRule(
            allowed_triggers=frozenset({Trigger.MANUAL}),
            required_permission="risk.override",
        ),
        # Deliberately no other outbound edge — kill_switch can only ever
        # resume to paper_only, never directly to a live mode.
    },
}
# fmt: on
