"""Paper Execution Service — the real Order -> fill -> Position ->
StopPlan/TrailPlan -> TradeOutcome lifecycle that Phase 3 introduces to
replace Phase 2's `close_dispatched_trade_intent_synthetically`/
`record_synthetic_outcome` stand-in.

Calls `BrokerPort.place_order`/`get_positions` against whichever adapter
`app.modules.broker_adapter.composition.get_execution_broker` resolves to
(the persistent mock, today, regardless of what `get_broker()` — the
market-data accessor — currently holds) rather than simulating fills purely
from tick data — this reuses Phase 1's already-built order/position
simulation and gives Reconciliation Service a genuine broker-side state to
diff local `positions` against. See the Phase 3 plan's "Key design
decision" note for the full reasoning; Phase 6's real live-order path will
extend `get_execution_broker` with graduation gating, not rewrite this
module. `get_execution_broker` is deliberately separate from `get_broker`
so that connecting Shoonya for real market data (Phase 5) can never, by
itself, cause a paper trade to place a real order — see
`broker_adapter/composition.py`'s own docstring for the incident that
motivated the split.

Callers (not this module): `strategy_engine.service.submit_signal` calls
`dispatch_trade_intent` right after `risk_engine.service.evaluate_trade_intent`
returns an AUTO-mode approval; `api.v1.strategies.approve_trade_approval`
calls it for the approval-required path. Neither call happens *inside*
`evaluate_trade_intent`'s own `LOCK_RISK_EVALUATION_QUEUE` scope — keeping
Risk Service's lock and this module's `LOCK_EXECUTION_SINGLETON` disjoint in
every call path avoids ever nesting the two locks.

Both `dispatch_trade_intent` and `close_position` run an event-triggered
`reconciliation.service.run_reconciliation` pass just before returning,
still inside their own `LOCK_EXECUTION_SINGLETON` scope — per the build
plan, Reconciliation Service is "event-triggered + polling", and
`PositionManager` only covers the polling half. Nesting is safe here: a
mismatch can escalate to `reconciliation_lock` via `transition_mode`, which
acquires the *same* `LOCK_EXECUTION_SINGLETON` again — `core/locking.py`'s
transaction-scoped advisory locks are reentrant/stacked per transaction (a
second `pg_advisory_xact_lock(key)` call on a key this same
session/transaction already holds returns immediately, no-op), so this
cannot self-deadlock; it only would if some other call path acquired
`LOCK_RISK_EVALUATION_QUEUE` before `LOCK_EXECUTION_SINGLETON` and the two
crossed with this one, which nothing in this codebase does.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.locking import LOCK_EXECUTION_SINGLETON, advisory_lock
from app.core.sleep_inhibitor import get_sleep_inhibitor
from app.domain.audit.models import ActorType, EventCategory
from app.domain.broker.models import ReconciliationTrigger
from app.domain.execution.models import (
    ExitReason,
    Order,
    OrderEvent,
    OrderMode,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionStatus,
    StopPlan,
    StopPlanStatus,
    TradeOutcome,
    TrailPlan,
    TrailPlanStatus,
)
from app.domain.market.models import Instrument, OptionContract
from app.domain.ops.models import AlertSeverity, SystemAlert
from app.domain.session.models import TradingSession
from app.domain.strategy.models import SignalSide, TradeIntent
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.base.contracts import BrokerOrderStatus, OrderRequest
from app.modules.broker_adapter.base.contracts import OrderSide as BrokerOrderSide
from app.modules.broker_adapter.base.contracts import OrderType as BrokerOrderType
from app.modules.broker_adapter.composition import get_execution_broker
from app.modules.reconciliation.service import run_reconciliation

logger = logging.getLogger("app.execution_engine.paper.service")

# Generic Phase-3 trailing rule, used when a TradeIntent doesn't specify its
# own (Phase 4's per-strategy override — see _open_position_from_fill).
# Activates once unrealized profit reaches this fraction of the
# entry->target distance; once active, the stop trails to lock in this
# fraction of favorable movement beyond the activation point, monotonically
# tightening only.
TRAIL_ACTIVATION_FRACTION = Decimal("0.5")
TRAIL_LOCK_FRACTION = Decimal("0.5")

# Phase 4: generic spread-blowout exit — the same threshold for every
# strategy regardless of structure_level (unlike the trail, this isn't
# per-method). A wider threshold than StrikeRankingConfig.max_spread_pct
# (0.15, an *entry* filter) deliberately: an open position shouldn't be
# force-exited by the same bar the entry filter would merely have scored
# lower, only once liquidity has genuinely dried up.
SPREAD_BLOWOUT_PCT = Decimal("0.30")


def _utcnow() -> datetime:
    return datetime.now(UTC)


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


def dispatch_trade_intent(
    db: Session,
    trading_session: TradingSession,
    trade_intent: TradeIntent,
    broker: BrokerPort | None = None,
) -> Order:
    """Idempotency-before-dispatch: a repeated call for the same TradeIntent
    (retry after a crash between the broker call and the commit, or a
    duplicate call from a confused caller) returns the existing `Order`
    rather than placing a second one — the check happens first, inside the
    same `LOCK_EXECUTION_SINGLETON` scope as the insert, so two concurrent
    callers can't both pass it.
    """
    broker = broker or get_execution_broker(trading_session)

    with advisory_lock(db, LOCK_EXECUTION_SINGLETON):
        existing = (
            db.query(Order)
            .filter(Order.idempotency_key == trade_intent.idempotency_key)
            .one_or_none()
        )
        if existing is not None:
            return existing

        option_contract = db.get(OptionContract, trade_intent.option_contract_id)
        if option_contract is None:
            raise ValueError(f"unknown option_contract_id {trade_intent.option_contract_id}")
        instrument = db.get(Instrument, option_contract.instrument_id)
        if instrument is None:
            raise ValueError(f"unknown instrument for option_contract {option_contract.id}")

        side = SignalSide(trade_intent.side)
        qty = trade_intent.qty_lots * instrument.lot_size
        now = _utcnow()

        order_result = broker.place_order(
            OrderRequest(
                idempotency_key=trade_intent.idempotency_key,
                contract_symbol=option_contract.symbol,
                side=_to_broker_side(side),
                order_type=BrokerOrderType.MARKET,
                qty=qty,
            )
        )

        order = Order(
            id=uuid.uuid4(),
            workspace_id=trading_session.workspace_id,
            trading_session_id=trading_session.id,
            option_contract_id=option_contract.id,
            trade_intent_id=trade_intent.id,
            idempotency_key=trade_intent.idempotency_key,
            mode=OrderMode.PAPER,
            side=_to_domain_side(side),
            order_type=OrderType.MARKET,
            qty=qty,
            status=_map_status(order_result.status),
            filled_qty=order_result.filled_qty,
            avg_fill_price=order_result.avg_fill_price,
            broker_order_id=order_result.broker_order_id,
            submitted_at=now,
            updated_at=now,
        )
        db.add(order)
        db.flush()

        db.add(
            OrderEvent(
                id=uuid.uuid4(),
                order_id=order.id,
                event_type="filled" if order.status == OrderStatus.FILLED else "submitted",
                raw_payload={
                    "broker_order_id": order_result.broker_order_id,
                    "status": order_result.status.value,
                    "filled_qty": order_result.filled_qty,
                    "avg_fill_price": order_result.avg_fill_price,
                },
                ts=now,
            )
        )

        record_event(
            db,
            workspace_id=trading_session.workspace_id,
            actor_type=ActorType.SYSTEM,
            event_category=EventCategory.ORDER_LIFECYCLE,
            event_type="order.dispatched",
            entity_type="order",
            entity_id=order.id,
            trading_session_id=trading_session.id,
            payload={
                "trade_intent_id": str(trade_intent.id),
                "qty": qty,
                "status": order.status.value,
            },
        )

        if order.status == OrderStatus.FILLED:
            _open_position_from_fill(
                db, trading_session, trade_intent, option_contract, order, side
            )

        db.flush()
        run_reconciliation(db, broker, trading_session, ReconciliationTrigger.EVENT)
        return order


def _open_position_from_fill(
    db: Session,
    trading_session: TradingSession,
    trade_intent: TradeIntent,
    option_contract: OptionContract,
    order: Order,
    side: SignalSide,
) -> Position:
    now = _utcnow()
    entry_price = _dec(order.avg_fill_price)

    position = Position(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=trade_intent.id,
        opening_order_id=order.id,
        side=_to_domain_side(side),
        qty=order.filled_qty,
        entry_price=float(entry_price),
        status=PositionStatus.OPEN,
        opened_at=now,
    )
    db.add(position)
    db.flush()

    # StopPlan.qty is meant to be recomputed on every fill event touching
    # this position — with the mock adapter's synchronous full-fill
    # behavior there's only ever one fill event this phase, so this always
    # equals order.filled_qty; the recompute path is designed for
    # partial-fill-capable brokers (Phase 5+), not exercised until then.
    stop_plan = StopPlan(
        id=uuid.uuid4(),
        position_id=position.id,
        stop_price=float(trade_intent.stop_price),
        qty=position.qty,
        structure_level=trade_intent.structure_level,
        status=StopPlanStatus.CONFIRMED,
        created_at=now,
        updated_at=now,
    )

    # Per-method trailing (Phase 4): a strategy that supplied its own
    # activation/lock fractions on the TradeIntent overrides the generic
    # Phase-3 0.5/0.5 rule; None (SyntheticStrategy, and any strategy that
    # doesn't set them) falls back to it unchanged.
    activation_fraction = (
        _dec(trade_intent.trail_activation_fraction)
        if trade_intent.trail_activation_fraction is not None
        else TRAIL_ACTIVATION_FRACTION
    )
    lock_fraction = (
        _dec(trade_intent.trail_lock_fraction)
        if trade_intent.trail_lock_fraction is not None
        else TRAIL_LOCK_FRACTION
    )
    activation_distance = (
        abs(_dec(trade_intent.target_price) - _dec(trade_intent.entry_price)) * activation_fraction
    )
    activation_price = (
        entry_price + activation_distance
        if side == SignalSide.BUY
        else entry_price - activation_distance
    )
    trail_plan = TrailPlan(
        id=uuid.uuid4(),
        position_id=position.id,
        trail_type="generic_activation_lock",
        activation_price=float(activation_price),
        trail_value=float(lock_fraction),
        current_stop_price=None,
        status=TrailPlanStatus.INACTIVE,
        updated_at=now,
    )
    db.add_all([stop_plan, trail_plan])
    db.flush()

    record_event(
        db,
        workspace_id=trading_session.workspace_id,
        actor_type=ActorType.SYSTEM,
        event_category=EventCategory.ORDER_LIFECYCLE,
        event_type="position.opened",
        entity_type="position",
        entity_id=position.id,
        trading_session_id=trading_session.id,
        payload={"qty": position.qty, "entry_price": float(entry_price)},
    )

    # Sleep inhibitor: "has an open position" half of the two overlapping
    # lifecycles core/sleep_inhibitor.py's own docstring describes — the
    # other half (actively scanning) is acquired/released in
    # api.v1.strategies.start_strategy/stop_strategy. Released in
    # close_position below.
    get_sleep_inhibitor().acquire(f"position:{position.id}")

    # Deliberately does NOT subscribe this position's option-contract symbol
    # for live pricing here — see PositionManager._ensure_symbol_subscribed's
    # own docstring. Doing it in this function (called directly, with a
    # test-owned `broker=`, from unit/integration tests all over this
    # codebase) would repeat the exact `ensure_ingestion_running`-touches-
    # production-`session_scope` trap this file's own module docstring
    # already warns about for PositionManager itself: a real, live bug in an
    # earlier version of this change spawned MarketDataIngestionService
    # background threads against the *real* dev database from ordinary test
    # runs (found via a QC pass — 3,400+ stray quote_ticks rows in the dev
    # DB). PositionManager subscribes for its own tracked positions instead,
    # directly on whichever market_data_provider it was given — no DB
    # session, no registry singleton, nothing for a test's own broker/db
    # fixtures to accidentally bypass.

    return position


def close_position(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    exit_reason: ExitReason,
    intended_price: float,
    broker: BrokerPort | None = None,
) -> TradeOutcome | None:
    """Idempotent no-op (returns `None`) if the position is already closed —
    stop/target/trail checks and EOD square-off can all race to close the
    same position (e.g. price crosses stop right at cutoff_time); the second
    caller must not double-exit. `intended_price` is the price level that
    justified this exit (stop_price/target_price/the trail's current stop,
    or the current market price for EOD/manual) — it's what `slippage` is
    measured against, not a duplicate of the actual fill price.
    """
    broker = broker or get_execution_broker(trading_session)

    with advisory_lock(db, LOCK_EXECUTION_SINGLETON):
        if position.status != PositionStatus.OPEN:
            return None

        option_contract = db.get(OptionContract, position.option_contract_id)
        if option_contract is None:
            raise ValueError(f"unknown option_contract_id {position.option_contract_id}")

        entry_side = SignalSide(position.side)
        exit_side = _opposite(entry_side)
        now = _utcnow()
        exit_idempotency_key = f"exit:{position.id}"

        exit_order = (
            db.query(Order).filter(Order.idempotency_key == exit_idempotency_key).one_or_none()
        )
        if exit_order is None:
            order_result = broker.place_order(
                OrderRequest(
                    idempotency_key=exit_idempotency_key,
                    contract_symbol=option_contract.symbol,
                    side=_to_broker_side(exit_side),
                    order_type=BrokerOrderType.MARKET,
                    qty=position.qty,
                )
            )
            exit_order = Order(
                id=uuid.uuid4(),
                workspace_id=trading_session.workspace_id,
                trading_session_id=trading_session.id,
                option_contract_id=option_contract.id,
                position_id=position.id,
                idempotency_key=exit_idempotency_key,
                mode=OrderMode.PAPER,
                side=_to_domain_side(exit_side),
                order_type=OrderType.MARKET,
                qty=position.qty,
                status=_map_status(order_result.status),
                filled_qty=order_result.filled_qty,
                avg_fill_price=order_result.avg_fill_price,
                broker_order_id=order_result.broker_order_id,
                submitted_at=now,
                updated_at=now,
            )
            db.add(exit_order)
            db.flush()
            db.add(
                OrderEvent(
                    id=uuid.uuid4(),
                    order_id=exit_order.id,
                    event_type="filled",
                    raw_payload={
                        "broker_order_id": order_result.broker_order_id,
                        "status": order_result.status.value,
                        "filled_qty": order_result.filled_qty,
                        "avg_fill_price": order_result.avg_fill_price,
                    },
                    ts=now,
                )
            )

        # Narrowly scoped: only a non-FILLED (or price-less) exit order hits
        # this — normal exits, the only path reachable in production today
        # (MockBrokerAdapter always fills; Shoonya's real place_order falls
        # back to get_order_status on an ack timeout), are unaffected. Left
        # OPEN rather than marked CLOSED off no/partial fill data, so the
        # next PositionManager cycle or a manual reconcile can still see and
        # retry it — no new state machine, reuses the existing SystemAlert
        # pattern every other hard-stop condition in this codebase already
        # uses.
        if exit_order.status != OrderStatus.FILLED or exit_order.avg_fill_price is None:
            logger.error(
                "exit order for position %s did not fill (status=%s) — "
                "leaving position OPEN for reconciliation/retry",
                position.id,
                exit_order.status,
            )
            db.add(
                SystemAlert(
                    id=uuid.uuid4(),
                    workspace_id=trading_session.workspace_id,
                    trading_session_id=trading_session.id,
                    severity=AlertSeverity.CRITICAL,
                    category="exit_order_unfilled",
                    message=(
                        f"Exit order for position {position.id} did not fill "
                        f"(status={exit_order.status}); position left OPEN."
                    ),
                    created_at=now,
                )
            )
            db.flush()
            return None

        exit_price = _dec(exit_order.avg_fill_price)
        entry_price = _dec(position.entry_price)
        # +1 for a long (BUY) position, -1 for a short (SELL) position —
        # same sign convention risk_engine.service.compute_pre_trade_analytics
        # uses for its P&L-scenario table.
        sign = Decimal("1") if entry_side == SignalSide.BUY else Decimal("-1")
        qty = Decimal(position.qty)
        realized_pnl = (exit_price - entry_price) * qty * sign
        slippage = (exit_price - _dec(intended_price)) * qty * sign

        position.status = PositionStatus.CLOSED
        position.closed_at = now
        position.closing_order_id = exit_order.id
        db.add(position)

        # Releases this position's half of the sleep inhibitor's reference
        # count — see the matching acquire in _open_position_from_fill.
        get_sleep_inhibitor().release(f"position:{position.id}")

        # No matching unsubscribe call here — see _open_position_from_fill's
        # own comment for why this module never touches market-data
        # subscriptions at all. PositionManager owns that lifecycle.

        stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one_or_none()
        if stop_plan is not None and stop_plan.status not in (
            StopPlanStatus.TRIGGERED,
            StopPlanStatus.CANCELLED,
        ):
            stop_plan.status = (
                StopPlanStatus.TRIGGERED
                if exit_reason == ExitReason.STOP
                else StopPlanStatus.CANCELLED
            )
            stop_plan.updated_at = now
            db.add(stop_plan)

        trail_plan = db.query(TrailPlan).filter(TrailPlan.position_id == position.id).one_or_none()
        if trail_plan is not None and trail_plan.status != TrailPlanStatus.TRIGGERED:
            if exit_reason == ExitReason.TRAIL:
                trail_plan.status = TrailPlanStatus.TRIGGERED
                trail_plan.updated_at = now
                db.add(trail_plan)

        outcome = TradeOutcome(
            id=uuid.uuid4(),
            workspace_id=trading_session.workspace_id,
            trading_session_id=trading_session.id,
            position_id=position.id,
            trade_intent_id=position.trade_intent_id,
            entry_price=float(entry_price),
            exit_price=float(exit_price),
            qty=position.qty,
            realized_pnl=float(realized_pnl),
            slippage=float(slippage),
            exit_reason=exit_reason,
            closed_at=now,
        )
        db.add(outcome)
        db.flush()

        record_event(
            db,
            workspace_id=trading_session.workspace_id,
            actor_type=ActorType.SYSTEM,
            event_category=EventCategory.ORDER_LIFECYCLE,
            event_type="position.closed",
            entity_type="position",
            entity_id=position.id,
            trading_session_id=trading_session.id,
            payload={
                "exit_reason": exit_reason.value,
                "realized_pnl": float(realized_pnl),
                "slippage": float(slippage),
            },
        )

        # Imported here, not at module scope: risk_engine.service never
        # imports this module (it only marks a TradeIntent DISPATCHED and
        # lets the caller invoke dispatch_trade_intent), so this stays a
        # one-directional dependency — importing at module scope would work
        # too, but keeping it local makes that directionality obvious at the
        # call site instead of relying on remembering it.
        from app.modules.risk_engine.service import record_trade_outcome_effects

        record_trade_outcome_effects(db, trading_session, float(realized_pnl))

        db.flush()
        run_reconciliation(db, broker, trading_session, ReconciliationTrigger.EVENT)
        return outcome


def evaluate_open_position(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    tick_price: float,
    broker: BrokerPort | None = None,
    bid: float | None = None,
    ask: float | None = None,
    underlying_price: float | None = None,
) -> TradeOutcome | None:
    """Checks stop/target/structure-break/spread-blowout/trail against
    `tick_price` (plus, for the two Phase 4 checks, the option's own live
    `bid`/`ask` and the *underlying's* current price) and closes the position
    if triggered; otherwise advances the trail plan per the generic Phase-3
    rule. Called by `PositionManager` on every price poll, and by
    `scheduler.eod_square_off` is *not* routed through here — EOD is an
    unconditional force-close regardless of where price sits.

    `bid`/`ask`/`underlying_price` are optional: a position whose
    `stop_plan.structure_level` is null (any strategy that doesn't set one,
    e.g. SyntheticStrategy) simply never triggers the structure-break check
    regardless, and callers that don't pass bid/ask (existing tests) just
    skip the spread-blowout check the same way.
    """
    if position.status != PositionStatus.OPEN:
        return None

    trade_intent = db.get(TradeIntent, position.trade_intent_id)
    if trade_intent is None:
        return None
    stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one_or_none()
    trail_plan = db.query(TrailPlan).filter(TrailPlan.position_id == position.id).one_or_none()
    if stop_plan is None:
        return None

    side = SignalSide(position.side)
    price = _dec(tick_price)
    stop_price = _dec(stop_plan.stop_price)
    target_price = _dec(trade_intent.target_price)
    favorable = side == SignalSide.BUY

    # 1. Stop hit (checked first — capital preservation takes priority over
    # a target that happens to be hit the same tick, which can't actually
    # occur for a sane stop < entry < target but is checked in this order
    # regardless, for defense-in-depth).
    hit_stop = price <= stop_price if favorable else price >= stop_price
    if hit_stop:
        return close_position(
            db, trading_session, position, ExitReason.STOP, float(stop_price), broker=broker
        )

    # 2. Target hit.
    hit_target = price >= target_price if favorable else price <= target_price
    if hit_target:
        return close_position(
            db, trading_session, position, ExitReason.TARGET, float(target_price), broker=broker
        )

    # 3. Structure break: the underlying-index level (opening-range boundary
    # / pullback extreme / EMA9) that justified this setup has been crossed
    # unfavorably — exit even though the option premium hasn't hit its own
    # stop yet. Skipped when either side of the comparison is unavailable
    # (no structure_level set, or no underlying_price supplied).
    if stop_plan.structure_level is not None and underlying_price is not None:
        structure_level = _dec(stop_plan.structure_level)
        underlying = _dec(underlying_price)
        structure_broken = (
            underlying < structure_level if favorable else underlying > structure_level
        )
        if structure_broken:
            return close_position(
                db,
                trading_session,
                position,
                ExitReason.STRUCTURE_BREAK,
                float(price),
                broker=broker,
            )

    # 4. Spread blowout: the option's own liquidity has dried up past a
    # tradeable width — exit at the current price rather than risk being
    # stuck in an illiquid contract waiting for stop/target. Generic
    # (SPREAD_BLOWOUT_PCT), not per-strategy, and skipped when bid/ask aren't
    # supplied (existing tests that only pass tick_price).
    if bid is not None and ask is not None and price > 0:
        spread_pct = _dec(ask - bid) / price
        if spread_pct > SPREAD_BLOWOUT_PCT:
            return close_position(
                db,
                trading_session,
                position,
                ExitReason.SPREAD_BLOWOUT,
                float(price),
                broker=broker,
            )

    # 5. Trail: activate once favorable move reaches the activation price;
    # once active, tighten (never loosen) an independent trailing stop
    # (`trail_plan.current_stop_price`) to lock in TRAIL_LOCK_FRACTION of
    # movement beyond activation, and exit if price pulls back through it.
    # Deliberately never writes the trailed level back onto
    # `stop_plan.stop_price` — that stays the original mandatory stop for
    # the life of the position (checked in step 1 above), so a STOP exit and
    # a TRAIL exit stay distinguishable outcomes instead of the trail
    # silently turning every later exit into a "stop hit".
    if trail_plan is not None and trail_plan.status != TrailPlanStatus.TRIGGERED:
        activation_price = _dec(trail_plan.activation_price)
        activated = price >= activation_price if favorable else price <= activation_price

        if activated:
            gain_beyond_activation = (
                (price - activation_price) if favorable else (activation_price - price)
            )
            locked_gain = gain_beyond_activation * _dec(trail_plan.trail_value)
            new_trail_stop = (
                activation_price + locked_gain if favorable else activation_price - locked_gain
            )

            current = (
                _dec(trail_plan.current_stop_price)
                if trail_plan.current_stop_price is not None
                else None
            )
            tightened = current is None or (
                new_trail_stop > current if favorable else new_trail_stop < current
            )
            if tightened:
                trail_plan.current_stop_price = float(new_trail_stop)
                trail_plan.status = TrailPlanStatus.ACTIVE
                trail_plan.updated_at = _utcnow()
                db.add(trail_plan)
                db.flush()
            else:
                new_trail_stop = current if current is not None else new_trail_stop

            # Strict inequality: on the very tick the trail activates (or
            # tightens), new_trail_stop is derived from this same price, so
            # they can be equal — a <= here would fire a spurious exit on
            # the activation tick itself instead of only once price later
            # actually pulls back through the trailed level.
            hit_trail = price < new_trail_stop if favorable else price > new_trail_stop
            if hit_trail:
                return close_position(
                    db,
                    trading_session,
                    position,
                    ExitReason.TRAIL,
                    float(new_trail_stop),
                    broker=broker,
                )

    return None
