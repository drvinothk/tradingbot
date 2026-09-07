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
from datetime import timedelta
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
        # filled*. A malformed request raising something other than
        # `BrokerError` (e.g. a normalization bug) is a real, concrete
        # example of what this must still catch.
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


def _modify_resting_order(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    resting_order_id: str,
    desired_trigger_price: Decimal,
    new_qty: int,
    broker: BrokerPort | None,
    *,
    context: str,
    fail_alert_category: str,
    extra_modify_kwargs: dict[str, object] | None = None,
) -> Decimal | None:
    """Shared body of every resting-SL-LMT `ModifyOrder` in this module
    (`sync_resting_protective_stop` TSL tightening, `resize_resting_protective_
    stop` carrier shrink, `exit_via_resting_stop` fire-now). Resolves the
    contract/instrument/broker, tick-rounds the trigger plus a buffered limit,
    issues exactly one `broker.modify_order`, and on any failure logs a WARNING
    + raises a WARNING `SystemAlert` in `fail_alert_category` — never raises to
    the caller (same "never leave the position worse off, never crash the
    caller" contract every helper in this file makes).

    Returns the tick-rounded trigger `Decimal` on success (the caller persists
    its own `stop_plan` fields from it) or `None` on any failure / unresolvable
    contract. `extra_modify_kwargs` is merged into the `modify_order` call
    verbatim -- a hook for a future caller that needs to change a field beyond
    trigger/limit/qty; no current caller uses it (the fire-now exit keeps the
    order type `SL-LIMIT`, only its trigger moves -- converting it to a plain
    `LIMIT` would reintroduce the RMS naked-short rejection this design exists
    to avoid). Shoonya requires `qty` on every `ModifyOrder` regardless of
    what is changing, so it is always sent.
    """
    option_contract = db.get(OptionContract, position.option_contract_id)
    if option_contract is None:
        return None
    instrument = db.get(Instrument, option_contract.instrument_id)
    if instrument is None:
        return None

    exit_side = _opposite(SignalSide(position.side))
    tick_size = _dec(instrument.tick_size)
    trigger_price = _round_to_tick(desired_trigger_price, tick_size, exit_side)
    buffer_pct = _dec(get_settings().app.live_limit_order_buffer_pct)
    limit_price = _round_to_tick(
        _apply_slippage(trigger_price, exit_side, buffer_pct), tick_size, exit_side
    )

    from app.modules.execution_engine.paper.service import resolve_broker_for_position

    resolved_broker = broker or resolve_broker_for_position(db, trading_session, position)

    modify_kwargs: dict[str, object] = {
        "contract_symbol": option_contract.symbol,
        "trigger_price": float(trigger_price),
        "limit_price": float(limit_price),
        "qty": new_qty,
    }
    if extra_modify_kwargs:
        modify_kwargs.update(extra_modify_kwargs)

    try:
        resolved_broker.modify_order(resting_order_id, **modify_kwargs)
    except Exception:  # noqa: BLE001 - see place_protective_stop's identical reasoning
        logger.warning(
            "%s failed for position %s (resting order %s) -- resting stop left armed at "
            "its last confirmed level; will retry next cycle",
            context,
            position.id,
            resting_order_id,
            exc_info=True,
        )
        send_alert(
            db,
            workspace_id=trading_session.workspace_id,
            trading_session_id=trading_session.id,
            severity=AlertSeverity.WARNING,
            category=fail_alert_category,
            message=(
                f"{context} failed for position {position.id}; resting stop still armed "
                f"at its last confirmed level, not yet {float(trigger_price)}."
            ),
            mode=OrderMode.LIVE,
            dedup_key=f"{fail_alert_category}:{position.id}",
        )
        db.flush()
        return None

    return trigger_price


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
    # 2026-09-08 defense-in-depth: once `exit_via_resting_stop` has driven
    # this same resting order to fire (`exit_fired_at` set), never move its
    # trigger back toward a protective/trail level -- that would un-fire a
    # pending fire-now exit. `evaluate_open_position`'s own call site already
    # guards this; the guard is repeated here so no future caller can bypass
    # it (see the 2026-09-07 exit-path redesign notes).
    if stop_plan.exit_fired_at is not None:
        return
    # Skip a redundant ModifyOrder when the tick-rounded trigger wouldn't
    # actually change at the broker -- `desired_trigger_price` creeps by
    # sub-tick amounts most cycles.
    option_contract = db.get(OptionContract, position.option_contract_id)
    if option_contract is None:
        return
    instrument = db.get(Instrument, option_contract.instrument_id)
    if instrument is None:
        return
    exit_side = _opposite(SignalSide(position.side))
    rounded = _round_to_tick(desired_trigger_price, _dec(instrument.tick_size), exit_side)
    current_price = (
        _dec(stop_plan.resting_order_price) if stop_plan.resting_order_price is not None else None
    )
    if current_price is not None and current_price == rounded:
        return

    confirmed_trigger = _modify_resting_order(
        db,
        trading_session,
        position,
        resting_order_id,
        desired_trigger_price,
        position.qty,
        broker,
        context="TSL sync",
        fail_alert_category="protective_stop_modify_failed",
    )
    if confirmed_trigger is None:
        return

    stop_plan.resting_order_price = float(confirmed_trigger)
    stop_plan.updated_at = _utcnow()
    db.add(stop_plan)
    db.flush()


def resize_resting_protective_stop(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    stop_plan: StopPlan,
    resting_order_id: str,
    desired_trigger_price: Decimal,
    new_qty: int,
    broker: BrokerPort | None,
) -> None:
    """Shrink a legged position's whole-position carrier SL-LMT to `new_qty`
    (and re-anchor its trigger to the new worst remaining leg stop) after a
    leg closes, via one `ModifyOrder`. `new_qty` is passed explicitly because
    `position.qty` may or may not have been decremented yet at the call site.

    Never raises. On a modify failure this leaves the *larger* order armed
    (a too-big stop is strictly safer than none) and raises a WARNING, not a
    CRITICAL: the carrier trigger is a hair below every leg stop, so it can
    only ever fire when the app poll is dead — at which point closing the
    whole remaining position is the correct outcome regardless of the exact
    qty. The stale size only matters on a triple-compound failure (resize
    fails, then the app disconnects, then price reaches the trigger), which
    reconciliation would then catch and lock on.
    """
    # 2026-09-08: same guard as `sync_resting_protective_stop` -- a carrier
    # already driven to fire (`exit_fired_at` set) must not have its trigger
    # re-anchored to a leg stop, which would un-fire the pending exit. This is
    # the call path `evaluate_open_position`'s own guard does NOT cover
    # (`_sync_carrier_stop_after_leg_close`), so it is load-bearing here, not
    # just belt-and-braces.
    if stop_plan.exit_fired_at is not None:
        return
    confirmed_trigger = _modify_resting_order(
        db,
        trading_session,
        position,
        resting_order_id,
        desired_trigger_price,
        new_qty,
        broker,
        context="carrier stop resize",
        fail_alert_category="protective_stop_resize_failed",
    )
    if confirmed_trigger is None:
        return

    stop_plan.qty = new_qty
    stop_plan.stop_price = float(desired_trigger_price)
    stop_plan.resting_order_price = float(confirmed_trigger)
    stop_plan.updated_at = _utcnow()
    db.add(stop_plan)
    db.flush()


class ExitViaStopOutcome(enum.Enum):
    """Result of `exit_via_resting_stop` — plumbing for `close_position` /
    the legged exit path, not a domain concept."""

    FIRED_PENDING = "fired_pending"
    MODIFY_FAILED = "modify_failed"
    NO_RESTING_ORDER = "no_resting_order"


# 2026-09-08 (A3): a fire-now `ModifyOrder` sets the trigger just below LTP, so
# it normally fills within a tick or two. If this long passes with the position
# still OPEN, the market has run away from the trigger (a TARGET exit where
# premium kept rising is the textbook case) -- re-anchor the trigger to the
# *current* LTP and try again. The window also rate-limits re-anchors so a
# per-cycle `force` caller (margin-breach sweep) can't hammer the broker. A
# non-`force` re-anchor consumes one `exit_fire_attempts`; after
# `_MAX_EXIT_ORDER_ATTEMPTS` of them the caller escalates to
# `_handle_exit_attempts_exhausted` (manual reconcile). A `force` re-anchor
# (EOD / margin-breach / manual square-off) does not consume the budget -- an
# operator flatten must keep chasing until it fills.
_FIRE_NOW_STALE_TIMEOUT = timedelta(seconds=90)


def exit_via_resting_stop(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    stop_plan: StopPlan,
    exit_reason: ExitReason,
    ltp: float,
    qty: int,
    broker: BrokerPort | None,
    *,
    force: bool = False,
) -> ExitViaStopOutcome:
    """LIVE exit for a position that already has a resting broker SL-LMT:
    drive that order to fire *now* via one `ModifyOrder` instead of
    cancelling it and placing a fresh `LIMIT` sell.

    Why: a fresh plain-`LIMIT` sell of a long option is margin-checked by
    Shoonya's RMS as a *new naked short* (full SPAN) and rejected on a thin
    account -- the 2026-09-07 live incident, twice. Modifying the
    already-accepted SL-LMT to a fire-now trigger (`ltp - 1 tick`, the
    max-below-LTP a sell stop allows) with a marketable buffered limit is
    the same order, same type, same direction of trigger movement as
    `sync_resting_protective_stop`'s trail-tighten -- the RMS-lightest exit.

    Never raises (delegates to `_modify_resting_order`, which owns the
    WARNING-alert-and-return-`None` contract). Returns:

    - `NO_RESTING_ORDER` -- `stop_plan.resting_order_id` (or its `Order`
      row) is absent; the caller falls back to the fresh-`LIMIT` path
      (only reachable when entry-time SL placement already failed + alerted).
    - `FIRED_PENDING` -- the modify is confirmed (or was already fired on a
      prior cycle: `stop_plan.exit_fired_at` set). The async
      `reconcile_pending_live_exit_orders` owns finalization from here,
      using `Order.intended_exit_reason` (persisted below) so a
      late-discovered fill reports the real reason, not generic STOP.
    - `MODIFY_FAILED` -- this attempt's modify failed;
      `stop_plan.exit_fire_attempts` is bumped, the caller retries next
      cycle, and `_MAX_EXIT_ORDER_ATTEMPTS` bumps escalate to the
      exhaustion path (a resting-order position never creates `exit:{id}`
      rows, so the `Order`-count exhaustion check can't see it).

    `force` (EOD / margin-breach / manual square-off) drives a re-anchor even
    when a fire was already confirmed -- an operator flatten must not be
    blocked by the idempotency gate if the earlier fire hasn't filled. Still
    rate-limited to one re-anchor per `_FIRE_NOW_STALE_TIMEOUT`.
    """
    from app.modules.execution_engine.paper.service import _MAX_EXIT_ORDER_ATTEMPTS

    resting_order_id = stop_plan.resting_order_id
    if resting_order_id is None:
        return ExitViaStopOutcome.NO_RESTING_ORDER

    # Already fired. Normally just report it and let
    # `reconcile_pending_live_exit_orders` finalise the async fill -- do NOT
    # re-`ModifyOrder` every ~3s poll. But if the fire has gone stale with no
    # fill (market ran away from the trigger), or a `force` caller wants it
    # out now, re-anchor to the current LTP -- once per `_FIRE_NOW_STALE_
    # TIMEOUT`.
    if stop_plan.exit_fired_at is not None:
        recently_fired = _utcnow() - stop_plan.exit_fired_at <= _FIRE_NOW_STALE_TIMEOUT
        if recently_fired:
            return ExitViaStopOutcome.FIRED_PENDING
        if not force and stop_plan.exit_fire_attempts >= _MAX_EXIT_ORDER_ATTEMPTS:
            # Chased the market this many times with no fill -- stop hitting
            # the broker; the caller's `>= _MAX` check routes to
            # `_handle_exit_attempts_exhausted`.
            return ExitViaStopOutcome.MODIFY_FAILED
        if not force:
            stop_plan.exit_fire_attempts += 1
        stop_plan.exit_fired_at = None
        stop_plan.updated_at = _utcnow()
        db.add(stop_plan)
        db.flush()

    # Retries exhausted -- stop bumping the counter / re-hitting the broker;
    # the caller's `>= _MAX_EXIT_ORDER_ATTEMPTS` check routes to
    # `_handle_exit_attempts_exhausted` (which keeps retrying auto-repair).
    # A `force` caller is exempt: an operator flatten still fires even at the
    # cap (it just can't push the counter higher).
    if not force and stop_plan.exit_fire_attempts >= _MAX_EXIT_ORDER_ATTEMPTS:
        return ExitViaStopOutcome.MODIFY_FAILED

    stop_order = (
        db.query(Order).filter(Order.idempotency_key == f"stop:{position.id}").one_or_none()
    )
    if stop_order is None:
        return ExitViaStopOutcome.NO_RESTING_ORDER

    option_contract = db.get(OptionContract, position.option_contract_id)
    instrument = (
        db.get(Instrument, option_contract.instrument_id) if option_contract is not None else None
    )
    if option_contract is None or instrument is None:
        return ExitViaStopOutcome.MODIFY_FAILED

    now = _utcnow()
    # Persist intent BEFORE the modify -- a crash after the broker acts must
    # still let reconciliation report the real reason and the right qty.
    stop_order.intended_exit_reason = exit_reason
    stop_order.qty = qty
    db.add(stop_order)
    db.flush()

    tick_size = _dec(instrument.tick_size)
    fire_trigger = _dec(ltp) - tick_size
    if fire_trigger <= 0:
        fire_trigger = tick_size

    confirmed_trigger = _modify_resting_order(
        db,
        trading_session,
        position,
        resting_order_id,
        fire_trigger,
        qty,
        broker,
        context="fire-now exit",
        fail_alert_category="protective_stop_exit_modify_failed",
    )
    if confirmed_trigger is None:
        stop_plan.exit_fire_attempts += 1
        stop_plan.updated_at = now
        db.add(stop_plan)
        db.flush()
        return ExitViaStopOutcome.MODIFY_FAILED

    stop_plan.exit_fired_at = now
    stop_plan.resting_order_price = float(confirmed_trigger)
    stop_plan.updated_at = now
    db.add(stop_plan)
    db.flush()
    return ExitViaStopOutcome.FIRED_PENDING
