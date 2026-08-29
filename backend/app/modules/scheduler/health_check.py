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
`SYSTEM`-triggered `degraded_mode` edge from `live_enabled`, never from
`paper_only` — so a paper-only session is
correctly logged-and-alerted only, never escalated, same as a broker auth
failure. A `SystemAlert` is written per affected workspace regardless of
mode, so paper-only visibility still exists even though no mode transition
happens for it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.clock import check_disk_space, check_ntp_drift, is_windows
from app.core.db.session import SessionFactory, session_scope
from app.core.modes.state_machine import ModeTransitionError, transition_mode
from app.domain.market.models import Instrument
from app.domain.ops.models import AlertSeverity
from app.domain.session.models import (
    SafeMode,
    TradingSession,
    TradingSessionStatus,
    TransitionTriggerType,
)
from app.modules.alerting.manager import send_alert
from app.modules.market_data.freshness import (
    FreshnessState,
    FreshnessThresholds,
    underlying_feed_state,
)
from app.modules.market_data.market_hours import TRADABLE_UNDERLYINGS, is_within_market_hours
from app.modules.ops import weekend_rest
from app.modules.ops.metrics_service import record_metric
from app.modules.scheduler.base import IntervalScheduler

logger = logging.getLogger("app.scheduler.health_check")

DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS = 300.0

# 2026-08-29: NSE is closed all weekend, and NTP drift / disk usage develop
# no faster on a Saturday than a Tuesday -- poll at 1/4 the weekday cadence
# (20 min instead of 5) so the box genuinely rests. Applied via the
# IntervalScheduler._wait_seconds() hook, so run_once() stays unchanged and
# every existing test that drives it directly is unaffected.
WEEKEND_INTERVAL_MULTIPLIER = 4

_DEGRADABLE_MODES = (SafeMode.LIVE_ENABLED,)

# 2026-08-25: dedicated "worth alerting a human" threshold for
# _check_market_data_staleness -- deliberately its own tier, not a reuse of
# market_data.freshness's TICK_THRESHOLDS (60s stale) or
# OPTION_CHAIN_THRESHOLDS (600s stale but a different signal entirely,
# option-chain snapshots rather than underlying ticks). "degraded" isn't
# meaningfully used here (only STALE/DEAD trigger an alert), so its value
# just needs to sit below stale_after_seconds.
_MARKET_DATA_STALE_THRESHOLDS = FreshnessThresholds(
    degraded_after_seconds=300.0, stale_after_seconds=600.0
)


class HealthCheckScheduler(IntervalScheduler):
    _cycle_failed_log_message = "health check cycle failed"

    def __init__(
        self,
        interval_seconds: float = DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
        session_factory: SessionFactory = session_scope,
    ) -> None:
        super().__init__(logger, interval_seconds, session_factory=session_factory)

    def _wait_seconds(self) -> float:
        if weekend_rest.is_weekend_ist():
            return self._interval_seconds * WEEKEND_INTERVAL_MULTIPLIER
        return self._interval_seconds

    def _run_cycle(self, db: Session) -> None:
        ntp = check_ntp_drift()
        disk = check_disk_space("C:/" if is_windows() else "/")

        active_sessions = (
            db.query(TradingSession)
            .filter(TradingSession.status == TradingSessionStatus.ACTIVE)
            .all()
        )
        workspace_ids = {s.workspace_id for s in active_sessions}
        recorded_at = datetime.now(UTC)

        # Independent of the ntp/disk checks below (which early-return the
        # whole cycle when both are ok) -- always runs.
        self._check_market_data_staleness(db, workspace_ids)

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
                send_alert(
                    db,
                    workspace_id=trading_session.workspace_id,
                    severity=AlertSeverity.CRITICAL if not disk.ok else AlertSeverity.WARNING,
                    category="health_check_failed",
                    message=reason,
                    # No specific position/order behind this -- infra-level,
                    # not paper-suppressed (mode left at its None default).
                    # Severity gates the Telegram push on its own: only the
                    # disk-failure (CRITICAL) case reaches Telegram, never
                    # the NTP-drift-only (WARNING) case.
                )

        db.commit()

    def _check_market_data_staleness(self, db: Session, workspace_ids: set[uuid.UUID]) -> None:
        """2026-08-25: real gap closed -- `market_data.freshness` classifies
        tick staleness (`classify_latest_tick`) but until now nothing ever
        alerted on it; a strategy just silently skipped its cycle
        (`market_data.freshness`'s own callers), and an underlying with a
        dead feed but no strategy currently in a position produced zero
        signal anywhere. Checks the fixed `TRADABLE_UNDERLYINGS` universe
        (same convention `MarketDataScheduler`'s own pre-market subscribe
        already uses) against a dedicated 10-minute threshold — deliberately
        its own `FreshnessThresholds`, not a reuse of `TICK_THRESHOLDS`
        (60s) or `OPTION_CHAIN_THRESHOLDS`, since "worth alerting a human"
        is a coarser granularity than either of those existing tiers.

        Gated on `is_within_market_hours()` — no ticks are expected outside
        it at all, so checking then would just alert on nothing new every
        5 minutes overnight. Also skipped on any weekend
        (`weekend_rest.is_weekend_ist()`, calendar — *not* the awake/dormant
        state): NSE is closed Sat/Sun, so a stale NIFTY/BANKNIFTY feed is
        expected and never actionable, whether or not a user happens to be
        signed in. (Weekday market holidays are not handled — same scope
        limit as `weekend_rest` itself.) The NTP/disk body of `_run_cycle`
        is unaffected and still runs. No specific position/order behind this
        (an underlying-level feed check, not a trade), so `mode` is left at
        its `None` default — infra-level, not paper-suppressed, same
        reasoning as `health_check_failed` above.
        """
        if not workspace_ids or not is_within_market_hours() or weekend_rest.is_weekend_ist():
            return

        for symbol in TRADABLE_UNDERLYINGS:
            instrument = db.query(Instrument).filter(Instrument.symbol == symbol).one_or_none()
            if instrument is None:
                continue

            # Both the tick stream *and* the REST-fallback bar stream must be
            # stale before this counts as a dead feed -- during a WS outage
            # the ingestion service keeps `price_bars` flowing via REST
            # polling even though `quote_ticks` have stopped, and that's a
            # healthy state, not one to alert a human about.
            state = underlying_feed_state(
                db,
                instrument.id,
                tick_thresholds=_MARKET_DATA_STALE_THRESHOLDS,
                bar_thresholds=_MARKET_DATA_STALE_THRESHOLDS,
            )
            if state not in (FreshnessState.STALE, FreshnessState.DEAD):
                continue

            message = f"No fresh {symbol} tick for over 10 minutes (state={state.value})."
            logger.warning("Health check: %s", message)
            for workspace_id in workspace_ids:
                # dedup_key includes workspace_id -- a shared key here would
                # mean only the first workspace in this loop ever actually
                # pushes, silently swallowing every other workspace's own
                # genuinely separate alert for the same cycle.
                send_alert(
                    db,
                    workspace_id=workspace_id,
                    severity=AlertSeverity.CRITICAL,
                    category="market_data_stale",
                    message=message,
                    dedup_key=f"market_data_stale:{symbol}:{workspace_id}",
                )



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
