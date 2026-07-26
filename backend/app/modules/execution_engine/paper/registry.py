"""Process-wide registry of running `PositionManager` threads, one per
`trading_session_id` — mirrors `api.v1.strategies._RUNNERS`'s reasoning
exactly (a plain in-process dict is safe only because
`LOCK_PROCESS_SINGLETON` already guarantees this backend runs as a single
process).

Deliberately not called from `execution_engine.paper.service.dispatch_trade_intent`
itself: that function is called directly (with an explicit test-owned
`broker=`) from unit/integration tests exercising dispatch in isolation, and
auto-starting a real background thread there would spawn one per test run,
each polling the *production* DB via `PositionManager`'s default
`session_scope` — the exact "background thread silently queries the wrong
database" trap `strategy_engine.strategies.synthetic.SyntheticStrategyRunner`
already had to design around. Starting a manager is instead the caller's
explicit responsibility, at the same layer `SyntheticStrategyRunner` is
started from (the strategy-start API flow) — plus `app.main`'s
startup-recovery check, which calls this for any session found with open
positions after a restart, which is what actually resumes stop/trail
management instead of coming back up idle.
"""

from __future__ import annotations

import uuid

from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.execution_engine.paper.position_manager import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_RECONCILE_EVERY_N_CYCLES,
    PositionManager,
)

_managers: dict[uuid.UUID, PositionManager] = {}


def ensure_position_manager_running(
    trading_session_id: uuid.UUID,
    broker: BrokerPort | None = None,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    reconcile_every_n_cycles: int = DEFAULT_RECONCILE_EVERY_N_CYCLES,
) -> PositionManager:
    existing = _managers.get(trading_session_id)
    if existing is not None and existing.is_alive():
        return existing

    manager = PositionManager(
        trading_session_id,
        broker=broker,
        poll_interval_seconds=poll_interval_seconds,
        reconcile_every_n_cycles=reconcile_every_n_cycles,
    )
    manager.start()
    _managers[trading_session_id] = manager
    return manager


def stop_position_manager(trading_session_id: uuid.UUID) -> None:
    manager = _managers.pop(trading_session_id, None)
    if manager is not None:
        manager.stop()


def stop_all() -> None:
    for trading_session_id in list(_managers):
        stop_position_manager(trading_session_id)


def get_running_position_manager(trading_session_id: uuid.UUID) -> PositionManager | None:
    return _managers.get(trading_session_id)
