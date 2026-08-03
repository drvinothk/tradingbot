"""`PositionManager`: a background-thread poller, same pattern as
`strategy_engine.runner.StrategyRunner` (daemon
thread, its own short-lived `session_scope()` per cycle, a `stop_event` for
clean shutdown). One instance per `trading_session`, started by
`api.v1.strategies.start_strategy` (mirroring exactly where
`StrategyRunner` itself gets started) and resumed by `app.main`'s
startup-recovery check after a restart — its job for the life of the
session is: check every open Position's stop/target/trail against the
current price, force a square-off once IST wall-clock passes
`cutoff_time`, watch for a margin breach on guarded-live/live sessions
(Addendum hardening batch's narrow emergency-square-off trigger), and
periodically run a polling reconciliation pass.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.clock import now_ist
from app.core.db.session import session_scope
from app.core.modes.state_machine import ModeTransitionError, transition_mode
from app.domain.audit.models import ActorType, EventCategory
from app.domain.broker.models import ReconciliationTrigger
from app.domain.execution.models import Position, PositionStatus
from app.domain.market.models import Instrument, OptionContract
from app.domain.ops.models import AlertSeverity, SystemAlert
from app.domain.session.models import (
    SafeMode,
    TradingSession,
    TradingSessionStatus,
    TransitionTriggerType,
)
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.base.errors import BrokerAuthError, BrokerError
from app.modules.broker_adapter.composition import get_execution_broker
from app.modules.execution_engine.paper.service import evaluate_open_position
from app.modules.reconciliation.service import run_reconciliation

logger = logging.getLogger("app.execution_engine.paper.position_manager")

_MARGIN_BREACH_MODES = (SafeMode.PAPER_PLUS_GUARDED_LIVE, SafeMode.LIVE_ENABLED)

SessionFactory = Callable[[], AbstractContextManager[Session]]

DEFAULT_POLL_INTERVAL_SECONDS = 3.0
DEFAULT_RECONCILE_EVERY_N_CYCLES = 5


class PositionManager:
    def __init__(
        self,
        trading_session_id: uuid.UUID,
        broker: BrokerPort | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        reconcile_every_n_cycles: int = DEFAULT_RECONCILE_EVERY_N_CYCLES,
        session_factory: SessionFactory = session_scope,
    ) -> None:
        self.trading_session_id = trading_session_id
        # Not resolved eagerly: __init__ only has a trading_session_id, not
        # the TradingSession row get_execution_broker needs, and this class
        # is often constructed before a DB session is open (registry.py).
        # None means "resolve get_execution_broker(trading_session) fresh
        # every cycle in _run_cycle"; an explicit broker (tests, always)
        # is used as-is, unchanged from before.
        self._broker_override = broker
        self._poll_interval_seconds = poll_interval_seconds
        self._reconcile_every_n_cycles = reconcile_every_n_cycles
        self._session_factory = session_factory
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._cycle_count = 0

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval_seconds + 5)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_once(self) -> None:
        """One poll cycle, exposed separately from `_loop` so tests can
        drive it deterministically rather than sleeping on the background
        thread — same reasoning `SyntheticStrategy.run_cycle` is tested
        directly, independent of `StrategyRunner`'s timer.
        """
        with self._session_factory() as db:
            trading_session = db.get(TradingSession, self.trading_session_id)
            if trading_session is None or trading_session.status != TradingSessionStatus.ACTIVE:
                return

            try:
                self._run_cycle(db, trading_session)
            except BrokerAuthError as exc:
                self._handle_broker_auth_error(db, trading_session, exc)

    def _handle_broker_auth_error(
        self, db: Session, trading_session: TradingSession, exc: BrokerAuthError
    ) -> None:
        """Broker-agnostic reaction to Phase 5's "invalid credentials / IP
        mismatch / mid-session expiry" scenarios — catches the generic
        `BrokerAuthError` (see `broker_adapter/base/errors.py`'s own
        docstring for why this module never imports anything
        Shoonya-specific).

        Only actually moves the session to `degraded_mode` when the
        transitions table (`core/modes/transitions.py`) has a legal
        `SYSTEM`-triggered edge there from the current mode — which is
        `paper_plus_guarded_live`/`live_enabled` only. `degraded_mode`
        exists to protect *live* money; a `paper_only` session (all of
        Phase 5's real traffic, since it's still no-live-orders) has
        nothing for it to protect, so the state machine deliberately has
        no edge there at all. Logging is the entire response in that case
        — same "market data hiccup, not a safety event" reasoning this
        system already applies to a `MockBrokerAdapter`-only run.
        """
        logger.warning(
            "broker auth failure for session %s: %s", self.trading_session_id, exc
        )
        from_mode = SafeMode(trading_session.mode)
        if from_mode not in (SafeMode.PAPER_PLUS_GUARDED_LIVE, SafeMode.LIVE_ENABLED):
            return
        try:
            transition_mode(
                db,
                trading_session,
                SafeMode.DEGRADED_MODE,
                TransitionTriggerType.SYSTEM,
                reason=f"broker auth failure: {exc}"[:500],
            )
            db.commit()
        except ModeTransitionError:
            logger.exception(
                "could not move session %s to degraded_mode after broker auth failure",
                self.trading_session_id,
            )

    def _check_margin_breach(
        self, db: Session, trading_session: TradingSession, broker: BrokerPort
    ) -> None:
        """Addendum hardening batch's one narrow automatic emergency-square-
        off trigger (build-plan.md: "a detected margin breach on a live
        position — not on connectivity loss, reconciliation lag, or any
        other transient condition"). Only called for
        `paper_plus_guarded_live`/`live_enabled` sessions with open
        positions (see `_run_cycle`'s guard) — kill-switch is deliberately
        untouched by this path; a manual "exit all" endpoint and the
        existing EOD square-off cover the other two legs of that same
        Addendum decision.
        """
        try:
            margin = broker.get_margin()
        except BrokerError:
            logger.exception(
                "get_margin failed during margin-breach check for session %s",
                self.trading_session_id,
            )
            return

        if margin.available_margin >= 0:
            return

        logger.warning(
            "margin breach detected for session %s: available_margin=%.2f",
            self.trading_session_id,
            margin.available_margin,
        )

        # Local import: same load-time-cycle reasoning as
        # run_eod_square_off's import in _run_cycle below.
        from app.modules.scheduler.eod_square_off import run_margin_breach_square_off

        run_margin_breach_square_off(db, broker, trading_session)

        reason = f"margin breach: available_margin={margin.available_margin:.2f}"[:500]
        db.add(
            SystemAlert(
                id=uuid.uuid4(),
                workspace_id=trading_session.workspace_id,
                trading_session_id=trading_session.id,
                severity=AlertSeverity.CRITICAL,
                category="margin_breach_square_off",
                message=reason,
                created_at=datetime.now(UTC),
            )
        )
        record_event(
            db,
            workspace_id=trading_session.workspace_id,
            actor_type=ActorType.SYSTEM,
            event_category=EventCategory.SYSTEM_HEALTH,
            event_type="margin_breach_square_off",
            entity_type="trading_session",
            entity_id=trading_session.id,
            trading_session_id=trading_session.id,
            payload={"available_margin": margin.available_margin},
        )
        db.commit()

    def _run_cycle(self, db: Session, trading_session: TradingSession) -> None:
        broker = self._broker_override or get_execution_broker(trading_session)
        open_positions = (
            db.query(Position)
            .filter(
                Position.trading_session_id == trading_session.id,
                Position.status == PositionStatus.OPEN,
            )
            .all()
        )
        # Cache underlying quotes within this cycle — multiple open
        # positions (Phase 4: several concurrent strategy runs) commonly
        # share the same underlying instrument, and this only ever needs
        # its current price once per cycle regardless of how many
        # positions reference it.
        underlying_price_cache: dict[uuid.UUID, float] = {}
        for position in open_positions:
            option_contract = db.get(OptionContract, position.option_contract_id)
            if option_contract is None:
                continue
            tick = broker.get_quote(option_contract.symbol)

            underlying_price = underlying_price_cache.get(option_contract.instrument_id)
            if underlying_price is None:
                instrument = db.get(Instrument, option_contract.instrument_id)
                if instrument is not None:
                    underlying_price = broker.get_quote(instrument.symbol).ltp
                    underlying_price_cache[option_contract.instrument_id] = underlying_price

            evaluate_open_position(
                db,
                trading_session,
                position,
                tick.ltp,
                broker=broker,
                bid=tick.bid,
                ask=tick.ask,
                underlying_price=underlying_price,
            )

        if open_positions and SafeMode(trading_session.mode) in _MARGIN_BREACH_MODES:
            self._check_margin_breach(db, trading_session, broker)

        # Local import: same load-time-cycle reasoning as
        # run_eod_square_off's import just below —
        # strategy_engine.service imports execution_engine.paper.service,
        # so importing it at module scope here would cycle back through
        # this package's own __init__.py.
        from app.modules.strategy_engine.service import expire_stale_pending_approvals

        expire_stale_pending_approvals(db, trading_session)

        if now_ist().time() >= trading_session.cutoff_time:
            # Local import: app.modules.scheduler's package __init__
            # eagerly re-exports eod_square_off, which itself imports
            # execution_engine.paper.service — importing it at module
            # scope here would form a load-time cycle through this
            # package's own __init__.py. Same one-directional-dependency
            # reasoning as close_position's local import of
            # risk_engine.service.
            from app.modules.scheduler.eod_square_off import run_eod_square_off

            run_eod_square_off(db, broker, trading_session)

        self._cycle_count += 1
        if self._cycle_count % self._reconcile_every_n_cycles == 0:
            run_reconciliation(
                db, broker, trading_session, ReconciliationTrigger.POLL
            )

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - a background loop must never die silently-crashed
                logger.exception(
                    "position manager cycle failed for session %s", self.trading_session_id
                )
            self._stop_event.wait(self._poll_interval_seconds)
