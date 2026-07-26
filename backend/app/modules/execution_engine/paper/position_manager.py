"""`PositionManager`: a background-thread poller, same pattern as
`strategy_engine.runner.StrategyRunner` (daemon
thread, its own short-lived `session_scope()` per cycle, a `stop_event` for
clean shutdown). One instance per `trading_session`, started by
`api.v1.strategies.start_strategy` (mirroring exactly where
`StrategyRunner` itself gets started) and resumed by `app.main`'s
startup-recovery check after a restart — its job for the life of the
session is: check every open Position's stop/target/trail against the
current price, force a square-off once IST wall-clock passes
`cutoff_time`, and periodically run a polling reconciliation pass.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager

from sqlalchemy.orm import Session

from app.core.clock import now_ist
from app.core.db.session import session_scope
from app.domain.broker.models import ReconciliationTrigger
from app.domain.execution.models import Position, PositionStatus
from app.domain.market.models import Instrument, OptionContract
from app.domain.session.models import TradingSession, TradingSessionStatus
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.composition import get_broker
from app.modules.execution_engine.paper.service import evaluate_open_position
from app.modules.reconciliation.service import run_reconciliation

logger = logging.getLogger("app.execution_engine.paper.position_manager")

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
        self._broker = broker or get_broker()
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
                tick = self._broker.get_quote(option_contract.symbol)

                underlying_price = underlying_price_cache.get(option_contract.instrument_id)
                if underlying_price is None:
                    instrument = db.get(Instrument, option_contract.instrument_id)
                    if instrument is not None:
                        underlying_price = self._broker.get_quote(instrument.symbol).ltp
                        underlying_price_cache[option_contract.instrument_id] = underlying_price

                evaluate_open_position(
                    db,
                    trading_session,
                    position,
                    tick.ltp,
                    broker=self._broker,
                    bid=tick.bid,
                    ask=tick.ask,
                    underlying_price=underlying_price,
                )

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

                run_eod_square_off(db, self._broker, trading_session)

            self._cycle_count += 1
            if self._cycle_count % self._reconcile_every_n_cycles == 0:
                run_reconciliation(
                    db, self._broker, trading_session, ReconciliationTrigger.POLL
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
