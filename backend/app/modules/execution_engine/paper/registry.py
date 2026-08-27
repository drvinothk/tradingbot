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
database" trap `strategy_engine.runner.StrategyRunner`
already had to design around. Starting a manager is instead the caller's
explicit responsibility, at the same layer `StrategyRunner` is
started from (the strategy-start API flow) — plus `app.main`'s
startup-recovery check, which calls this for any session found with open
positions after a restart, which is what actually resumes stop/trail
management instead of coming back up idle.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.execution_engine.paper.position_manager import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_RECONCILE_EVERY_N_CYCLES,
    PositionManager,
)

logger = logging.getLogger("app.execution_engine.paper.registry")

_managers: dict[uuid.UUID, PositionManager] = {}


def rebuild_execution_mock_position_book(db: Session) -> int:
    """Reconstruct the persistent execution mock's in-memory position book
    from the durable `positions` table — run once on startup, before any
    reconciliation pass, so a restart can't leave the mock net short by an
    opening fill it lost while the DB kept it (see
    `MockBrokerAdapter.seed_position`'s docstring for the full failure
    mode). Nets every OPEN position whose *opening* order was `PAPER`, per
    contract symbol — the same grouping `reconciliation.service
    ._local_net_qty_by_symbol` uses, so the two agree by construction.
    Returns the number of contracts seeded.
    """
    from app.domain.execution.models import Order, OrderMode, OrderSide, Position, PositionStatus
    from app.domain.market.models import OptionContract
    from app.modules.broker_adapter.composition import get_execution_mock

    net_qty: dict[str, int] = {}
    last_price: dict[str, float] = {}
    positions = (
        db.query(Position)
        .join(Order, Order.id == Position.opening_order_id)
        .filter(
            Position.status == PositionStatus.OPEN,
            Order.mode == OrderMode.PAPER,
        )
        .all()
    )
    for position in positions:
        option_contract = db.get(OptionContract, position.option_contract_id)
        if option_contract is None:
            continue
        signed_qty = position.qty if position.side == OrderSide.BUY else -position.qty
        net_qty[option_contract.symbol] = net_qty.get(option_contract.symbol, 0) + signed_qty
        last_price[option_contract.symbol] = float(position.entry_price)

    mock = get_execution_mock()
    for symbol, qty in net_qty.items():
        mock.seed_position(symbol, qty, last_price.get(symbol, 0.0))

    if net_qty:
        logger.info(
            "Rebuilt execution mock position book from DB: %d contract(s) seeded (%s)",
            len(net_qty),
            {s: q for s, q in net_qty.items()},
        )
    return len(net_qty)


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
