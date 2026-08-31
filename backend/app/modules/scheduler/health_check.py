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
from sqlalchemy.pool import QueuePool

from app.config.settings import get_settings
from app.core.clock import check_disk_space, check_ntp_drift, is_windows
from app.core.db.session import SessionFactory, engine, session_scope
from app.core.locking import (
    LOCK_ACQUIRE_TIMEOUT,
    LOCK_AUDIT_CHAIN,
    LOCK_EXECUTION_SINGLETON,
    LOCK_RISK_EVALUATION_QUEUE,
    pop_lock_hold_stats,
    pop_lock_wait_stats,
)
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
# 2026-08-31: tightened 10min -> 5min per explicit user request -- this is
# the "no tick from any feed, primary or backup" signal (see the check's own
# docstring: it reads quote_ticks/price_bars by instrument_id only, with no
# notion of which provider is currently active), so 5 minutes is the real
# "reconnect and look at this" threshold; the 5-minute HealthCheckScheduler
# cadence means worst-case detection latency is ~5-10 minutes.
_MARKET_DATA_STALE_THRESHOLDS = FreshnessThresholds(
    degraded_after_seconds=150.0, stale_after_seconds=300.0
)

# 2026-08-31: leading indicators for the whole-app-hang incident (see
# settings.py's DBSettings.pool_size comment for the full root cause) --
# alert-only, deliberately never escalating to degraded_mode the way the
# ntp/disk checks above do: a pool/lock spike during a real multi-strategy
# entry burst is expected, self-recovering load, not a broker/infra failure,
# and forcing a live session out of live_enabled over a transient burst would
# be the exact "twitchy" trap this codebase's own failover-threshold
# reasoning already avoids elsewhere.
_POOL_SATURATION_ALERT_RATIO = 0.8
# 70% of core.locking.LOCK_ACQUIRE_TIMEOUT (10s) -- worth a human's attention
# before a dispatch actually starts failing with a lock-timeout error.
_LOCK_WAIT_ALERT_THRESHOLD_SECONDS = 7.0
# A place_order/close_position broker call holding LOCK_EXECUTION_SINGLETON
# this long is already anomalously slow on its own -- worth surfacing before
# it also starts causing other callers' *wait* time to approach the timeout
# above. WARNING, not CRITICAL (see _check_lock_hold_time) -- this is a
# root-cause/diagnostic signal, distinct from "something is actively being
# blocked right now," which _check_lock_contention above already covers.
_LOCK_HOLD_ALERT_THRESHOLD_SECONDS = 5.0
# Only the real, named locks this system relies on (core/locking.py's own
# "add new ones here rather than inventing ad-hoc strings" list) -- filters
# out any other lock name (a test's own throwaway lock, or any future ad-hoc
# caller) so it can never show up as a metric or alert here.
_MONITORED_LOCK_NAMES = frozenset(
    {LOCK_EXECUTION_SINGLETON, LOCK_RISK_EVALUATION_QUEUE, LOCK_AUDIT_CHAIN}
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
        self._check_db_pool_saturation(db, workspace_ids, recorded_at)
        self._check_lock_contention(db, workspace_ids, recorded_at)
        self._check_lock_hold_time(db, workspace_ids, recorded_at)

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
        already uses) against a dedicated 5-minute threshold (tightened from
        10 minutes 2026-08-31, per explicit user request) — deliberately
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

            message = f"No fresh {symbol} tick for over 5 minutes (state={state.value})."
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

    def _check_db_pool_saturation(
        self, db: Session, workspace_ids: set[uuid.UUID], recorded_at: datetime
    ) -> None:
        """2026-08-31: leading indicator for the exact incident fixed in
        `DBSettings.pool_size` (see that field's own comment) -- reads
        `engine.pool.checkedout()`, a free in-memory counter (no query), so
        this adds no load of its own. Recorded every cycle regardless of
        workspace_ids/market hours, same as ntp_drift_seconds/disk_free_gb,
        for continuous visibility even overnight.

        `checkedout()` is a `QueuePool`-specific method, not on the abstract
        `Pool` base `engine.pool` is typed as -- `create_engine` always
        returns a `QueuePool` for this app's Postgres URL (no `NullPool`/
        `StaticPool` override anywhere), but the isinstance guard keeps this
        degrading to "0, skip" rather than an AttributeError if that ever
        changes, instead of taking the whole health-check cycle down with it.
        """
        settings = get_settings()
        capacity = settings.db.pool_size + settings.db.max_overflow
        pool = engine.pool
        checked_out = pool.checkedout() if isinstance(pool, QueuePool) else 0
        ratio = checked_out / capacity if capacity else 0.0

        for workspace_id in workspace_ids:
            record_metric(
                db,
                workspace_id=workspace_id,
                metric_name="db_pool_checked_out",
                value=float(checked_out),
                recorded_at=recorded_at,
                tags={"capacity": capacity},
            )

        if ratio < _POOL_SATURATION_ALERT_RATIO:
            return

        message = (
            f"DB connection pool at {checked_out}/{capacity} checked out "
            f"({ratio:.0%}) -- order dispatch and API requests may start "
            f"queuing for a connection."
        )
        logger.warning("Health check: %s", message)
        for workspace_id in workspace_ids:
            send_alert(
                db,
                workspace_id=workspace_id,
                severity=AlertSeverity.CRITICAL,
                category="db_pool_saturated",
                message=message,
                dedup_key=f"db_pool_saturated:{workspace_id}",
            )

    def _check_lock_contention(
        self, db: Session, workspace_ids: set[uuid.UUID], recorded_at: datetime
    ) -> None:
        """2026-08-31: leading indicator for the *other* half of the same
        incident -- `db_pool_saturation` above catches the symptom
        (connections held while queued), this catches the cause (how long
        callers actually wait to acquire `LOCK_EXECUTION_SINGLETON` et al).
        Drains `core.locking.pop_lock_wait_stats()`, which only ever
        populates an entry once a real acquisition took >= 1s (see that
        module's own `_SLOW_ACQUIRE_THRESHOLD_SECONDS`) -- most cycles this
        is empty and nothing is recorded, by design.

        Filtered to `_MONITORED_LOCK_NAMES` -- the stats dict is a single
        process-wide store shared with anything that ever calls
        `advisory_lock` (including test code using a throwaway lock name),
        so this filter is what keeps an unrelated caller's lock name from
        ever surfacing here as a metric or alert.
        """
        for lock_name, (max_wait, slow_count) in pop_lock_wait_stats().items():
            if lock_name not in _MONITORED_LOCK_NAMES:
                continue

            for workspace_id in workspace_ids:
                record_metric(
                    db,
                    workspace_id=workspace_id,
                    metric_name="lock_wait_max_seconds",
                    value=max_wait,
                    recorded_at=recorded_at,
                    tags={"lock_name": lock_name, "slow_count": slow_count},
                )

            if max_wait < _LOCK_WAIT_ALERT_THRESHOLD_SECONDS:
                continue

            message = (
                f"Advisory lock '{lock_name}' took up to {max_wait:.1f}s to acquire "
                f"({slow_count} slow acquisition(s) since last check) -- approaching "
                f"the {LOCK_ACQUIRE_TIMEOUT} timeout."
            )
            logger.warning("Health check: %s", message)
            for workspace_id in workspace_ids:
                send_alert(
                    db,
                    workspace_id=workspace_id,
                    severity=AlertSeverity.CRITICAL,
                    category="lock_contention_high",
                    message=message,
                    dedup_key=f"lock_contention_high:{lock_name}:{workspace_id}",
                )

    def _check_lock_hold_time(
        self, db: Session, workspace_ids: set[uuid.UUID], recorded_at: datetime
    ) -> None:
        """2026-08-31: root-cause counterpart to `_check_lock_contention` --
        that one measures how long callers *wait* to acquire a lock; this
        measures how long a caller actually *holds* one once acquired
        (`core.locking.pop_lock_hold_stats()`, same drain-on-read/1s-gate
        design as the wait-side tracker). Answers the question this whole
        investigation started from directly: is a slow acquire actually
        caused by a slow broker call while the lock is held? See
        `advisory_lock`'s own docstring for the one caveat -- this
        undercounts hold time for the few call sites that commit early,
        inside the `with` block, by design (not `dispatch_trade_intent`/
        `close_position`, the two this exists to diagnose).

        WARNING, not CRITICAL, and deliberately outside `TELEGRAM_ALLOWED_
        CATEGORIES` -- a single slow broker call with nobody else contending
        for the lock isn't yet causing any real queuing (that's what
        `lock_contention_high` above alerts CRITICAL for); this is a
        diagnostic signal for the dashboard/audit trail, not a phone page.
        Same `_MONITORED_LOCK_NAMES` filter as `_check_lock_contention`, for
        the same reason (a throwaway test lock name must never surface here).
        """
        for lock_name, (max_hold, slow_count) in pop_lock_hold_stats().items():
            if lock_name not in _MONITORED_LOCK_NAMES:
                continue

            for workspace_id in workspace_ids:
                record_metric(
                    db,
                    workspace_id=workspace_id,
                    metric_name="lock_hold_max_seconds",
                    value=max_hold,
                    recorded_at=recorded_at,
                    tags={"lock_name": lock_name, "slow_count": slow_count},
                )

            if max_hold < _LOCK_HOLD_ALERT_THRESHOLD_SECONDS:
                continue

            message = (
                f"Advisory lock '{lock_name}' was held for up to {max_hold:.1f}s "
                f"({slow_count} slow hold(s) since last check) -- likely a slow "
                f"broker call while the lock was held, not just contention."
            )
            logger.warning("Health check: %s", message)
            for workspace_id in workspace_ids:
                send_alert(
                    db,
                    workspace_id=workspace_id,
                    severity=AlertSeverity.WARNING,
                    category="lock_hold_high",
                    message=message,
                    dedup_key=f"lock_hold_high:{lock_name}:{workspace_id}",
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
