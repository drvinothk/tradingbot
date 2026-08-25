"""Ops-Hardening Phase 4. `DailyBootstrapScheduler` — same background-thread
shape as `scheduler.health_check.HealthCheckScheduler` (daemon thread, a
`stop_event`, `run_once()` exposed separately so tests can drive it
deterministically), triggering once daily at `BOOTSTRAP_TIME` (09:00 IST,
pre-market). Business logic (`run_daily_bootstrap`) lives in this same file
rather than split into a separate module, matching `HealthCheckScheduler`'s
own single-file shape exactly (unlike `market_data_scheduler.py`/
`market_hours.py` or this project's own `reporting/export_scheduler.py`/
`exporter.py`, which split thread-mechanics from pure logic).

**Real deviation from the original spec, made deliberately: stale sessions
are only ever auto-closed if they're genuinely empty.** The spec's literal
"forcefully transition it to CLOSED" would bypass `api.v1.sessions
.end_session`'s own existing safety check, which refuses (409) to close a
session that still has open positions or live strategy runs -- for good
reason: force-closing would orphan real open risk with nothing left
tracking it (no `PositionManager`, no reachable path to manage that
position's stop/target afterward). A stale `ACTIVE` session found *with*
real open risk at 09:00 (EOD square-off should already have cleared it by
`cutoff_time` the day before) is a genuinely abnormal, alarming state --
handled by firing a CRITICAL alert (`app.modules.alerting.manager
.send_alert`, Phase 2) for a human to investigate, not by silently
discarding the tracking. Today's session is still created regardless, so a
stuck stale session from yesterday never blocks today's trading.

Every date comparison goes through `now_ist()`/`to_ist()` -- comparing a
UTC-aware `started_at`'s raw `.date()` against an IST calendar date (which
`api.v1.sessions.create_session`'s own existing "today" check does) is
wrong near the IST midnight boundary; this module deliberately doesn't
repeat that pattern.

**Ops-Hardening Phase 6 (Auto-Spawner)**: `run_daily_bootstrap` now calls
`strategy_engine.auto_spawner.spawn_enabled_strategies` immediately after
resolving each workspace's today's `TradingSession`, closing the gap this
module always left open -- a session existed, but nothing traded until a
human clicked Start on every strategy. See that module's own docstring for
why it only creates `StrategyRun` rows and never starts a thread itself.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, time

from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.clock import now_ist, to_ist
from app.core.db.session import session_scope
from app.domain.audit.models import ActorType, EventCategory
from app.domain.execution.models import Order, OrderMode, Position, PositionStatus
from app.domain.identity.models import Workspace
from app.domain.ops.models import AlertSeverity
from app.domain.session.models import SafeMode, TradingSession, TradingSessionStatus
from app.domain.strategy.models import StrategyRun, StrategyRunStatus
from app.main import _resume_strategy_runners
from app.modules.alerting.manager import send_alert
from app.modules.audit_service.service import record_event
from app.modules.strategy_engine.auto_spawner import spawn_enabled_strategies

logger = logging.getLogger("app.session.bootstrapper")

SessionFactory = Callable[[], AbstractContextManager[Session]]

BOOTSTRAP_TIME = time(9, 0)


def _close_if_safe(db: Session, stale_session: TradingSession) -> None:
    live_runs = (
        db.query(StrategyRun)
        .filter(
            StrategyRun.trading_session_id == stale_session.id,
            StrategyRun.status != StrategyRunStatus.STOPPED,
        )
        .count()
    )
    open_positions = (
        db.query(Position)
        .filter(
            Position.trading_session_id == stale_session.id,
            Position.status == PositionStatus.OPEN,
        )
        .count()
    )

    if live_runs or open_positions:
        message = (
            f"trading_session {stale_session.id} (started "
            f"{to_ist(stale_session.started_at).date().isoformat()}) is still ACTIVE at "
            f"09:00 IST with {live_runs} live strategy run(s) and {open_positions} open "
            "position(s) -- EOD square-off should have cleared this by cutoff_time "
            "yesterday. NOT auto-closed (would orphan real open risk); today's session "
            "was created separately so trading isn't blocked, but this needs manual "
            "reconciliation."
        )
        logger.critical("Daily bootstrap: %s", message)
        # 2026-08-25: "no notification for paper trade at all" -- the open
        # risk left behind here could be entirely paper, entirely live, or
        # a mix (per-strategy graduation lets one session hold both). Only
        # push if at least one of the still-open positions is genuinely
        # LIVE; a paper-only stale session stays DB-only.
        has_live_open_position = (
            db.query(Position)
            .join(Order, Order.id == Position.opening_order_id)
            .filter(
                Position.trading_session_id == stale_session.id,
                Position.status == PositionStatus.OPEN,
                Order.mode == OrderMode.LIVE,
            )
            .count()
            > 0
        )
        send_alert(
            db,
            workspace_id=stale_session.workspace_id,
            severity=AlertSeverity.CRITICAL,
            category="stale_session_not_closed",
            message=message,
            trading_session_id=stale_session.id,
            mode=OrderMode.LIVE if has_live_open_position else OrderMode.PAPER,
        )
        return

    stale_session.status = TradingSessionStatus.ENDED
    stale_session.ended_at = datetime.now(UTC)
    db.add(stale_session)
    db.flush()
    record_event(
        db,
        workspace_id=stale_session.workspace_id,
        actor_type=ActorType.SYSTEM,
        event_category=EventCategory.SYSTEM_HEALTH,
        event_type="trading_session.auto_closed",
        entity_type="trading_session",
        entity_id=stale_session.id,
        trading_session_id=stale_session.id,
        broker_account_id=stale_session.broker_account_id,
        payload={"started_at": stale_session.started_at.isoformat()},
    )
    logger.warning(
        "Daily bootstrap: auto-closed stale, empty trading_session %s (started %s, no "
        "open positions or live runs)",
        stale_session.id,
        to_ist(stale_session.started_at).date().isoformat(),
    )


def _bootstrap_workspace(
    db: Session, workspace_id: uuid.UUID, today_ist: date
) -> TradingSession | None:
    """Returns today's `TradingSession` (existing or freshly created) so the
    caller can hand it to Phase 6's auto-spawner -- `None` only for the
    "no prior session ever, nothing to bootstrap from" skip case, where
    there's genuinely no session for the spawner to attach runs to either.
    """
    stale_active = (
        db.query(TradingSession)
        .filter(
            TradingSession.workspace_id == workspace_id,
            TradingSession.status == TradingSessionStatus.ACTIVE,
        )
        .all()
    )
    for stale_session in stale_active:
        if to_ist(stale_session.started_at).date() >= today_ist:
            continue  # today's own session, not stale -- left for the check below
        _close_if_safe(db, stale_session)

    all_sessions = (
        db.query(TradingSession).filter(TradingSession.workspace_id == workspace_id).all()
    )
    todays_session = next(
        (s for s in all_sessions if to_ist(s.started_at).date() == today_ist), None
    )
    if todays_session is not None:
        logger.info(
            "Daily bootstrap: today's trading_session already exists for workspace %s",
            workspace_id,
        )
        return todays_session

    most_recent = (
        db.query(TradingSession)
        .filter(TradingSession.workspace_id == workspace_id)
        .order_by(TradingSession.started_at.desc())
        .first()
    )
    if most_recent is None:
        logger.info(
            "Daily bootstrap: workspace %s has no prior trading_session to bootstrap "
            "from -- skipping (create the first session manually via the UI/API).",
            workspace_id,
        )
        return None

    defaults = get_settings().risk_defaults
    new_session = TradingSession(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        broker_account_id=most_recent.broker_account_id,
        started_by_user_id=most_recent.started_by_user_id,
        mode=SafeMode.PAPER_ONLY,
        started_at=datetime.now(UTC),
        budget_amount=defaults.default_budget,
        daily_target_profit=defaults.daily_target_profit,
        daily_loss_cap=defaults.daily_loss_cap,
        funding_mode=most_recent.funding_mode,
    )
    db.add(new_session)
    db.flush()
    record_event(
        db,
        workspace_id=workspace_id,
        actor_type=ActorType.SYSTEM,
        event_category=EventCategory.SYSTEM_HEALTH,
        event_type="trading_session.bootstrapped",
        entity_type="trading_session",
        entity_id=new_session.id,
        trading_session_id=new_session.id,
        broker_account_id=new_session.broker_account_id,
        payload={"broker_account_id": str(new_session.broker_account_id)},
    )
    logger.warning(
        "Daily bootstrap: created today's trading_session %s for workspace %s "
        "(broker_account=%s, continued from trading_session %s)",
        new_session.id,
        workspace_id,
        new_session.broker_account_id,
        most_recent.id,
    )
    return new_session


def run_daily_bootstrap(*, session_factory: SessionFactory = session_scope) -> None:
    today_ist = now_ist().date()
    with session_factory() as db:
        # Every workspace, not just ones with existing trading_session
        # history -- a workspace with zero sessions ever (a human created
        # it and a broker account but never started a first session
        # manually) must still reach _bootstrap_workspace's own "nothing to
        # bootstrap from" skip-and-log branch, not be silently invisible to
        # this loop entirely.
        workspace_ids = {row[0] for row in db.query(Workspace.id).all()}
        for workspace_id in workspace_ids:
            todays_session = _bootstrap_workspace(db, workspace_id, today_ist)
            # Ops-Hardening Phase 6: creates committed StrategyRun rows only
            # (no thread starting here) -- _resume_strategy_runners below
            # picks them up, same as it would any other non-STOPPED run with
            # no live thread yet. `todays_session` can be `None` ("no prior
            # session ever" skip case) or non-ACTIVE (a human already ended
            # today's session, e.g. kill_switch/manual end, before a restart
            # re-ran this same-day bootstrap tick) -- both must be skipped,
            # not just the None case: _resume_strategy_runners only ever
            # resumes runs on an ACTIVE session, so attaching a fresh run to
            # an already-ended one would create a zombie StrategyRun no
            # runner thread ever picks up.
            if todays_session is not None and todays_session.status == TradingSessionStatus.ACTIVE:
                spawn_enabled_strategies(db, todays_session, today_ist)

    # Resuming strategy runners is idempotent and self-contained (queries
    # fresh state each call) -- safe to call unconditionally here even
    # though the common case (a freshly-created, empty session) finds
    # nothing to resume. Its real value is the "server restarted at 09:05"
    # scenario the original spec names -- though in that exact case, the
    # normal app.main lifespan startup sequence already calls this once on
    # its own before this scheduler's thread even starts, making this call
    # a harmless no-op rather than the only time it runs.
    #
    # Module-level import (see top of file), not a local one here -- this
    # function has no session_factory of its own (hardcoded to the real
    # session_scope internally), so a test must be able to monkeypatch this
    # module's own reference to it (`bootstrapper_module._resume_strategy_
    # runners`) rather than have it re-imported fresh from app.main on every
    # call, which a local import here would make impossible to intercept.
    _resume_strategy_runners()


class DailyBootstrapScheduler:
    def __init__(self, tick_seconds: float = 60.0) -> None:
        self._tick_seconds = tick_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_bootstrap_date: date | None = None

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._tick_seconds + 5)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_once(self) -> None:
        now = now_ist()
        if now.time() < BOOTSTRAP_TIME or self._last_bootstrap_date == now.date():
            return
        run_daily_bootstrap()
        self._last_bootstrap_date = now.date()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - a background loop must never die silently-crashed
                logger.exception("daily bootstrap cycle failed")
            self._stop_event.wait(self._tick_seconds)


_scheduler: DailyBootstrapScheduler | None = None


def ensure_daily_bootstrap_scheduler_running(tick_seconds: float = 60.0) -> DailyBootstrapScheduler:
    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = DailyBootstrapScheduler(tick_seconds=tick_seconds)
        _scheduler.start()
    return _scheduler


def stop_daily_bootstrap_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.stop()
        _scheduler = None
