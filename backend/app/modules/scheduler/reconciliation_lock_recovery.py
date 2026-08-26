"""`ReconciliationLockRecoveryScheduler`: unattended recovery from
`reconciliation_lock` — 2026-08-25, closing a real gap the user raised: the
manual recovery button (`POST /sessions/{id}/recover-from-reconciliation-lock`,
`api/v1/sessions.py`) already re-runs `run_full_reconciliation` and only
proceeds if clean, but nothing previously re-checked a locked session on its
own — a purely technical, since-resolved mismatch stayed locked (blocking
all new live entries) until a human happened to open the app and click it.

Same background-thread shape as `scheduler.health_check.HealthCheckScheduler`
(daemon thread, its own short-lived `session_scope()` per cycle, a
`stop_event` for clean shutdown, `run_once()` exposed for deterministic
tests, a plain module-level singleton) — a genuinely different concern from
NTP/disk health, kept as its own scheduler rather than folded into that one,
matching this codebase's own one-concern-per-scheduler precedent
(`MarketDataScheduler`, `DailyBootstrapScheduler`, `ContractSyncScheduler`,
`TradeLogExportScheduler` are all separate too).

**The actual recovery logic lives in `core.modes.state_machine.recover_from_
reconciliation_lock`, not here** — this scheduler's only job is deciding
*when* to call it: `run_full_reconciliation` (the exact aggregator the
manual button already uses, no new reconciliation logic) once per cycle per
locked session, incrementing `TradingSession.reconciliation_lock_clean_
streak` on a clean result and resetting it to 0 on a dirty one, then calling
the state-machine function with `TransitionTriggerType.RECONCILIATION` once
the streak reaches `DEFAULT_CLEAN_STREAK_THRESHOLD` consecutive clean
checks, each `DEFAULT_INTERVAL_SECONDS` apart. See that function's own
docstring for why `RECONCILIATION` (not a bare `SYSTEM` trigger) is the only
automatic trigger allowed to resume a locked session all the way back to a
live `prior_mode` — a deliberate, scoped exception to this codebase's Rule 4,
confirmed explicitly with the user, reserved exclusively for this scheduler.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core.db.session import SessionFactory, session_scope
from app.core.modes.state_machine import ModeTransitionError, recover_from_reconciliation_lock
from app.domain.broker.models import ReconciliationTrigger
from app.domain.session.models import (
    SafeMode,
    TradingSession,
    TradingSessionStatus,
    TransitionTriggerType,
)
from app.modules.reconciliation.service import run_full_reconciliation
from app.modules.scheduler.base import IntervalScheduler

logger = logging.getLogger("app.scheduler.reconciliation_lock_recovery")

DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_CLEAN_STREAK_THRESHOLD = 3


class ReconciliationLockRecoveryScheduler(IntervalScheduler):
    _cycle_failed_log_message = "reconciliation-lock recovery cycle failed"

    def __init__(
        self,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        clean_streak_threshold: int = DEFAULT_CLEAN_STREAK_THRESHOLD,
        session_factory: SessionFactory = session_scope,
    ) -> None:
        super().__init__(logger, interval_seconds, session_factory=session_factory)
        self._clean_streak_threshold = clean_streak_threshold

    def _run_cycle(self, db: Session) -> None:
        locked_sessions = (
            db.query(TradingSession)
            .filter(
                TradingSession.status == TradingSessionStatus.ACTIVE,
                TradingSession.mode == SafeMode.RECONCILIATION_LOCK,
            )
            .all()
        )
        for trading_session in locked_sessions:
            try:
                self._check_one(db, trading_session)
            except Exception:  # noqa: BLE001 - one session's broker error (e.g. a transient
                # connectivity blip on the real GetPositions call inside run_full_
                # reconciliation) must never block the other locked sessions in this
                # same cycle from being checked and committed.
                logger.exception(
                    "reconciliation-lock recovery check failed for session %s",
                    trading_session.id,
                )
        db.commit()

    def _check_one(self, db: Session, trading_session: TradingSession) -> None:
        runs = run_full_reconciliation(db, trading_session, ReconciliationTrigger.POLL)
        is_clean = all(run.mismatches_found == 0 for run in runs)

        if not is_clean:
            trading_session.reconciliation_lock_clean_streak = 0
            db.add(trading_session)
            return

        trading_session.reconciliation_lock_clean_streak += 1
        db.add(trading_session)

        if trading_session.reconciliation_lock_clean_streak < self._clean_streak_threshold:
            return

        streak = trading_session.reconciliation_lock_clean_streak
        try:
            recover_from_reconciliation_lock(
                db,
                trading_session,
                TransitionTriggerType.RECONCILIATION,
                reason=(
                    f"auto-recovered after {streak} consecutive clean "
                    "reconciliation checks"
                ),
            )
            logger.info(
                "session %s auto-recovered from reconciliation_lock after %d "
                "consecutive clean checks (mode now %s)",
                trading_session.id,
                streak,
                trading_session.mode,
            )
        except ModeTransitionError:
            logger.exception(
                "reconciliation_lock auto-recovery failed for session %s despite a "
                "%d-check clean streak",
                trading_session.id,
                streak,
            )



_scheduler: ReconciliationLockRecoveryScheduler | None = None


def ensure_reconciliation_lock_recovery_scheduler_running(
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    clean_streak_threshold: int = DEFAULT_CLEAN_STREAK_THRESHOLD,
) -> ReconciliationLockRecoveryScheduler:
    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = ReconciliationLockRecoveryScheduler(
            interval_seconds=interval_seconds, clean_streak_threshold=clean_streak_threshold
        )
        _scheduler.start()
    return _scheduler


def stop_reconciliation_lock_recovery_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.stop()
        _scheduler = None
