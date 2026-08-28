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
        SafeMode.LIVE_ENABLED: TransitionRule(
            # 2026-08-28: was `paper_only -> paper_plus_guarded_live`. The
            # guarded intermediate tier only ever paired with per-strategy
            # `StrategyConfig.status == LIVE` (a field with no setter), so it
            # was retired -- the master switch now promotes straight to
            # live_enabled and a single strategy is held back with
            # `StrategyRuntimeMode.FORCE_PAPER`.
            allowed_triggers=frozenset({Trigger.MANUAL}),
            required_permission="livetrade.execute",
        ),
        SafeMode.KILL_SWITCH: TransitionRule(
            # RISK alongside SYSTEM: a daily-loss-cap breach escalates
            # straight to kill_switch regardless of safe-mode (see Phase 2's
            # Risk Service) — paper_only is not exempt just because no real
            # money is at stake; the discipline of the safety flow applies
            # the same way it already does from live_enabled below.
            allowed_triggers=frozenset({Trigger.MANUAL, Trigger.SYSTEM, Trigger.RISK}),
            required_permission="session.stop",
        ),
    },
    SafeMode.LIVE_ENABLED: {
        SafeMode.PAPER_ONLY: TransitionRule(
            # Manual only — a daily loss cap breach goes straight to
            # kill_switch, never an automatic step-down to paper.
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
        # reconciliation_lock -> prior_mode is handled by state_machine
        # .recover_from_reconciliation_lock, not this table, because the
        # target is dynamic (same reasoning as degraded_mode's own recovery
        # above) -- including a deliberate, scoped exception to rule 4 that
        # lets a reconciliation-verified auto-recovery restore all the way
        # to a live prior_mode. Only reconciliation_lock -> kill_switch is a
        # static edge.
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
