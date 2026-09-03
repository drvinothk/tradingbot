"""`AlertHousekeepingScheduler`: closes the loop on `system_alerts`
row-collapse (see `alerting.manager.send_alert`'s own docstring) -- nothing
previously auto-resolved a row (`is_resolved` was permanently stuck `False`
for every row this codebase ever wrote outside a one-off manual SQL fix), so
old noise accumulated forever and drowned out genuinely current issues in the
Control Room Attention card's `?is_resolved=false` query.

Same background-thread shape as `scheduler.health_check.HealthCheckScheduler`/
`scheduler.reconciliation_lock_recovery.ReconciliationLockRecoveryScheduler`
(daemon thread via `IntervalScheduler`, its own short-lived `session_scope()`
per cycle, `run_once()` exposed for deterministic tests, a module-level
singleton).

Two independent sweeps per cycle, both driven off `Settings.app`:
- **Auto-close**: any unresolved row whose `last_seen_at` is older than
  `system_alert_collapse_window_hours` (default 24h) is marked resolved --
  it hasn't recurred, so the issue is no longer actively firing (see
  `send_alert`'s docstring for the caveat: this means "not re-detected,"
  not "verified fixed").
- **Purge**: any *resolved* row older than `system_alert_retention_days`
  (default 30) is deleted outright. Gated on `is_resolved` so a chronic,
  still-recurring issue is never silently deleted for being old -- it keeps
  resetting its own `last_seen_at` and stays visible instead.

Both are plain bulk `UPDATE`/`DELETE` statements (`synchronize_session=False`
-- this scheduler's session is short-lived and holds no other in-memory
references to these rows), matching how prior incidents in this codebase
were cleaned up by hand before this existed.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.db.session import SessionFactory, session_scope
from app.domain.ops.models import SystemAlert
from app.modules.scheduler.base import IntervalScheduler

logger = logging.getLogger("app.scheduler.alert_housekeeping")

DEFAULT_INTERVAL_SECONDS = 3600.0


class AlertHousekeepingScheduler(IntervalScheduler):
    _cycle_failed_log_message = "alert housekeeping cycle failed"

    def __init__(
        self,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        session_factory: SessionFactory = session_scope,
    ) -> None:
        super().__init__(logger, interval_seconds, session_factory=session_factory)

    def _run_cycle(self, db: Session) -> None:
        now = datetime.now(UTC)
        app_settings = get_settings().app
        collapse_window = timedelta(hours=app_settings.system_alert_collapse_window_hours)
        retention = timedelta(days=app_settings.system_alert_retention_days)

        auto_closed = (
            db.query(SystemAlert)
            .filter(
                SystemAlert.is_resolved.is_(False),
                SystemAlert.last_seen_at < now - collapse_window,
            )
            .update({"is_resolved": True, "resolved_at": now}, synchronize_session=False)
        )
        purged = (
            db.query(SystemAlert)
            .filter(
                SystemAlert.is_resolved.is_(True),
                # COALESCE, not bare resolved_at: retention should count from
                # when the issue actually stopped (resolved_at), not when it
                # first started (created_at) -- a chronic issue recurring for
                # weeks then resolving must still get the full window from
                # its resolution, not from day zero. Falls back to created_at
                # for rows resolved by the 2026-08-27 one-off manual SQL fix,
                # which set is_resolved=true but never resolved_at.
                func.coalesce(SystemAlert.resolved_at, SystemAlert.created_at) < now - retention,
            )
            .delete(synchronize_session=False)
        )
        db.commit()

        if auto_closed or purged:
            logger.info(
                "alert housekeeping: auto-closed=%d purged=%d", auto_closed, purged
            )


_scheduler: AlertHousekeepingScheduler | None = None


def ensure_alert_housekeeping_scheduler_running(
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
) -> AlertHousekeepingScheduler:
    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = AlertHousekeepingScheduler(interval_seconds=interval_seconds)
        _scheduler.start()
    return _scheduler


def stop_alert_housekeeping_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.stop()
        _scheduler = None
