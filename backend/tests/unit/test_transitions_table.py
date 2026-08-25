"""Structural checks on the transition table itself — these encode the
safety invariants from the design doc as executable assertions, so a future
edit that accidentally breaks one (e.g. adding an automatic path into
live_enabled) fails CI immediately rather than being caught in review.
"""

from app.core.modes.transitions import ALLOWED_TRANSITIONS
from app.domain.session.models import SafeMode
from app.domain.session.models import TransitionTriggerType as Trigger


def test_kill_switch_has_no_outbound_edges_except_paper_only():
    edges = ALLOWED_TRANSITIONS.get(SafeMode.KILL_SWITCH, {})
    assert set(edges.keys()) == {SafeMode.PAPER_ONLY}


def test_kill_switch_is_reachable_from_every_other_mode():
    for mode in SafeMode:
        if mode == SafeMode.KILL_SWITCH:
            continue
        edges = ALLOWED_TRANSITIONS.get(mode, {})
        assert SafeMode.KILL_SWITCH in edges, f"{mode} cannot reach kill_switch"


def test_no_automatic_trigger_ever_targets_a_live_mode():
    """Rule 4: automatic (SYSTEM/RISK/RECONCILIATION) transitions only ever
    move down in privilege — none of them may target paper_plus_guarded_live
    or live_enabled."""
    live_targets = {SafeMode.PAPER_PLUS_GUARDED_LIVE, SafeMode.LIVE_ENABLED}
    automatic_triggers = {Trigger.SYSTEM, Trigger.RISK, Trigger.RECONCILIATION}

    for from_mode, edges in ALLOWED_TRANSITIONS.items():
        for to_mode, rule in edges.items():
            if to_mode in live_targets:
                assert rule.allowed_triggers.isdisjoint(automatic_triggers), (
                    f"{from_mode} -> {to_mode} allows an automatic trigger "
                    f"into a live mode: {rule.allowed_triggers}"
                )


def test_live_enabled_loss_cap_stepdown_is_manual_only():
    """Loss cap breach must escalate straight to kill_switch, never a soft
    step-down to guarded_live — so that edge must not permit RISK-triggered
    automatic transitions."""
    rule = ALLOWED_TRANSITIONS[SafeMode.LIVE_ENABLED][SafeMode.PAPER_PLUS_GUARDED_LIVE]
    assert rule.allowed_triggers == frozenset({Trigger.MANUAL})


def test_promotions_require_livetrade_execute_permission():
    promotions = [
        (SafeMode.PAPER_ONLY, SafeMode.PAPER_PLUS_GUARDED_LIVE),
        (SafeMode.PAPER_PLUS_GUARDED_LIVE, SafeMode.LIVE_ENABLED),
    ]
    for from_mode, to_mode in promotions:
        rule = ALLOWED_TRANSITIONS[from_mode][to_mode]
        assert rule.required_permission == "livetrade.execute"
        assert rule.allowed_triggers == frozenset({Trigger.MANUAL})


def test_kill_switch_recovery_requires_permission():
    assert (
        ALLOWED_TRANSITIONS[SafeMode.KILL_SWITCH][SafeMode.PAPER_ONLY].required_permission
        == "risk.override"
    )


def test_reconciliation_lock_has_no_static_recovery_edge():
    """reconciliation_lock -> prior_mode is dynamic (state_machine.recover_
    from_reconciliation_lock), same reasoning degraded_mode's own recovery
    already has no static table entry — only reconciliation_lock ->
    kill_switch remains a static edge."""
    edges = ALLOWED_TRANSITIONS.get(SafeMode.RECONCILIATION_LOCK, {})
    assert set(edges.keys()) == {SafeMode.KILL_SWITCH}
