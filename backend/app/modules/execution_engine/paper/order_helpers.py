"""Pure, side-effect-free helpers shared across `execution_engine.paper` —
factored out of `service.py` 2026-08-26 so `protective_stop.py` (the resting
protective-stop sub-feature) can depend on them too without creating a
circular import back into `service.py` itself. No DB session, no broker
call, no logging here by design — every function is a plain value
transform, safe to import from any direction.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from app.domain.execution.models import (
    ExitReason,
    Order,
    OrderEvent,
    OrderMode,
    OrderSide,
    OrderStatus,
    OrderType,
)
from app.domain.market.models import OptionContract
from app.domain.session.models import TradingSession
from app.domain.strategy.models import SignalSide
from app.modules.broker_adapter.base.contracts import BrokerOrderStatus, OrderResult
from app.modules.broker_adapter.base.contracts import OrderSide as BrokerOrderSide


def _dec(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _opposite(side: SignalSide) -> SignalSide:
    return SignalSide.SELL if side == SignalSide.BUY else SignalSide.BUY


def _to_broker_side(side: SignalSide) -> BrokerOrderSide:
    return BrokerOrderSide.BUY if side == SignalSide.BUY else BrokerOrderSide.SELL


def _to_domain_side(side: SignalSide) -> OrderSide:
    return OrderSide.BUY if side == SignalSide.BUY else OrderSide.SELL


def _map_status(status: BrokerOrderStatus) -> OrderStatus:
    return OrderStatus(status.value)


def _new_order(
    trading_session: TradingSession,
    option_contract: OptionContract,
    order_result: OrderResult,
    *,
    mode: OrderMode,
    side: OrderSide,
    order_type: OrderType,
    qty: int,
    idempotency_key: str,
    now: datetime,
    trade_intent_id: uuid.UUID | None = None,
    position_id: uuid.UUID | None = None,
    intended_exit_reason: ExitReason | None = None,
) -> Order:
    """Shared field set every real `Order` row in this module writes — the
    entry order (`dispatch_trade_intent`), the resting protective stop
    (`protective_stop.place_protective_stop`), and the exit order
    (`close_position`) differ only in which FK is set (`trade_intent_id` vs
    `position_id`), and their own `mode`/`side`/`order_type`/`qty`/
    `idempotency_key`/`intended_exit_reason` — previously each built this
    same ~15-field constructor call independently.
    """
    return Order(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=trade_intent_id,
        position_id=position_id,
        idempotency_key=idempotency_key,
        mode=mode,
        side=side,
        order_type=order_type,
        qty=qty,
        status=_map_status(order_result.status),
        filled_qty=order_result.filled_qty,
        avg_fill_price=order_result.avg_fill_price,
        broker_order_id=order_result.broker_order_id,
        intended_exit_reason=intended_exit_reason,
        submitted_at=now,
        updated_at=now,
    )


def _new_order_event(
    order_id: uuid.UUID,
    order_result: OrderResult,
    *,
    event_type: str,
    now: datetime,
    include_raw_message: bool = False,
    source: str | None = None,
) -> OrderEvent:
    """Shared `raw_payload` core (broker_order_id/status/filled_qty/
    avg_fill_price) every `OrderEvent` row in this module writes — the two
    reconciliation call sites additionally tag `source` (which function
    resolved this), and only the very first fill event includes
    `raw_message` (the broker's own rejection/ack text, useful right at
    dispatch, redundant on every later resolution of the same order).
    """
    raw_payload: dict[str, object] = {
        "broker_order_id": order_result.broker_order_id,
        "status": order_result.status.value,
        "filled_qty": order_result.filled_qty,
        "avg_fill_price": order_result.avg_fill_price,
    }
    if include_raw_message:
        raw_payload["raw_message"] = order_result.raw_message
    if source is not None:
        raw_payload["source"] = source
    return OrderEvent(
        id=uuid.uuid4(), order_id=order_id, event_type=event_type, raw_payload=raw_payload, ts=now
    )


def _apply_slippage(price: Decimal, order_side: SignalSide, slippage_pct: Decimal) -> Decimal:
    """Worse execution than the reference price, in whichever direction
    actually hurts the trader for *this order's own side* -- a BUY (opening
    a long, or closing a short) fills slightly higher; a SELL (closing a
    long, or opening a short) fills slightly lower. The same rule works
    uniformly for both an entry order and an exit order without needing to
    know which one this is -- only the order's own side matters.
    """
    if slippage_pct == 0:
        return price
    if order_side == SignalSide.BUY:
        return price * (Decimal("1") + slippage_pct)
    return price * (Decimal("1") - slippage_pct)


def _round_to_tick(price: Decimal, tick_size: Decimal, order_side: SignalSide) -> Decimal:
    """**2026-08-20, live incident**: a real Shoonya order was rejected for
    a price that wasn't a multiple of the instrument's own tick size (NSE
    requires 0.05 multiples for these contracts; anything else, e.g. a
    stray 0.03, is rejected outright) -- `_apply_slippage`'s percentage-
    based buffer (`AppSettings.live_limit_order_buffer_pct`) almost never
    lands on a clean multiple on its own (a real quoted `entry_price` is
    already tick-aligned, but multiplying it by `1 + buffer_pct` generally
    isn't). Only ever actually mattered for LIVE limit orders -- PAPER
    still sends MARKET, which has no price to round.

    Rounds in the direction that preserves (or very slightly increases)
    the buffer's own protective margin rather than eroding it, same "which
    direction actually helps this order's own side" reasoning
    `_apply_slippage` already uses: up for a BUY (rounding down would mean
    paying *less* than the buffer intended, undermining the whole point of
    padding the price to tolerate LTP movement since the trading decision
    was made), down for a SELL (mirror reasoning).
    """
    if tick_size <= 0:
        return price
    ticks = price / tick_size
    rounding = ROUND_CEILING if order_side == SignalSide.BUY else ROUND_FLOOR
    return ticks.to_integral_value(rounding=rounding) * tick_size
