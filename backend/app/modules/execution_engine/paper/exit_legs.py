"""Multi-leg (staged) exit execution — the PAPER-path leg lifecycle.

Only reached when a `Position` has `position_exit_legs` rows, which happens
only when all of: `TradeIntent.exit_legs` carried a spec of >= 2 legs; the
position is paper-routed; and it was large enough to allocate >= 1 lot to
every leg. Otherwise `build_position_exit_legs` returns `None` and the caller
keeps the pre-existing single-`StopPlan`/`TrailPlan` path completely unchanged
(and raises one `exit_legs_collapsed` `SystemAlert` when a spec *was* present
but couldn't be honoured).

Each leg manages its own stop / target / structure-break / spread-blowout /
trail against its own `qty` slice, closes independently
(`_close_leg` → `exit:{position_id}:{leg_index}`), and produces its own
`TradeOutcome` row. The `Position` stays `OPEN` (with `position.qty`
decremented per leg) until the last leg closes, at which point the
once-per-position finalize runs: `position.closed` audit event,
sleep-inhibitor release, and `record_trade_outcome_effects` with P&L summed
across every leg (QC finding 1 — never per leg, which would trip kill_switch
mid-trade / corrupt the loss streak).

LIVE positions never get legs yet — per-leg broker resting SL-LMTs + their
async-fill reconciliation are a deliberately-gated follow-up (see the plan).
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.db.base import utcnow as _utcnow
from app.core.locking import LOCK_EXECUTION_SINGLETON, advisory_lock
from app.core.pnl import signed_pnl
from app.core.sleep_inhibitor import get_sleep_inhibitor
from app.domain.audit.models import ActorType, EventCategory
from app.domain.broker.models import ReconciliationTrigger
from app.domain.execution.models import (
    ExitReason,
    Order,
    OrderMode,
    OrderStatus,
    Position,
    PositionExitLeg,
    PositionExitLegStatus,
    PositionStatus,
    StopPlan,
    TradeOutcome,
    TrailPlan,
    TrailPlanStatus,
)
from app.domain.market.models import Instrument, OptionContract, OptionType
from app.domain.ops.models import AlertSeverity
from app.domain.session.models import TradingSession
from app.domain.strategy.exit_legs import allocate_leg_lots, deserialize_exit_legs
from app.domain.strategy.models import SignalSide, TradeIntent
from app.modules.alerting.manager import send_alert
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.base.contracts import OrderRequest
from app.modules.execution_engine.paper.order_helpers import (
    _dec,
    _new_order,
    _new_order_event,
    _opposite,
    _to_broker_side,
    _to_domain_side,
)

logger = logging.getLogger("app.execution_engine.paper.service")


def position_has_exit_legs(db: Session, position_id: uuid.UUID) -> bool:
    return (
        db.query(PositionExitLeg.id)
        .filter(PositionExitLeg.position_id == position_id)
        .first()
        is not None
    )


def compute_position_open_risk(db: Session, position: Position) -> float | None:
    """Rupees at risk if every open leg's (or the position's single) stop
    hits right now: Σ max(0, entry_price − effective_stop) × qty. `qty` is
    already the absolute, lot-multiplied quantity (`qty = trade_intent
    .qty_lots * instrument.lot_size`, set at dispatch in `paper.service`),
    the same unit `Position.unrealized_pnl`/`realized_pnl` are computed in —
    do not multiply by lot_size again here.

    `effective_stop` prefers an ACTIVE trailing stop over the fixed stop
    price, since that's the tighter, currently-live invalidation level for
    that leg/position. A leg/position with no stop configured at all
    contributes nothing and, if *no* leg/the position has one, this returns
    `None` (not 0) so callers can render "no stop data" instead of a
    misleading "₹0 at risk".
    """
    if position_has_exit_legs(db, position.id):
        legs = (
            db.query(PositionExitLeg)
            .filter(
                PositionExitLeg.position_id == position.id,
                PositionExitLeg.status == PositionExitLegStatus.OPEN,
            )
            .all()
        )
        total = Decimal("0")
        found_stop = False
        for leg in legs:
            effective_stop = (
                leg.trail_current_stop_price
                if leg.trail_status == TrailPlanStatus.ACTIVE
                and leg.trail_current_stop_price is not None
                else leg.stop_price
            )
            if effective_stop is None:
                continue
            found_stop = True
            total += max(Decimal("0"), _dec(position.entry_price) - _dec(effective_stop)) * leg.qty
        return float(total) if found_stop else None

    stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).first()
    if stop_plan is None:
        return None
    trail_plan = db.query(TrailPlan).filter(TrailPlan.position_id == position.id).first()
    effective_stop = (
        trail_plan.current_stop_price
        if trail_plan is not None
        and trail_plan.status == TrailPlanStatus.ACTIVE
        and trail_plan.current_stop_price is not None
        else stop_plan.stop_price
    )
    risk = max(Decimal("0"), _dec(position.entry_price) - _dec(effective_stop)) * stop_plan.qty
    return float(risk)


def build_position_exit_legs(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    trade_intent: TradeIntent,
    *,
    filled_qty: int,
    lot_size: int,
    is_live: bool,
) -> list[PositionExitLeg] | None:
    """Create `position_exit_legs` for a fresh entry fill, or return `None`
    (caller keeps the legacy single-exit path). `None` cases:

    - no `trade_intent.exit_legs` spec at all — the common case, unchanged.
    - the position is LIVE — collapse + alert (gated follow-up).
    - the position is too small to give every leg >= 1 lot — collapse + alert.

    Idempotent: if legs already exist for this position (a retried
    `_open_position_from_fill` via `_apply_resolved_pending_order`), returns
    the existing rows without creating duplicates.
    """
    if position_has_exit_legs(db, position.id):
        return list(
            db.query(PositionExitLeg)
            .filter(PositionExitLeg.position_id == position.id)
            .order_by(PositionExitLeg.leg_index)
        )

    specs = deserialize_exit_legs(trade_intent.exit_legs)
    if not specs or len(specs) < 2:
        return None

    if is_live:
        _alert_collapsed(
            db,
            trading_session,
            position,
            "multi-leg staged exit is not yet supported for LIVE positions",
            is_live=True,
        )
        return None

    total_lots = filled_qty // lot_size if lot_size else 0
    per_leg_lots = allocate_leg_lots(total_lots, [s.qty_fraction for s in specs])
    if total_lots < len(specs) or any(lots < 1 for lots in per_leg_lots):
        _alert_collapsed(
            db,
            trading_session,
            position,
            f"position has {total_lots} lot(s), too few to stage across "
            f"{len(specs)} exit legs",
            is_live=False,
        )
        return None
    if total_lots * lot_size != filled_qty:
        # A partial fill that isn't a clean lot multiple would leave the
        # legs' summed qty short of `position.qty`, so `position.qty` could
        # never decrement to 0 and the position would never finalize.
        # Shouldn't happen on paper (mock fills whole), but collapse rather
        # than create an un-closeable staged position.
        _alert_collapsed(
            db,
            trading_session,
            position,
            f"filled qty {filled_qty} is not a whole multiple of lot size {lot_size}",
            is_live=False,
        )
        return None

    now = _utcnow()
    entry_price = _dec(position.entry_price)
    base_target = _dec(trade_intent.target_price)
    legs: list[PositionExitLeg] = []
    for idx, (spec, lots) in enumerate(zip(specs, per_leg_lots, strict=True)):
        # Trail activation price: same shape as the legacy path
        # (_open_position_from_fill) — a fraction of the entry->target
        # distance, defaulting to the generic 0.5 when the leg doesn't
        # specify one. A runner leg (no target) anchors to the signal's base
        # target so it still has a meaningful activation distance.
        act_frac = _dec(
            spec.trail_activation_fraction
            if spec.trail_activation_fraction is not None
            else Decimal("0.5")
        )
        ref_target = _dec(spec.target_price) if spec.target_price is not None else base_target
        act_distance = abs(ref_target - entry_price) * act_frac
        activation_price = (
            entry_price + act_distance
            if position.side == SignalSide.BUY.value
            else entry_price - act_distance
        )
        leg = PositionExitLeg(
            id=uuid.uuid4(),
            position_id=position.id,
            leg_index=idx,
            kind=spec.kind,
            qty=lots * lot_size,
            stop_price=spec.stop_price,
            target_price=spec.target_price,
            structure_level=spec.structure_level,
            structure_break_buffer=spec.structure_break_buffer,
            structure_break_persistence_seconds=spec.structure_break_persistence_seconds,
            trail_activation_fraction=spec.trail_activation_fraction,
            trail_lock_fraction=spec.trail_lock_fraction,
            max_loss_per_lot=spec.max_loss_per_lot,
            time_stop_minutes=spec.time_stop_minutes,
            trail_status=TrailPlanStatus.INACTIVE,
            trail_activation_price=float(activation_price),
            status=PositionExitLegStatus.OPEN,
            created_at=now,
            updated_at=now,
        )
        legs.append(leg)
    db.add_all(legs)
    db.flush()

    record_event(
        db,
        workspace_id=trading_session.workspace_id,
        actor_type=ActorType.SYSTEM,
        event_category=EventCategory.ORDER_LIFECYCLE,
        event_type="position.exit_legs_created",
        entity_type="position",
        entity_id=position.id,
        trading_session_id=trading_session.id,
        payload={"legs": [{"leg_index": leg.leg_index, "qty": leg.qty} for leg in legs]},
    )
    return legs


def _alert_collapsed(
    db: Session, trading_session: TradingSession, position: Position, why: str, *, is_live: bool
) -> None:
    """`is_live` drives both `severity` and `mode`, not just the message text.
    The two paper-only collapse reasons (position too small, fill not a lot
    multiple) are a harmless, expected fallback — WARNING, `mode=PAPER`,
    never pushed to Telegram, unchanged from before. The LIVE-position
    reason is different in kind: a strategy's staged-exit *risk config* was
    silently ignored on a real-money position, which the operator should
    actually be told about — CRITICAL, `mode=LIVE`, eligible for Telegram
    once `exit_legs_collapsed` is on `TELEGRAM_ALLOWED_CATEGORIES` (all
    other gates — window, dedup — still apply as normal). Found via a 2026-
    08-30 QC pass: every call site previously hardcoded `mode=OrderMode
    .PAPER` regardless of which case fired, silently making the LIVE case
    unable to ever reach Telegram even after allowlisting, since `send_alert`
    always paper-suppresses a `mode=PAPER` alert.
    """
    logger.warning("exit_legs collapsed for position %s: %s", position.id, why)
    send_alert(
        db,
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        severity=AlertSeverity.CRITICAL if is_live else AlertSeverity.WARNING,
        category="exit_legs_collapsed",
        message=(
            f"Staged exit config ignored for position {position.id}; "
            f"using a single full-qty exit instead ({why})."
        ),
        mode=OrderMode.LIVE if is_live else OrderMode.PAPER,
        dedup_key=f"exit_legs_collapsed:{position.id}",
    )


# --- evaluation -------------------------------------------------------------

# Mirror of service.py's module constants — kept local so this module has no
# import cycle back into service.py at module load. Same values.
_TRAIL_LOCK_FRACTION_DEFAULT = Decimal("0.5")
_SPREAD_BLOWOUT_PCT = Decimal("0.30")


def evaluate_leg_position(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    tick_price: float,
    broker: BrokerPort | None,
    bid: float | None,
    ask: float | None,
    underlying_price: float | None,
) -> TradeOutcome | None:
    """Leg-aware counterpart of `service.evaluate_open_position`. Runs the
    same five checks (stop / target / structure-break / spread-blowout /
    trail) once per still-OPEN leg, against that leg's own config, closing
    each leg independently. Returns the last leg-close `TradeOutcome`
    produced this cycle (or `None`); as with the legacy function, a non-None
    return does **not** mean the whole position closed.
    """
    if position.status != PositionStatus.OPEN:
        return None
    open_legs = (
        db.query(PositionExitLeg)
        .filter(
            PositionExitLeg.position_id == position.id,
            PositionExitLeg.status == PositionExitLegStatus.OPEN,
        )
        .order_by(PositionExitLeg.leg_index)
        .all()
    )
    if not open_legs:
        return None

    side = SignalSide(position.side)
    favorable = side == SignalSide.BUY
    price = _dec(tick_price)
    last_outcome: TradeOutcome | None = None

    # Resolved once per position (all legs share the same
    # `option_contract_id`), not once per leg per cycle as the pre-fix code
    # effectively did. `None` when `underlying_price` itself is unavailable
    # this cycle, or the lookup somehow fails (unreachable in practice,
    # FK-protected) -- `_check_leg` treats that identically to "no
    # structure_level set on this leg" and skips its structure-break check.
    structure_option_contract = (
        db.get(OptionContract, position.option_contract_id)
        if underlying_price is not None
        else None
    )

    for leg in open_legs:
        outcome = _check_leg(
            db,
            trading_session,
            position,
            leg,
            price,
            favorable,
            structure_option_contract,
            bid,
            ask,
            underlying_price,
            broker,
        )
        if outcome is not None:
            last_outcome = outcome
    return last_outcome


def _check_leg(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    leg: PositionExitLeg,
    price: Decimal,
    favorable: bool,
    structure_option_contract: OptionContract | None,
    bid: float | None,
    ask: float | None,
    underlying_price: float | None,
    broker: BrokerPort | None,
) -> TradeOutcome | None:
    from app.modules.execution_engine.paper.service import (
        _structure_break_confirmed_by_bar_close,
    )

    # 1. Stop (leg may have none — a runner leg without an explicit stop).
    if leg.stop_price is not None:
        stop_price = _dec(leg.stop_price)
        hit_stop = price <= stop_price if favorable else price >= stop_price
        if hit_stop:
            return _close_leg(
                db, trading_session, position, leg, ExitReason.STOP, float(stop_price), broker
            )

    # 2. Target (None => runner leg, skip).
    if leg.target_price is not None:
        target_price = _dec(leg.target_price)
        hit_target = price >= target_price if favorable else price <= target_price
        if hit_target:
            return _close_leg(
                db, trading_session, position, leg, ExitReason.TARGET, float(target_price), broker
            )

    # 3. Structure break — candidate/confirm/reclaim on the leg's own row.
    #
    # Uses `structure_favorable` (CE/PE-derived from the position's real
    # option_type), NOT `favorable` (side == BUY, always True here) -- same
    # reasoning as `service.evaluate_open_position`'s identical fix: this
    # check compares against the *underlying's* spot price, where favorable
    # direction depends on CE vs PE, unlike the premium-based stop/target
    # checks above where `favorable` is correctly direction-agnostic.
    if (
        leg.structure_level is not None
        and underlying_price is not None
        and structure_option_contract is not None
    ):
        structure_favorable = structure_option_contract.option_type == OptionType.CE
        structure_level = _dec(leg.structure_level)
        underlying = _dec(underlying_price)
        buffer = _dec(leg.structure_break_buffer or 0)
        persistence_seconds = float(leg.structure_break_persistence_seconds or 0)
        buffered_level = (
            structure_level - buffer if structure_favorable else structure_level + buffer
        )
        breached = (
            underlying < buffered_level if structure_favorable else underlying > buffered_level
        )

        if breached:
            if leg.structure_break_candidate_since is None:
                leg.structure_break_candidate_since = _utcnow()
                leg.structure_break_candidate_extreme = underlying_price
            else:
                prior = _dec(leg.structure_break_candidate_extreme)
                worse = underlying < prior if structure_favorable else underlying > prior
                if worse:
                    leg.structure_break_candidate_extreme = underlying_price
            db.add(leg)
            db.flush()

            elapsed = (_utcnow() - leg.structure_break_candidate_since).total_seconds()
            if elapsed >= persistence_seconds:
                if persistence_seconds > 0:
                    confirmed = _structure_break_confirmed_by_bar_close(
                        db,
                        structure_option_contract.instrument_id,
                        buffered_level,
                        structure_favorable,
                    )
                else:
                    confirmed = True
                if confirmed:
                    return _close_leg(
                        db,
                        trading_session,
                        position,
                        leg,
                        ExitReason.STRUCTURE_BREAK,
                        float(price),
                        broker,
                    )
        elif leg.structure_break_candidate_since is not None:
            leg.structure_break_candidate_since = None
            leg.structure_break_candidate_extreme = None
            db.add(leg)
            db.flush()

    # 4. Spread blowout (generic threshold, same as the legacy path).
    if bid is not None and ask is not None and price > 0:
        spread_pct = _dec(ask - bid) / price
        if spread_pct > _SPREAD_BLOWOUT_PCT:
            return _close_leg(
                db, trading_session, position, leg, ExitReason.SPREAD_BLOWOUT, float(price), broker
            )

    # 5. Trail — activate at the leg's activation price, then tighten only.
    if (
        leg.trail_status != TrailPlanStatus.TRIGGERED
        and leg.trail_activation_price is not None
    ):
        activation_price = _dec(leg.trail_activation_price)
        activated = price >= activation_price if favorable else price <= activation_price
        if activated:
            lock_fraction = _dec(
                leg.trail_lock_fraction
                if leg.trail_lock_fraction is not None
                else _TRAIL_LOCK_FRACTION_DEFAULT
            )
            gain_beyond = (
                (price - activation_price) if favorable else (activation_price - price)
            )
            locked_gain = gain_beyond * lock_fraction
            new_trail_stop = (
                activation_price + locked_gain if favorable else activation_price - locked_gain
            )
            current = (
                _dec(leg.trail_current_stop_price)
                if leg.trail_current_stop_price is not None
                else None
            )
            tightened = current is None or (
                new_trail_stop > current if favorable else new_trail_stop < current
            )
            if tightened:
                leg.trail_current_stop_price = float(new_trail_stop)
                leg.trail_status = TrailPlanStatus.ACTIVE
                leg.updated_at = _utcnow()
                db.add(leg)
                db.flush()
            else:
                new_trail_stop = current if current is not None else new_trail_stop

            hit_trail = price < new_trail_stop if favorable else price > new_trail_stop
            if hit_trail:
                return _close_leg(
                    db,
                    trading_session,
                    position,
                    leg,
                    ExitReason.TRAIL,
                    float(new_trail_stop),
                    broker,
                )

    return None


# --- closing --------------------------------------------------------------


def close_all_open_legs(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    exit_reason: ExitReason,
    intended_price: float,
    broker: BrokerPort | None,
) -> TradeOutcome | None:
    """The has-legs branch of `service.close_position` — flatten every
    still-OPEN leg at one shared `intended_price` (EOD / margin-breach /
    manual square-off). One price is passed straight through to each leg;
    no per-leg option-chain refresh (QC finding 7).
    """
    open_legs = (
        db.query(PositionExitLeg)
        .filter(
            PositionExitLeg.position_id == position.id,
            PositionExitLeg.status == PositionExitLegStatus.OPEN,
        )
        .order_by(PositionExitLeg.leg_index)
        .all()
    )
    last_outcome: TradeOutcome | None = None
    for leg in open_legs:
        outcome = _close_leg(
            db, trading_session, position, leg, exit_reason, intended_price, broker
        )
        if outcome is not None:
            last_outcome = outcome
    return last_outcome


def _close_leg(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    leg: PositionExitLeg,
    exit_reason: ExitReason,
    intended_price: float,
    broker: BrokerPort | None,
) -> TradeOutcome | None:
    from app.modules.broker_adapter.composition import is_execution_broker_live
    from app.modules.execution_engine.paper.service import (
        resolve_broker_for_position,
    )

    broker = broker or resolve_broker_for_position(db, trading_session, position)
    order_mode = OrderMode.LIVE if is_execution_broker_live(broker) else OrderMode.PAPER

    with advisory_lock(db, LOCK_EXECUTION_SINGLETON):
        # Re-checked inside the lock: evaluate_leg_position / close_all_open_legs
        # and an EOD/margin sweep can race to close the same leg. Same
        # in-session-object trust as service.close_position's own guard.
        if position.status != PositionStatus.OPEN or leg.status != PositionExitLegStatus.OPEN:
            return None
        return _close_leg_locked(
            db, trading_session, position, leg, exit_reason, intended_price, broker, order_mode
        )


def _close_leg_locked(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    leg: PositionExitLeg,
    exit_reason: ExitReason,
    intended_price: float,
    broker: BrokerPort,
    order_mode: OrderMode,
) -> TradeOutcome | None:
    from app.modules.execution_engine.paper.service import _resolve_order_pricing

    option_contract = db.get(OptionContract, position.option_contract_id)
    if option_contract is None:
        raise ValueError(f"unknown option_contract_id {position.option_contract_id}")
    instrument = db.get(Instrument, option_contract.instrument_id)
    if instrument is None:
        raise ValueError(f"unknown instrument for option_contract {option_contract.id}")

    exit_side = _opposite(SignalSide(position.side))
    now = _utcnow()
    idem = f"exit:{position.id}:{leg.leg_index}"

    exit_order = db.query(Order).filter(Order.idempotency_key == idem).one_or_none()
    if exit_order is None:
        limit_price, broker_order_type, domain_order_type = _resolve_order_pricing(
            order_mode, _dec(intended_price), exit_side, _dec(instrument.tick_size)
        )
        order_result = broker.place_order(
            OrderRequest(
                idempotency_key=idem,
                contract_symbol=option_contract.symbol,
                side=_to_broker_side(exit_side),
                order_type=broker_order_type,
                qty=leg.qty,
                limit_price=limit_price,
                lot_size=instrument.lot_size,
                tag=f"session:{trading_session.id}",
            )
        )
        exit_order = _new_order(
            trading_session,
            option_contract,
            order_result,
            mode=order_mode,
            side=_to_domain_side(exit_side),
            order_type=domain_order_type,
            qty=leg.qty,
            idempotency_key=idem,
            now=now,
            position_id=position.id,
            intended_exit_reason=exit_reason,
        )
        db.add(exit_order)
        db.flush()
        db.add(_new_order_event(exit_order.id, order_result, event_type="filled", now=now))

    if exit_order.status != OrderStatus.FILLED or exit_order.avg_fill_price is None:
        # Same "leave it OPEN for retry" contract as close_position. Paper
        # always fills synchronously via the mock, so this is a safety net,
        # not a path production reaches today.
        logger.error(
            "exit order for position %s leg %s did not fill (status=%s) — leaving leg OPEN",
            position.id,
            leg.leg_index,
            exit_order.status,
        )
        return None

    return _finalize_leg_and_maybe_position(
        db,
        trading_session,
        position,
        leg,
        exit_order,
        exit_reason,
        order_mode,
        intended_price,
        broker,
    )


def _finalize_leg_and_maybe_position(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    leg: PositionExitLeg,
    exit_order: Order,
    exit_reason: ExitReason,
    order_mode: OrderMode,
    intended_price: float | None,
    broker: BrokerPort,
) -> TradeOutcome:
    from app.modules.reconciliation.service import run_reconciliation

    now = _utcnow()
    entry_side = SignalSide(position.side)
    entry_price = _dec(position.entry_price)
    exit_price = _dec(exit_order.avg_fill_price)
    qty = Decimal(leg.qty)
    realized_pnl = signed_pnl(entry_price, exit_price, qty, entry_side)
    slippage = (
        signed_pnl(_dec(intended_price), exit_price, qty, entry_side)
        if intended_price is not None
        else Decimal("0")
    )

    leg.status = PositionExitLegStatus.CLOSED
    leg.closed_at = now
    leg.closing_order_id = exit_order.id
    leg.realized_pnl = float(realized_pnl)
    leg.slippage = float(slippage)
    leg.exit_reason = exit_reason
    if exit_reason == ExitReason.TRAIL:
        leg.trail_status = TrailPlanStatus.TRIGGERED
    leg.updated_at = now
    db.add(leg)

    # Shrink the position's remaining open size so reconciliation
    # (_local_net_qty_by_symbol reads position.qty) and unrealized-P&L stay
    # correct while other legs are still open (QC findings 4, 13).
    position.qty = max(0, position.qty - leg.qty)
    db.add(position)

    outcome = TradeOutcome(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        position_id=position.id,
        position_exit_leg_id=leg.id,
        trade_intent_id=position.trade_intent_id,
        entry_price=float(entry_price),
        exit_price=float(exit_price),
        qty=leg.qty,
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
        event_type="position.leg_closed",
        entity_type="position",
        entity_id=position.id,
        trading_session_id=trading_session.id,
        payload={
            "leg_index": leg.leg_index,
            "exit_reason": exit_reason.value,
            "realized_pnl": float(realized_pnl),
            "slippage": float(slippage),
        },
    )

    remaining_open = (
        db.query(PositionExitLeg.id)
        .filter(
            PositionExitLeg.position_id == position.id,
            PositionExitLeg.status == PositionExitLegStatus.OPEN,
        )
        .first()
    )
    if remaining_open is None:
        _finalize_position_after_last_leg(
            db, trading_session, position, exit_order, order_mode
        )

    run_reconciliation(db, broker, trading_session, ReconciliationTrigger.EVENT)
    return outcome


def _finalize_position_after_last_leg(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    last_exit_order: Order,
    order_mode: OrderMode,
) -> None:
    from app.modules.risk_engine.service import record_trade_outcome_effects

    now = _utcnow()
    position.status = PositionStatus.CLOSED
    position.closed_at = now
    position.closing_order_id = last_exit_order.id
    db.add(position)

    get_sleep_inhibitor().release(f"position:{position.id}")

    net_pnl = (
        db.query(PositionExitLeg)
        .filter(PositionExitLeg.position_id == position.id)
        .with_entities(PositionExitLeg.realized_pnl)
        .all()
    )
    total_realized = float(sum((row[0] or 0.0) for row in net_pnl))

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
        payload={"exit_reason": "staged", "realized_pnl": total_realized},
    )

    # Once per position, on the NET P&L across all legs (QC findings 1, 9).
    record_trade_outcome_effects(
        db, trading_session, total_realized, is_live=(order_mode == OrderMode.LIVE)
    )
