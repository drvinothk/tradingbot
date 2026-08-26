"""The resting-protective-stop sub-feature — factored out of the 2,000+-line
`service.py` 2026-08-26 (pure move, no logic change) so this ~430-line,
safety-critical "Hard SL with Local Target" mechanism has its own file.
Three entry points, each called from a different `service.py` function:
`place_protective_stop` (from `_open_position_from_fill`, on entry fill),
`cancel_resting_protective_stop` (from `close_position`, before any
non-STOP exit), and `sync_resting_protective_stop` (from
`evaluate_open_position`, the TSL tightening step).

**Why this still imports `_finalize_position_close`/`resolve_broker_for_
position` from `service.py` inside the functions below, not at module
level**: those two are genuinely shared with `service.py`'s own
non-protective-stop code (`close_position`, the two reconciliation
functions) and stay there — but `place_protective_stop`/
`sync_resting_protective_stop` also need to call them, and `service.py`
itself needs to call every function in this file. A module-level import in
either direction would be circular; `order_helpers.py` (the other
extraction from the same pass) has zero such back-reference and is safe to
import from both modules at module level instead.
"""

from __future__ import annotations

import enum
import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.db.base import utcnow as _utcnow
from app.domain.execution.models import (
    ExitReason,
    Order,
    OrderMode,
    OrderStatus,
    OrderType,
    Position,
    StopPlan,
)
from app.domain.market.models import Instrument, OptionContract
from app.domain.ops.models import AlertSeverity
from app.domain.session.models import TradingSession
from app.domain.strategy.models import SignalSide
from app.modules.alerting.manager import send_alert
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.base.contracts import BrokerOrderStatus, OrderRequest
from app.modules.broker_adapter.base.contracts import OrderType as BrokerOrderType
from app.modules.execution_engine.paper.order_helpers import (
    _apply_slippage,
    _dec,
    _new_order,
    _new_order_event,
    _opposite,
    _round_to_tick,
    _to_broker_side,
    _to_domain_side,
)

logger = logging.getLogger("app.execution_engine.paper.service")


def place_protective_stop(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    stop_plan: StopPlan,
    option_contract: OptionContract,
    broker: BrokerPort,
) -> None:
    """LIVE-only crash-resilience layer: places a real broker-side SL-LMT
    immediately on entry fill, tagged `stop:{position_id}` (distinct from
    the existing `exit:{position_id}` convention `close_position` uses) so
    `reconcile_pending_live_exit_orders`/`_apply_resolved_pending_exit_order`
    can tell "the resting stop itself filled" apart from "a manual/target
    exit filled" — see that function's own branch. `SL-MKT` is exchange-
    banned for options since 2021-09-27 (confirmed, see project memory),
    hence `SL_LIMIT`, never `SL_MARKET`.

    Never raises — a placement failure must leave the position exactly as
    protected as it was before this feature existed (today's pure local
    stop/target/trail monitoring), not worse. On failure: log, raise a
    CRITICAL `SystemAlert`, and return with `stop_plan.resting_order_id`
    left `None` — `evaluate_open_position`'s own stop check only skips
    itself when `resting_order_id` is set, so a `None` here means local
    monitoring keeps working exactly as it does for every position today.

    Deliberately does **not** call `run_preflight_checks` (unlike every
    other LIVE order this module places) — that gate exists to block a
    *new risk-taking* dispatch on stale option-chain data or thin margin,
    but a protective stop *reduces* risk. Gating it the same way would be
    actively counterproductive: margin is often at its tightest right after
    the entry that just consumed it, which is exactly when this must not
    be skipped.
    """
    instrument = db.get(Instrument, option_contract.instrument_id)
    if instrument is None:
        return

    exit_side = _opposite(SignalSide(position.side))
    tick_size = _dec(instrument.tick_size)
    trigger_price = _round_to_tick(_dec(stop_plan.stop_price), tick_size, exit_side)
    # Same buffer/tick discipline as every other LIVE limit-priced exit in
    # this module (see `_apply_slippage`/`_round_to_tick`'s own docstrings)
    # -- the limit price trails the trigger by the protective buffer so the
    # order can actually execute once triggered, not sit rejected-on-fill
    # for being priced exactly at a level the market has already passed.
    buffer_pct = _dec(get_settings().app.live_limit_order_buffer_pct)
    limit_price = _round_to_tick(
        _apply_slippage(trigger_price, exit_side, buffer_pct), tick_size, exit_side
    )
    stop_idempotency_key = f"stop:{position.id}"

    try:
        order_result = broker.place_order(
            OrderRequest(
                idempotency_key=stop_idempotency_key,
                contract_symbol=option_contract.symbol,
                side=_to_broker_side(exit_side),
                order_type=BrokerOrderType.SL_LIMIT,
                qty=position.qty,
                limit_price=float(limit_price),
                trigger_price=float(trigger_price),
                lot_size=instrument.lot_size,
                tag=f"session:{trading_session.id}",
            )
        )
    except Exception:  # noqa: BLE001 - see this function's own "never raises" contract
        # Broader than `BrokerError` deliberately: this function's own
        # docstring promises to never leave the position worse off than
        # before this feature existed, and `_open_position_from_fill`
        # (this function's only caller) has nothing wrapping it either --
        # an uncaught exception here would abort the entire entry-fill
        # transaction for a position the broker has *already genuinely
        # filled*. `place_order`'s own 1-lot `CriticalSafetyException` is a real,
        # concrete example of a non-`BrokerError` this must still catch.
        logger.exception(
            "protective SL-LMT placement failed for position %s -- falling back to "
            "local-only stop/target/trail monitoring for this position",
            position.id,
        )
        send_alert(
            db,
            workspace_id=trading_session.workspace_id,
            trading_session_id=trading_session.id,
            severity=AlertSeverity.CRITICAL,
            category="protective_stop_placement_failed",
            message=(
                f"Protective SL-LMT placement failed for position {position.id}; "
                "using local-only monitoring."
            ),
            mode=OrderMode.LIVE,
            dedup_key=f"protective_stop_placement_failed:{position.id}",
        )
        return

    now = _utcnow()
    stop_order = _new_order(
        trading_session,
        option_contract,
        order_result,
        mode=OrderMode.LIVE,
        side=_to_domain_side(exit_side),
        order_type=OrderType.SL_LIMIT,
        qty=position.qty,
        idempotency_key=stop_idempotency_key,
        now=now,
        position_id=position.id,
    )
    db.add(stop_order)
    stop_plan.resting_order_id = order_result.broker_order_id
    stop_plan.resting_order_price = float(trigger_price)
    stop_plan.updated_at = now
    db.add(stop_plan)
    db.flush()

    db.add(
        _new_order_event(
            stop_order.id,
            order_result,
            event_type="filled" if stop_order.status == OrderStatus.FILLED else "submitted",
            now=now,
        )
    )

    # Defensive only -- a real resting stop shouldn't fill synchronously at
    # placement (its trigger is on the wrong side of the current price by
    # construction), but every other order in this system already handles
    # this synchronous/asynchronous duality, so this does too rather than
    # leaving a FILLED order dangling with resting_order_id still set.
    if stop_order.status == OrderStatus.FILLED and stop_order.avg_fill_price is not None:
        from app.modules.execution_engine.paper.service import _finalize_position_close

        _finalize_position_close(
            db, trading_session, position, stop_order, ExitReason.STOP, OrderMode.LIVE, None
        )
        db.flush()


class CancelOutcome(enum.Enum):
    """Result of `cancel_resting_protective_stop` — deliberately not
    exposed anywhere beyond `close_position`'s own use of it; this is
    call-site plumbing, not a domain concept."""

    CANCELLED = "cancelled"
    ALREADY_FILLED = "already_filled"
    FAILED = "failed"


def cancel_resting_protective_stop(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    stop_plan: StopPlan,
    resting_order_id: str,
    broker: BrokerPort,
) -> CancelOutcome:
    """Cancels this position's resting protective SL-LMT before
    `close_position` proceeds with any other exit reason — see that
    function's own comment for the safety invariant this exists to
    protect. Never raises; every outcome (including a genuine failure) is
    reported back as a `CancelOutcome` so the caller can decide what's
    safe to do next rather than this helper guessing. `resting_order_id`
    is passed explicitly (not re-read from `stop_plan`) so a caller that
    already narrowed it non-`None` doesn't lose that at this call boundary.
    """
    now = _utcnow()
    try:
        result = broker.cancel_order(resting_order_id)
    except Exception:  # noqa: BLE001 - see place_protective_stop's identical reasoning
        # Broader than `BrokerError` deliberately -- `close_position` has
        # nothing wrapping this call either, and an uncaught exception here
        # would abort whatever closed this position for (target/EOD/
        # manual/margin-breach), same "never worse off, never crash the
        # caller" contract every helper in this feature makes.
        logger.exception(
            "failed to cancel resting protective stop %s for position %s -- "
            "not proceeding with a new exit order until this is resolved",
            resting_order_id,
            position.id,
        )
        send_alert(
            db,
            workspace_id=trading_session.workspace_id,
            trading_session_id=trading_session.id,
            severity=AlertSeverity.CRITICAL,
            category="protective_stop_cancel_failed",
            message=(
                f"Failed to cancel resting protective stop for position "
                f"{position.id}; exit deferred."
            ),
            mode=OrderMode.LIVE,
            dedup_key=f"protective_stop_cancel_failed:{position.id}",
        )
        return CancelOutcome.FAILED

    if result.status == BrokerOrderStatus.FILLED:
        # The stop fired before our cancel reached the broker -- record its
        # real fill on the stop order's own row; the caller finalizes the
        # position as STOP from there.
        stop_order = (
            db.query(Order).filter(Order.idempotency_key == f"stop:{position.id}").one_or_none()
        )
        if stop_order is not None:
            stop_order.status = OrderStatus.FILLED
            stop_order.filled_qty = result.filled_qty
            stop_order.avg_fill_price = result.avg_fill_price
            stop_order.updated_at = now
            db.add(stop_order)
        stop_plan.resting_order_id = None
        stop_plan.resting_order_price = None
        stop_plan.updated_at = now
        db.add(stop_plan)
        db.flush()
        return CancelOutcome.ALREADY_FILLED

    if result.status == BrokerOrderStatus.CANCELLED:
        stop_plan.resting_order_id = None
        stop_plan.resting_order_price = None
        stop_plan.updated_at = now
        db.add(stop_plan)
        db.flush()
        return CancelOutcome.CANCELLED

    # Any other status (still pending-cancel, an unexpected rejection of
    # the cancel itself, etc.) is ambiguous -- same conservative treatment
    # as the BrokerError case above, never guess.
    logger.error(
        "cancel_order for resting protective stop %s (position %s) returned "
        "unexpected status %s -- not proceeding with a new exit order",
        resting_order_id,
        position.id,
        result.status,
    )
    send_alert(
        db,
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        severity=AlertSeverity.CRITICAL,
        category="protective_stop_cancel_unresolved",
        message=(
            f"Cancelling resting protective stop for position {position.id} "
            f"returned unexpected status {result.status.value}; exit deferred."
        ),
        mode=OrderMode.LIVE,
        dedup_key=f"protective_stop_cancel_unresolved:{position.id}",
    )
    return CancelOutcome.FAILED


def sync_resting_protective_stop(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    stop_plan: StopPlan,
    resting_order_id: str,
    desired_trigger_price: Decimal,
    broker: BrokerPort | None,
) -> None:
    """Keeps this position's resting protective SL-LMT's own trigger/limit
    price in step with `desired_trigger_price` (the current effective
    protective floor — `trail_plan.current_stop_price` once trail is
    active, computed by `evaluate_open_position`'s own step 5) via a real
    `ModifyOrder` call — the TSL half of "Hard SL with Local Target",
    `place_protective_stop`'s placement being the other half.

    **Fallback if the modify is rejected**: never raises, and never
    touches `stop_plan.resting_order_id` — the resting order itself is
    untouched, still armed at its last successfully-confirmed price, which
    is real, valid protection, just not yet at the tightened level. Only
    `stop_plan.resting_order_price` tracks "the price we last successfully
    confirmed" versus "the price we currently want" — if a modify fails,
    those two values keep disagreeing, so this same function retries on
    every later cycle the trail is active, with no separate retry/backoff
    bookkeeping needed. Critically, the position's actual exit (target/
    trail/structure/spread/EOD/manual/margin-breach) never depends on the
    resting order's own armed price at all — `close_position`'s Path B
    (`cancel_resting_protective_stop`) always cancels whatever is resting
    and places a fresh exit at the locally-computed intended price,
    regardless of what price the resting order happened to be armed at —
    so a stuck/failed sync only degrades this position's *crash-only*
    resilience for the trailed delta, never its normal (process-alive)
    exit correctness. A `WARNING`, not `CRITICAL`, `SystemAlert` reflects
    that: the position is not left unprotected, just running on its last
    confirmed level.
    """
    option_contract = db.get(OptionContract, position.option_contract_id)
    if option_contract is None:
        return
    instrument = db.get(Instrument, option_contract.instrument_id)
    if instrument is None:
        return

    exit_side = _opposite(SignalSide(position.side))
    tick_size = _dec(instrument.tick_size)
    trigger_price = _round_to_tick(desired_trigger_price, tick_size, exit_side)

    # Compare tick-*rounded* values, not the raw Decimal the trail
    # arithmetic produced -- `desired_trigger_price` creeps by sub-tick
    # amounts most cycles, which would otherwise trigger a redundant
    # ModifyOrder call even when the actual price at the broker wouldn't
    # change at all once rounded.
    current_price = (
        _dec(stop_plan.resting_order_price) if stop_plan.resting_order_price is not None else None
    )
    if current_price is not None and current_price == trigger_price:
        return

    buffer_pct = _dec(get_settings().app.live_limit_order_buffer_pct)
    limit_price = _round_to_tick(
        _apply_slippage(trigger_price, exit_side, buffer_pct), tick_size, exit_side
    )

    from app.modules.execution_engine.paper.service import resolve_broker_for_position

    resolved_broker = broker or resolve_broker_for_position(db, trading_session, position)

    try:
        resolved_broker.modify_order(
            resting_order_id,
            contract_symbol=option_contract.symbol,
            trigger_price=float(trigger_price),
            limit_price=float(limit_price),
        )
    except Exception:  # noqa: BLE001 - see place_protective_stop's identical reasoning
        # Broader than `BrokerError` deliberately -- `evaluate_open_
        # position` has nothing wrapping this specific call, so an uncaught
        # exception here would abort the rest of *this* position's own
        # evaluation this cycle (stop/target/trail checks after the TSL
        # sync). PositionManager's own per-position loop (its caller) has
        # had its own try/except since 2026-08-25, so it would still catch
        # this and move on to the next position rather than aborting the
        # whole cycle -- but this function shouldn't rely on that outer
        # safety net to avoid leaving its own TSL-sync step half-done.
        logger.warning(
            "TSL sync failed for position %s (resting order %s) -- resting stop stays "
            "armed at its last confirmed price %s, not the newly tightened %s; will "
            "retry next cycle",
            position.id,
            resting_order_id,
            stop_plan.resting_order_price,
            float(trigger_price),
            exc_info=True,
        )
        send_alert(
            db,
            workspace_id=trading_session.workspace_id,
            trading_session_id=trading_session.id,
            severity=AlertSeverity.WARNING,
            category="protective_stop_modify_failed",
            message=(
                f"TSL modify failed for position {position.id}; resting stop still "
                f"armed at {stop_plan.resting_order_price}, not yet {float(trigger_price)}."
            ),
            mode=OrderMode.LIVE,
            dedup_key=f"protective_stop_modify_failed:{position.id}",
        )
        db.flush()
        return

    stop_plan.resting_order_price = float(trigger_price)
    stop_plan.updated_at = _utcnow()
    db.add(stop_plan)
    db.flush()
