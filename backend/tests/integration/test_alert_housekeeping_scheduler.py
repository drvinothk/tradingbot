"""AlertHousekeepingScheduler -- auto-closes a `system_alerts` row once it
stops recurring (`last_seen_at` older than `system_alert_collapse_window_hours`)
and purges a resolved row past `system_alert_retention_days`. See
`app.modules.scheduler.alert_housekeeping`'s own module docstring and
`alerting.manager.send_alert`'s row-collapse docstring for the full design.

Same `_session_factory_for`/`.run_once()` pattern as
`test_reconciliation_lock_recovery_scheduler.py` -- no real threading, fully
deterministic.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.domain.ops.models import AlertSeverity, SystemAlert
from app.modules.scheduler.alert_housekeeping import AlertHousekeepingScheduler


def _session_factory_for(db: Session):
    @contextmanager
    def _factory():
        yield db

    return _factory


def _scheduler_for(db: Session) -> AlertHousekeepingScheduler:
    return AlertHousekeepingScheduler(session_factory=_session_factory_for(db))


def _make_alert(
    db: Session,
    workspace,
    *,
    created_at: datetime,
    last_seen_at: datetime,
    is_resolved: bool,
) -> SystemAlert:
    alert = SystemAlert(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        severity=AlertSeverity.WARNING,
        category="test_category",
        message="y",
        dedup_key=f"test_category:{uuid.uuid4()}",
        created_at=created_at,
        last_seen_at=last_seen_at,
        is_resolved=is_resolved,
    )
    db.add(alert)
    db.flush()
    return alert


def test_run_once_auto_closes_a_stale_unresolved_row(db: Session, workspace):
    now = datetime.now(UTC)
    stale = _make_alert(
        db, workspace, created_at=now - timedelta(hours=30), last_seen_at=now - timedelta(hours=25),
        is_resolved=False,
    )

    _scheduler_for(db).run_once()

    db.refresh(stale)
    assert stale.is_resolved is True
    assert stale.resolved_at is not None


def test_run_once_does_not_close_a_fresh_recurring_row(db: Session, workspace):
    now = datetime.now(UTC)
    fresh = _make_alert(
        db, workspace, created_at=now - timedelta(hours=30), last_seen_at=now - timedelta(hours=1),
        is_resolved=False,
    )

    _scheduler_for(db).run_once()

    db.refresh(fresh)
    assert fresh.is_resolved is False
    assert fresh.resolved_at is None


def test_run_once_purges_a_resolved_row_past_retention(db: Session, workspace):
    now = datetime.now(UTC)
    old_resolved = _make_alert(
        db, workspace, created_at=now - timedelta(days=31), last_seen_at=now - timedelta(days=31),
        is_resolved=True,
    )
    alert_id = old_resolved.id

    _scheduler_for(db).run_once()

    assert db.get(SystemAlert, alert_id) is None


def test_run_once_keeps_a_resolved_row_within_retention(db: Session, workspace):
    now = datetime.now(UTC)
    recent_resolved = _make_alert(
        db, workspace, created_at=now - timedelta(days=5), last_seen_at=now - timedelta(days=5),
        is_resolved=True,
    )
    alert_id = recent_resolved.id

    _scheduler_for(db).run_once()

    assert db.get(SystemAlert, alert_id) is not None


def test_run_once_measures_retention_from_resolved_at_not_created_at(db: Session, workspace):
    """A chronic issue that recurred for weeks before finally resolving must
    get the full retention window from its *resolution*, not from when it
    first started -- otherwise a long-running incident could purge almost
    immediately after resolving."""
    now = datetime.now(UTC)
    old_but_recently_resolved = _make_alert(
        db, workspace, created_at=now - timedelta(days=40), last_seen_at=now - timedelta(days=2),
        is_resolved=True,
    )
    old_but_recently_resolved.resolved_at = now - timedelta(days=2)
    db.flush()
    alert_id = old_but_recently_resolved.id

    _scheduler_for(db).run_once()

    assert db.get(SystemAlert, alert_id) is not None


def test_run_once_purges_via_created_at_fallback_when_resolved_at_is_null(
    db: Session, workspace
):
    """Rows resolved by the 2026-08-27 one-off manual SQL fix (`UPDATE
    system_alerts SET is_resolved=true`, no resolved_at) must still age out
    eventually -- COALESCE falls back to created_at for exactly this case."""
    now = datetime.now(UTC)
    legacy_resolved = _make_alert(
        db, workspace, created_at=now - timedelta(days=31), last_seen_at=now - timedelta(days=31),
        is_resolved=True,
    )
    legacy_resolved.resolved_at = None
    db.flush()
    alert_id = legacy_resolved.id

    _scheduler_for(db).run_once()

    assert db.get(SystemAlert, alert_id) is None


def test_run_once_never_purges_an_unresolved_row_regardless_of_age(db: Session, workspace):
    """A chronic, still-recurring issue must never be silently deleted for
    being old -- it keeps resetting last_seen_at and stays visible instead."""
    now = datetime.now(UTC)
    chronic = _make_alert(
        db, workspace, created_at=now - timedelta(days=40), last_seen_at=now,
        is_resolved=False,
    )
    alert_id = chronic.id

    _scheduler_for(db).run_once()

    row = db.get(SystemAlert, alert_id)
    assert row is not None
    assert row.is_resolved is False
