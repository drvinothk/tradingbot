"""`HealthCheckScheduler`: the periodic timer loop `core/clock.py`'s own
docstring has promised since Phase 0 ("the Scheduler module... calls these on
a periodic loop") and `ShoonyaBrokerAdapter`'s docstring names as "the next
concrete step" — NTP/disk checks previously only ran once at
`app.main`'s startup (`_run_startup_health_checks`, logged, never fatal).

Same background-thread shape as `execution_engine.paper.position_manager.
PositionManager` (daemon thread, its own short-lived `session_scope()` per
cycle, a `stop_event` for clean shutdown, `run_once()` exposed separately so
tests can drive it deterministically) — but a single process-wide instance,
not one per trading_session, since NTP drift and disk space are process-wide
facts, not per-session ones. A plain module-level singleton is enough (no
per-instance registry dict like `execution_engine/paper/registry.py`'s,
since there's only ever one), same reasoning `broker_adapter.composition`'s
`_broker` singleton already relies on: `core.locking.LOCK_PROCESS_SINGLETON`
guarantees exactly one backend process.

Reaction to a failing check reuses `PositionManager._handle_broker_auth_error`'s
exact legal-edge reasoning: `core/modes/transitions.py` only has a
`SYSTEM`-triggered `degraded_mode` edge from `paper_plus_guarded_live`/
`live_enabled`, never from `paper_only` — so a paper-only session is
correctly logged-and-alerted only, never escalated, same as a broker auth
failure. A `SystemAlert` is written per affected workspace regardless of
mode, so paper-only visibility still exists even though no mode transition
happens for it.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.clock import check_disk_space, check_ntp_drift
from app.core.db.session import session_scope
from app.core.modes.state_machine import ModeTransitionError, transition_mode
from app.domain.ops.models import AlertSeverity, SystemAlert
from app.domain.session.models import (
    SafeMode,
    TradingSession,
    TradingSessionStatus,
    TransitionTriggerType,
)
from app.modules.ops.metrics_service import record_metric

logger = logging.getLogger("app.scheduler.health_check")

SessionFactory = Callable[[], AbstractContextManager[Session]]

DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS = 300.0

_DEGRADABLE_MODES = (SafeMode.PAPER_PLUS_GUARDED_LIVE, SafeMode.LIVE_ENABLED)


class HealthCheckScheduler:
    def __init__(
        self,
        interval_seconds: float = DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
        session_factory: SessionFactory = session_scope,
    ) -> None:
        self._interval_seconds = interval_seconds
        self._session_factory = session_factory
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_seconds + 5)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_once(self) -> None:
        with self._session_factory() as db:
            self._run_cycle(db)

    def _run_cycle(self, db: Session) -> None:
        ntp = check_ntp_drift()
        disk = check_disk_space("C:/" if _is_windows() else "/")

        active_sessions = (
            db.query(TradingSession)
            .filter(TradingSession.status == TradingSessionStatus.ACTIVE)
            .all()
        )
        workspace_ids = {s.workspace_id for s in active_sessions}
        recorded_at = datetime.now(UTC)
        for workspace_id in workspace_ids:
            record_metric(
                db,
                workspace_id=workspace_id,
                metric_name="ntp_drift_seconds",
                value=ntp.drift_seconds if ntp.drift_seconds is not None else 0.0,
                recorded_at=recorded_at,
            )
            record_metric(
                db,
                workspace_id=workspace_id,
                metric_name="disk_free_gb",
                value=disk.free_gb,
                recorded_at=recorded_at,
            )

        if ntp.ok and disk.ok:
            db.commit()
            return

        reason = (
            f"health check failed: ntp_ok={ntp.ok} "
            f"(drift={ntp.drift_seconds}, err={ntp.error}) "
            f"disk_ok={disk.ok} (free_gb={disk.free_gb:.1f})"
        )[:500]
        logger.warning(reason)

        alerted_workspaces: set[uuid.UUID] = set()
        for trading_session in active_sessions:
            from_mode = SafeMode(trading_session.mode)
            if from_mode in _DEGRADABLE_MODES:
                try:
                    transition_mode(
                        db,
                        trading_session,
                        SafeMode.DEGRADED_MODE,
                        TransitionTriggerType.SYSTEM,
                        reason=reason,
                    )
                except ModeTransitionError:
                    logger.exception(
                        "could not move session %s to degraded_mode after failed health check",
                        trading_session.id,
                    )

            if trading_session.workspace_id not in alerted_workspaces:
                alerted_workspaces.add(trading_session.workspace_id)
                db.add(
                    SystemAlert(
                        id=uuid.uuid4(),
                        workspace_id=trading_session.workspace_id,
                        trading_session_id=None,
                        severity=AlertSeverity.CRITICAL if not disk.ok else AlertSeverity.WARNING,
                        category="health_check_failed",
                        message=reason,
                        created_at=recorded_at,
                    )
                )

        db.commit()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - a background loop must never die silently-crashed
                logger.exception("health check cycle failed")
            self._stop_event.wait(self._interval_seconds)


def _is_windows() -> bool:
    import sys

    return sys.platform == "win32"


_scheduler: HealthCheckScheduler | None = None


def ensure_health_check_scheduler_running(
    interval_seconds: float = DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
) -> HealthCheckScheduler:
    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = HealthCheckScheduler(interval_seconds=interval_seconds)
        _scheduler.start()
    return _scheduler


def stop_health_check_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.stop()
        _scheduler = None
