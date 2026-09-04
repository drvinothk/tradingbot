"""Ops-Hardening Phase 2: app.modules.alerting.manager.send_alert -- the
dual-write (SystemAlert row + best-effort Telegram) every alert-raising
call site goes through, plus the 2026-08-25 Telegram push-gating rules
(category allowlist, CRITICAL-only, paper-mode suppression, 09:00-15:30
IST window, 15-min dedup) added after a real noise complaint.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from datetime import time as dt_time

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session

import app.modules.alerting.manager as alerting_manager
from app.config.settings import get_settings
from app.core.clock import IST
from app.domain.execution.models import OrderMode
from app.domain.identity.models import Workspace
from app.domain.ops.models import AlertSeverity, SystemAlert
from app.modules.alerting.manager import send_alert

# Any category not in TELEGRAM_ALLOWED_CATEGORIES, used to prove the
# allowlist itself blocks a push regardless of every other condition.
_DISALLOWED_CATEGORY = "auto_spawn_no_underlying"
_ALLOWED_CATEGORY = "strategy_run_stalled"
_WITHIN_WINDOW = datetime(2026, 1, 5, 11, 0, tzinfo=IST)  # a Monday, 11:00 IST
_OUTSIDE_WINDOW = datetime(2026, 1, 5, 17, 0, tzinfo=IST)  # 17:00 IST


@pytest.fixture(autouse=True)
def _reset_module_state():
    """`_warned_missing_config`, `_last_pushed_by_key`, and
    `_first_seen_by_key` are module-level globals -- reset around each test
    so one test's trigger can't leak into another's assertion, matching this
    file's own pre-existing isolation discipline for the config-warning flag.
    """
    alerting_manager._warned_missing_config = False
    alerting_manager._last_pushed_by_key.clear()
    alerting_manager._first_seen_by_key.clear()
    yield
    alerting_manager._warned_missing_config = False
    alerting_manager._last_pushed_by_key.clear()
    alerting_manager._first_seen_by_key.clear()


@pytest.fixture(autouse=True)
def _telegram_unconfigured(monkeypatch):
    """Every real deployment of this test suite must never fire a real
    Telegram HTTP call regardless of what's in a local credentials file --
    default every test to "unconfigured" explicitly rather than relying on
    telegram.env simply not existing on whichever machine runs this suite.
    """
    settings = get_settings()
    monkeypatch.setattr(settings.telegram, "bot_token", SecretStr(""))
    monkeypatch.setattr(settings.telegram, "chat_id", "")


@pytest.fixture
def _within_alert_window(monkeypatch):
    monkeypatch.setattr(alerting_manager, "now_ist", lambda: _WITHIN_WINDOW)


@pytest.fixture
def _configured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.telegram, "bot_token", SecretStr("fake-token"))
    monkeypatch.setattr(settings.telegram, "chat_id", "12345")
    calls: list[tuple] = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: calls.append((a, kw)))
    return calls


def test_send_alert_writes_a_system_alert_row(db: Session, workspace):
    alert = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.WARNING,
        category="test_category",
        message="something happened",
    )
    db.flush()

    row = db.get(SystemAlert, alert.id)
    assert row is not None
    assert row.workspace_id == workspace.id
    assert row.severity == AlertSeverity.WARNING
    assert row.category == "test_category"
    assert row.message == "something happened"
    assert row.trading_session_id is None


def test_send_alert_persists_mode_on_a_new_row(db: Session, workspace):
    # 2026-09-04: mode used to be used only in-memory for the Telegram
    # push decision and then discarded -- Control Room's Attention card
    # now needs it persisted too.
    alert = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category="order_rejected",
        message="rejected",
        mode=OrderMode.LIVE,
        dedup_key="order_rejected:mode-persist-test",
    )
    db.flush()
    row = db.get(SystemAlert, alert.id)
    assert row is not None
    assert row.mode == OrderMode.LIVE


def test_send_alert_refreshes_mode_on_a_collapsed_row(db: Session, workspace):
    first = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category="exit_order_unfilled",
        message="first",
        mode=OrderMode.PAPER,
        dedup_key="exit_order_unfilled:collapse-mode-test",
    )
    db.flush()
    second = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category="exit_order_unfilled",
        message="second",
        mode=OrderMode.LIVE,
        dedup_key="exit_order_unfilled:collapse-mode-test",
    )
    db.flush()

    assert second.id == first.id  # collapsed into the same row
    collapsed_row = db.get(SystemAlert, first.id)
    assert collapsed_row is not None
    assert collapsed_row.mode == OrderMode.LIVE


def test_send_alert_skips_telegram_when_unconfigured(
    db: Session, workspace, monkeypatch, _within_alert_window
):
    calls: list[tuple] = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: calls.append((a, kw)))

    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
    )

    assert calls == []


def test_send_alert_calls_telegram_when_configured(
    db: Session, workspace, _configured, _within_alert_window
):
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="feed is stalled",
    )

    assert len(_configured) == 1
    (url,), kwargs = _configured[0]
    assert url == "https://api.telegram.org/botfake-token/sendMessage"
    assert kwargs["json"]["chat_id"] == "12345"
    assert kwargs["json"]["text"].startswith(f"[CRITICAL] {_ALLOWED_CATEGORY}: feed is stalled")
    assert alerting_manager.TELEGRAM_SUGGESTED_ACTIONS[_ALLOWED_CATEGORY] in kwargs["json"]["text"]
    assert kwargs["timeout"] == alerting_manager._TELEGRAM_TIMEOUT_SECONDS


def test_send_alert_appends_no_tip_line_for_a_category_with_none_registered(
    db: Session, workspace, _configured, _within_alert_window, monkeypatch
):
    monkeypatch.delitem(
        alerting_manager.TELEGRAM_SUGGESTED_ACTIONS, _ALLOWED_CATEGORY, raising=False
    )

    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="feed is stalled",
    )

    assert len(_configured) == 1
    (_,), kwargs = _configured[0]
    assert kwargs["json"]["text"] == f"[CRITICAL] {_ALLOWED_CATEGORY}: feed is stalled"


def test_send_alert_survives_telegram_failure(
    db: Session, workspace, _configured, monkeypatch, _within_alert_window
):
    def _raise(*a, **kw):
        raise httpx.ConnectTimeout("boom")

    monkeypatch.setattr(httpx, "post", _raise)

    # Must not raise -- the SystemAlert row is already the durable record;
    # a Telegram outage is never allowed to propagate into the caller's own
    # background cycle.
    alert = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
    )

    assert db.get(SystemAlert, alert.id) is not None


def test_send_alert_uses_given_trading_session_id(db: Session, workspace):
    session_id = uuid.uuid4()

    alert = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.INFO,
        category="x",
        message="y",
        trading_session_id=session_id,
    )

    row = db.get(SystemAlert, alert.id)
    assert row is not None
    assert row.trading_session_id == session_id


def test_telegram_blocked_for_category_not_on_the_allowlist(
    db: Session, workspace, _configured, _within_alert_window
):
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_DISALLOWED_CATEGORY,
        message="y",
    )

    assert _configured == []


def test_telegram_blocked_for_non_critical_severity_even_if_category_allowed(
    db: Session, workspace, _configured, _within_alert_window
):
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.WARNING,
        category=_ALLOWED_CATEGORY,
        message="y",
    )

    assert _configured == []


def test_telegram_blocked_for_paper_mode(db: Session, workspace, _configured, _within_alert_window):
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
        mode=OrderMode.PAPER,
    )

    assert _configured == []


def test_telegram_paper_mode_still_pushed_when_override_suppression_set(
    db: Session, workspace, _configured, _within_alert_window
):
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
        mode=OrderMode.PAPER,
        override_paper_mode_suppression=True,
    )

    assert len(_configured) == 1


def test_override_suppression_does_not_bypass_the_other_gates(
    db: Session, workspace, _configured, _within_alert_window
):
    # Non-CRITICAL severity is still blocked even with the override.
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.WARNING,
        category=_ALLOWED_CATEGORY,
        message="y",
        mode=OrderMode.PAPER,
        override_paper_mode_suppression=True,
    )

    assert _configured == []


def test_telegram_allowed_for_live_mode(db: Session, workspace, _configured, _within_alert_window):
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
        mode=OrderMode.LIVE,
    )

    assert len(_configured) == 1


def test_telegram_allowed_when_mode_not_given_at_all(
    db: Session, workspace, _configured, _within_alert_window
):
    """`mode=None` (the default) is for system-level alerts with no specific
    paper/live position behind them -- never paper-suppressed."""
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
    )

    assert len(_configured) == 1


def test_telegram_blocked_outside_the_alert_window(
    db: Session, workspace, _configured, monkeypatch
):
    monkeypatch.setattr(alerting_manager, "now_ist", lambda: _OUTSIDE_WINDOW)

    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
    )

    assert _configured == []


@pytest.mark.parametrize("edge", [dt_time(9, 0), dt_time(15, 30)])
def test_telegram_allowed_at_the_window_boundaries_inclusive(
    db: Session, workspace, _configured, monkeypatch, edge
):
    edge_dt = datetime(2026, 1, 5, edge.hour, edge.minute, tzinfo=IST)
    monkeypatch.setattr(alerting_manager, "now_ist", lambda: edge_dt)

    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
    )

    assert len(_configured) == 1


def test_telegram_dedup_suppresses_a_repeat_of_the_same_issue_within_the_cooldown(
    db: Session, workspace, _configured, _within_alert_window
):
    for _ in range(2):
        send_alert(
            db,
            workspace_id=workspace.id,
            severity=AlertSeverity.CRITICAL,
            category=_ALLOWED_CATEGORY,
            message="y",
            dedup_key="same-issue",
        )

    assert len(_configured) == 1


def test_telegram_dedup_does_not_suppress_a_different_dedup_key(
    db: Session, workspace, _configured, _within_alert_window
):
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
        dedup_key="issue-a",
    )
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
        dedup_key="issue-b",
    )

    assert len(_configured) == 2


def test_telegram_dedup_allows_a_resend_after_the_cooldown_elapses(
    db: Session, workspace, _configured, monkeypatch
):
    monkeypatch.setattr(alerting_manager, "now_ist", lambda: _WITHIN_WINDOW)
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
        dedup_key="same-issue",
    )

    later = _WITHIN_WINDOW + timedelta(seconds=alerting_manager._DEDUP_COOLDOWN_SECONDS + 1)
    monkeypatch.setattr(alerting_manager, "now_ist", lambda: later)
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
        dedup_key="same-issue",
    )

    assert len(_configured) == 2


def test_telegram_blocked_while_weekend_rest_mode_is_dormant(
    db: Session, workspace, _configured, _within_alert_window, monkeypatch
):
    """A dormant weekend (no signed-in user) suppresses the push -- but the
    SystemAlert DB row must still be written, same invariant as every other
    gate in _should_push_to_telegram."""
    monkeypatch.setattr(alerting_manager.weekend_rest, "is_dormant", lambda *a, **k: True)

    alert = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
    )

    assert _configured == []
    assert db.get(SystemAlert, alert.id) is not None


def test_dormant_suppression_does_not_consume_a_dedup_slot(
    db: Session, workspace, _configured, _within_alert_window, monkeypatch
):
    """The dormant check sits before the dedup step, so a candidate blocked
    while dormant must not start that issue's cooldown -- once the system is
    awake again the first real alert still pushes."""
    dormant = {"v": True}
    monkeypatch.setattr(
        alerting_manager.weekend_rest, "is_dormant", lambda *a, **k: dormant["v"]
    )

    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
        dedup_key="same-issue",
    )
    assert _configured == []

    dormant["v"] = False
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
        dedup_key="same-issue",
    )
    assert len(_configured) == 1


def test_send_alert_default_dedup_key_falls_back_to_session_or_workspace(
    db: Session, workspace, _configured, _within_alert_window
):
    """No explicit dedup_key given -- two different trading_session_ids for
    the same category must be treated as two different issues."""
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
        trading_session_id=uuid.uuid4(),
    )
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
        trading_session_id=uuid.uuid4(),
    )

    assert len(_configured) == 2


# 2026-08-30: exit_legs_collapsed was added to the allowlist alongside a fix
# to exit_legs.py._alert_collapsed, which previously hardcoded mode=PAPER
# for every collapse reason -- including the LIVE-position one, which made
# it permanently un-pushable regardless of severity/allowlist since a
# mode=PAPER alert is always suppressed. These prove the real category name
# end-to-end through send_alert, not just the generic _ALLOWED_CATEGORY
# stand-in the rest of this file uses.
def test_telegram_allowed_for_exit_legs_collapsed_on_a_live_position(
    db: Session, workspace, _configured, _within_alert_window
):
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category="exit_legs_collapsed",
        message="y",
        mode=OrderMode.LIVE,
    )

    assert len(_configured) == 1


def test_telegram_blocked_for_exit_legs_collapsed_on_a_paper_position(
    db: Session, workspace, _configured, _within_alert_window
):
    """The two paper-only collapse reasons stay WARNING + mode=PAPER --
    never pushed, matching every other paper-suppressed alert."""
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.WARNING,
        category="exit_legs_collapsed",
        message="y",
        mode=OrderMode.PAPER,
    )

    assert _configured == []


# 2026-09-03: self-healing grace window -- protective_stop_cancel_unresolved,
# exit_order_unfilled, and reconciliation_mismatch are raised on an ambiguous
# intermediate state that PositionManager's own 3s retry cycle resolves
# within ~1s the overwhelming majority of the time. See manager.py's own
# docstring ("2026-09-03: self-healing grace window") for the full reasoning.
_SELF_HEALING_START = datetime(2026, 1, 5, 10, 0, tzinfo=alerting_manager.UTC)


@pytest.fixture
def _grace_clock(monkeypatch):
    """Controls `_utcnow()` (the grace-window clock) independently of
    `now_ist()` (the alert-window clock) -- returns a mutable holder so a
    test can advance time mid-test without re-patching."""
    current = {"v": _SELF_HEALING_START}
    monkeypatch.setattr(alerting_manager, "_utcnow", lambda: current["v"])
    return current


@pytest.mark.parametrize(
    "category",
    ["protective_stop_cancel_unresolved", "exit_order_unfilled", "reconciliation_mismatch"],
)
def test_telegram_blocked_on_first_sighting_of_a_self_healing_category(
    db: Session, workspace, _configured, _within_alert_window, _grace_clock, category
):
    """The very first occurrence must never push immediately -- it only
    records the sighting and starts the grace clock."""
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=category,
        message="y",
        dedup_key="position-1",
    )

    assert _configured == []


def test_telegram_still_blocked_before_the_grace_window_elapses(
    db: Session, workspace, _configured, _within_alert_window, _grace_clock
):
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category="exit_order_unfilled",
        message="y",
        dedup_key="position-1",
    )
    _grace_clock["v"] = _SELF_HEALING_START + timedelta(
        seconds=alerting_manager._SELF_HEALING_GRACE_SECONDS - 1
    )
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category="exit_order_unfilled",
        message="y",
        dedup_key="position-1",
    )

    assert _configured == []


def test_telegram_pushes_once_the_grace_window_elapses_and_the_issue_recurs(
    db: Session, workspace, _configured, _within_alert_window, _grace_clock
):
    """A genuinely stuck case (still recurring after the grace window) must
    still reach Telegram -- this gate only delays, never permanently
    suppresses."""
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category="exit_order_unfilled",
        message="y",
        dedup_key="position-1",
    )
    _grace_clock["v"] = _SELF_HEALING_START + timedelta(
        seconds=alerting_manager._SELF_HEALING_GRACE_SECONDS
    )
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category="exit_order_unfilled",
        message="y",
        dedup_key="position-1",
    )

    assert len(_configured) == 1


def test_self_healing_grace_never_fires_if_the_issue_never_recurs(
    db: Session, workspace, _configured, _within_alert_window, _grace_clock
):
    """The whole point: a position that self-heals within one call never
    triggers a second send_alert, so Telegram is never reached at all --
    not even after the grace window would have elapsed."""
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category="protective_stop_cancel_unresolved",
        message="y",
        dedup_key="position-1",
    )

    assert _configured == []
    # Confirmed the grace clock genuinely would have allowed a push by now --
    # this really is "never called again", not "called too soon to tell".
    _grace_clock["v"] = _SELF_HEALING_START + timedelta(hours=1)
    assert _configured == []


def test_self_healing_grace_does_not_apply_to_other_categories(
    db: Session, workspace, _configured, _within_alert_window, _grace_clock
):
    """A category outside the self-healing set (e.g. the terminal
    exit_order_attempts_exhausted signal) must push on its very first
    occurrence, unaffected by this gate."""
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category=_ALLOWED_CATEGORY,
        message="y",
        dedup_key="position-1",
    )

    assert len(_configured) == 1


def test_self_healing_grace_does_not_consume_a_dedup_slot(
    db: Session, workspace, _configured, _within_alert_window, _grace_clock
):
    """A candidate held back by the grace window must not start/extend the
    15-minute dedup cooldown -- once it does push (grace elapsed), a later
    genuinely-new recurrence should not be silently swallowed by a dedup
    window that was never really started."""
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category="exit_order_unfilled",
        message="y",
        dedup_key="position-1",
    )
    assert alerting_manager._last_pushed_by_key.get("position-1") is None


# 2026-09-03: row-collapse -- a recurring alert with the same dedup_key
# updates one row (occurrence_count++) instead of inserting a new row every
# time. See send_alert's own docstring for the full design and the real
# incident (198 reconciliation_mismatch rows in one day) this closes.


def test_send_alert_collapses_a_repeat_within_the_window(db: Session, workspace):
    first = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.WARNING,
        category="test_category",
        message="first occurrence",
        dedup_key="same-issue",
    )
    second = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.WARNING,
        category="test_category",
        message="second occurrence",
        dedup_key="same-issue",
    )

    assert second.id == first.id
    row = db.get(SystemAlert, first.id)
    assert row is not None
    assert row.occurrence_count == 2
    assert row.message == "second occurrence"
    assert (
        db.query(SystemAlert).filter(SystemAlert.dedup_key == "same-issue").count() == 1
    )


def test_send_alert_collapse_keeps_the_higher_severity(db: Session, workspace):
    """A later, lower-severity recurrence must never mask an earlier CRITICAL
    occurrence on the same collapsed row (health_check_failed genuinely
    varies WARNING/CRITICAL within one category -- see _max_severity)."""
    first = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.CRITICAL,
        category="test_category",
        message="critical first",
        dedup_key="same-issue",
    )
    send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.WARNING,
        category="test_category",
        message="warning second",
        dedup_key="same-issue",
    )

    row = db.get(SystemAlert, first.id)
    assert row is not None
    assert row.severity == AlertSeverity.CRITICAL
    assert row.occurrence_count == 2


def test_send_alert_different_dedup_keys_do_not_collapse(db: Session, workspace):
    first = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.WARNING,
        category="test_category",
        message="y",
        dedup_key="issue-a",
    )
    second = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.WARNING,
        category="test_category",
        message="y",
        dedup_key="issue-b",
    )

    assert first.id != second.id
    first_row = db.get(SystemAlert, first.id)
    second_row = db.get(SystemAlert, second.id)
    assert first_row is not None
    assert second_row is not None
    assert first_row.occurrence_count == 1
    assert second_row.occurrence_count == 1


def test_send_alert_starts_a_fresh_row_once_the_existing_one_is_resolved(
    db: Session, workspace
):
    first = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.WARNING,
        category="test_category",
        message="y",
        dedup_key="same-issue",
    )
    row = db.get(SystemAlert, first.id)
    assert row is not None
    row.is_resolved = True
    db.flush()

    second = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.WARNING,
        category="test_category",
        message="y",
        dedup_key="same-issue",
    )

    assert second.id != first.id
    second_row = db.get(SystemAlert, second.id)
    assert second_row is not None
    assert second_row.occurrence_count == 1


def test_send_alert_does_not_collapse_across_workspaces(db: Session, workspace):
    other_workspace = Workspace(id=uuid.uuid4(), name=f"other-{uuid.uuid4().hex[:8]}")
    db.add(other_workspace)
    db.flush()

    first = send_alert(
        db,
        workspace_id=workspace.id,
        severity=AlertSeverity.WARNING,
        category="test_category",
        message="y",
        dedup_key="same-issue",
    )
    second = send_alert(
        db,
        workspace_id=other_workspace.id,
        severity=AlertSeverity.WARNING,
        category="test_category",
        message="y",
        dedup_key="same-issue",
    )

    assert first.id != second.id
