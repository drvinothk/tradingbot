"""Generalized strategy run-cycle + background-thread runner. Phase 2's
`SyntheticStrategyRunner` (removed) hardcoded this loop to one `Strategy`
subclass; Phase 4 needs the identical evaluate -> submit_signal -> refresh-
status cycle shared by all four strategies (synthetic, ORB, VWAP Pullback,
EMA Micro-pullback) — implemented once here instead of duplicated per
strategy, same "common rules" principle as `strategy_engine.common_rules`.

`StrategyRunner` mirrors `execution_engine.paper.position_manager
.PositionManager`'s shape exactly: a daemon thread, its own short-lived
`session_scope()` per cycle (never shared across threads), a `stop_event`
for clean shutdown. `run_cycle` is exposed standalone, not just via
`StrategyRunner._loop`, so tests can drive it deterministically without a
real background thread — same reasoning `PositionManager.run_once` is.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from datetime import date

from sqlalchemy.orm import Session

from app.core.clock import is_within_global_trading_window, now_ist
from app.core.db.session import session_scope
from app.domain.risk.models import RiskDecision
from app.domain.session.models import TradingSession
from app.domain.strategy.models import StrategyConfig, StrategyRun, StrategyRunStatus
from app.modules.broker_adapter.composition import get_broker
from app.modules.market_data.freshness import FreshnessState, ensure_fresh_option_chain
from app.modules.strategy_engine.common_rules import (
    get_open_position_for_run,
    get_recent_completed_bars,
)
from app.modules.strategy_engine.interface import Strategy
from app.modules.strategy_engine.service import submit_signal

logger = logging.getLogger("app.strategy_engine.runner")

SessionFactory = Callable[[], AbstractContextManager[Session]]


def run_cycle(
    db: Session,
    strategy: Strategy,
    strategy_run: StrategyRun,
    trading_session: TradingSession,
    strategy_config: StrategyConfig,
) -> RiskDecision | None:
    """One `evaluate` -> `submit_signal` cycle, plus a ground-truth refresh
    of `strategy_run.status` (IN_POSITION vs SCANNING) computed from whether
    this run actually has an open Position right now — correct regardless of
    which path a trade took this cycle (auto-dispatch, approval-required,
    risk-rejected, or no signal at all), unlike trying to track the
    transition manually. Never touches a `STOPPED` run's status.

    Before `evaluate()`, ensures the option-chain snapshot this strategy
    ranks against isn't stale (`market_data.freshness.ensure_fresh_option_
    chain` refreshes it once if needed — the actual fix for the "only ever
    snapshotted once, at start_strategy time" gap). `evaluate()` is skipped
    entirely (same as a normal "no signal" cycle) if data is still STALE/DEAD
    after that refresh attempt — a broker hiccup should mean "wait for next
    cycle," never "trade off data we can't vouch for."

    The freshness-check + `evaluate()` + `submit_signal()` block only runs
    inside `is_within_global_trading_window()` (09:31-15:09 IST) — outside
    it, this is a normal "no signal" cycle too, but the status-refresh below
    still always runs so an already-open position's status stays
    ground-truth.

    The window gate keys off the latest completed bar's own `bucket_start`,
    not wall-clock `now()` — fetched once, here, and threaded through to
    `evaluate()` so a bar-consuming strategy doesn't re-query the identical
    row a moment later. Falls back to wall-clock `now_ist()` when no bar
    exists yet for this instrument (a strategy that doesn't consume bars,
    e.g. `SyntheticStrategy`, or an instrument before its first bar
    completes) — that fallback also doubles as this function's empty-bar
    guard, so a cold-start cycle degrades to the old wall-clock behavior
    instead of ever touching `latest_bar.bucket_start` on a `None`.
    """
    decision: RiskDecision | None = None

    @contextmanager
    def _same_session():
        # Reuses run_cycle's own already-open db/transaction for any
        # option-chain refresh, rather than record_option_chain_snapshot's
        # own default of opening a second, independently-committing
        # connection — keeps the refresh atomic with the rest of this cycle.
        yield db

    latest_bars = get_recent_completed_bars(db, strategy.instrument_id, limit=1)
    if latest_bars:
        latest_bar = latest_bars[0]
        window_ts = latest_bar.bucket_start
    else:
        latest_bar = None
        window_ts = now_ist()

    if is_within_global_trading_window(window_ts):
        freshness = ensure_fresh_option_chain(
            db,
            get_broker(),
            strategy.instrument_id,
            strategy.expiry_date,
            session_factory=_same_session,
        )
        if freshness in (FreshnessState.STALE, FreshnessState.DEAD):
            logger.warning(
                "run %s: option-chain data %s for instrument %s expiry %s — skipping cycle",
                strategy_run.id,
                freshness.value,
                strategy.instrument_id,
                strategy.expiry_date,
            )
            proposal = None
        else:
            proposal = strategy.evaluate(db, strategy_run, latest_bar)

        if proposal is not None:
            decision = submit_signal(db, strategy_run, trading_session, strategy_config, proposal)

    if strategy_run.status != StrategyRunStatus.STOPPED:
        has_position = get_open_position_for_run(db, strategy_run) is not None
        new_status = StrategyRunStatus.IN_POSITION if has_position else StrategyRunStatus.SCANNING
        if strategy_run.status != new_status:
            strategy_run.status = new_status
            db.add(strategy_run)
            db.flush()

    return decision


class StrategyRunner:
    def __init__(
        self,
        strategy: Strategy,
        strategy_run_id: uuid.UUID,
        interval_seconds: float = 30.0,
        session_factory: SessionFactory = session_scope,
    ) -> None:
        self._strategy = strategy
        self._strategy_run_id = strategy_run_id
        self._interval_seconds = interval_seconds
        self._session_factory = session_factory
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def instrument_id(self) -> uuid.UUID:
        return self._strategy.instrument_id

    @property
    def expiry_date(self) -> date:
        return self._strategy.expiry_date

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_seconds + 5)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                with self._session_factory() as db:
                    strategy_run = db.get(StrategyRun, self._strategy_run_id)
                    if strategy_run is None or strategy_run.status == StrategyRunStatus.STOPPED:
                        return
                    trading_session = db.get(TradingSession, strategy_run.trading_session_id)
                    strategy_config = db.get(StrategyConfig, strategy_run.strategy_config_id)
                    if trading_session is None or strategy_config is None:
                        logger.warning(
                            "strategy_run %s references a missing session/config — stopping",
                            self._strategy_run_id,
                        )
                        return
                    run_cycle(db, self._strategy, strategy_run, trading_session, strategy_config)
            except Exception:  # noqa: BLE001 - a background loop must never die silently-crashed
                logger.exception(
                    "strategy cycle failed for run %s", self._strategy_run_id
                )
            self._stop_event.wait(self._interval_seconds)
