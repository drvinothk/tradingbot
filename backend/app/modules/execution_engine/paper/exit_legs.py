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

LIVE positions build legs the same way as paper (the gate opened 2026-09-04):
one whole-position carrier `StopPlan` (`build_carrier_stop_plan`) rather than
one broker resting SL-LMT per leg — see that function's own docstring for
why. Per-leg exit orders and the carrier's own fill are both leg-aware end to
end, including late/async resolution (`finalize_all_open_legs_from_one_fill`,
called from `service._apply_resolved_pending_exit_order` and
`service.close_position_from_external_fill`).
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
    StopPlanStatus,
    TradeOutcome,
    TrailPlan,
    TrailPlanStatus,
)
from app.domain.market.models import Instrument, OptionContract, OptionType
from app.domain.ops.models import AlertSeverity
from app.domain.session.models import TradingSession
from app.domain.strategy.exit_legs import allocate_leg_lots_floored, deserialize_exit_legs
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


def compute_position_potential_profit(db: Session, position: Position) -> float | None:
    """Rupees gained if every open leg's (or the position's single) target
    hits right now: Σ max(0, target − entry_price) × qty. Mirrors
    `compute_position_open_risk` above, but is NOT a literal mirror of its
    single-leg data source: that function reads the single-leg stop from
    `StopPlan.stop_price`, but the single-leg *target* was never put on
    `StopPlan` -- it lives on `TradeIntent.target_price` instead (the same
    field `api.v1.execution`'s `PositionOut` building code already reads for
    a non-staged position's own target). `TradeIntent.target_price` is
    non-nullable (unlike a staged leg's own `target_price`, which is
    genuinely optional -- a "runner" leg with no profit target, see
    `PositionExitLeg`'s own comment) so the single-leg case only returns
    `None` if the intent row itself can't be found, which shouldn't happen
    given `Position.trade_intent_id`'s NOT NULL FK -- kept as a defensive
    check, not an expected path.
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
        found_target = False
        for leg in legs:
            if leg.target_price is None:
                continue
            found_target = True
            leg_profit = max(Decimal("0"), _dec(leg.target_price) - _dec(position.entry_price))
            total += leg_profit * leg.qty
        return float(total) if found_target else None

    trade_intent = db.get(TradeIntent, position.trade_intent_id)
    if trade_intent is None:
        return None
    per_unit = max(Decimal("0"), _dec(trade_intent.target_price) - _dec(position.entry_price))
    return float(per_unit * position.qty)


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
    - the position is only 1 lot — a staged exit is meaningless, collapse to a
      single full-qty exit + a WARNING alert.
    - a partial fill that isn't a whole lot multiple — collapse + alert.

    A position with >= 2 lots but fewer lots than legs is **not** collapsed:
    `allocate_leg_lots_floored` keeps the largest-fraction legs (1 lot each,
    excess to the biggest) and drops the rest, with an `exit_legs_reduced`
    alert naming the dropped legs.

    LIVE positions build legs the same way as paper. The caller
    (`_open_position_from_fill`) then creates the carrier `StopPlan` and, for
    LIVE, places the single whole-position resting SL-LMT a hair below the
    worst leg's stop (`is_live` is passed through only so the reduced/collapsed
    alerts carry the right mode).

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

    total_lots = filled_qty // lot_size if lot_size else 0
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
            is_live=is_live,
        )
        return None

    kept_lots, dropped = allocate_leg_lots_floored(
        total_lots, [s.qty_fraction for s in specs]
    )
    if not kept_lots:
        # total_lots <= 1: a single-lot staged position is meaningless — fall
        # back to the legacy single full-qty exit. Expected/benign regardless
        # of mode, so WARNING not CRITICAL.
        _alert_collapsed(
            db,
            trading_session,
            position,
            f"position has {total_lots} lot(s), collapsing to a single full-qty exit "
            f"(config has {len(specs)} exit legs)",
            is_live=False,
        )
        return None

    kept_specs = [s for i, s in enumerate(specs) if i not in set(dropped)]
    if dropped:
        dropped_kinds = ", ".join(specs[i].kind for i in dropped)
        kept_desc = ", ".join(
            f"{s.kind}={lots}L" for s, lots in zip(kept_specs, kept_lots, strict=True)
        )
        _alert_reduced(
            db,
            trading_session,
            position,
            f"position has {total_lots} lot(s); staged across {len(kept_specs)} of "
            f"{len(specs)} legs ({kept_desc}), dropped: {dropped_kinds}",
            is_live=is_live,
        )

    now = _utcnow()
    entry_price = _dec(position.entry_price)
    base_target = _dec(trade_intent.target_price)
    legs: list[PositionExitLeg] = []
    for idx, (spec, lots) in enumerate(zip(kept_specs, kept_lots, strict=True)):
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
    """`is_live` drives both `severity` and `mode`, not just the message text
    — CRITICAL/`mode=LIVE` (eligible for Telegram once `exit_legs_collapsed`
    is on `TELEGRAM_ALLOWED_CATEGORIES`; all other gates — window, dedup —
    still apply as normal) when `is_live` is True, else WARNING/`mode=PAPER`
    (never pushed to Telegram). Found via a 2026-08-30 QC pass: every call
    site previously hardcoded `mode=OrderMode.PAPER` regardless of which case
    fired, silently making the LIVE case unable to ever reach Telegram even
    after allowlisting, since `send_alert` always paper-suppresses a
    `mode=PAPER` alert.

    Since the Part-3b LIVE gate opened, callers choose `is_live` per reason,
    not just per position: the "position too small" (1-lot) collapse is
    always WARNING/PAPER regardless of the position's real mode — it's the
    LIVE default sizing (1 lot), so treating it as a real-money-risk event
    would make it the single noisiest Telegram category in the app. "fill not
    a lot multiple" passes the position's real `is_live` — a genuinely
    anomalous fill (not just routine 1-lot sizing) on a real-money position
    still gets CRITICAL/LIVE treatment.
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


def _alert_reduced(
    db: Session, trading_session: TradingSession, position: Position, why: str, *, is_live: bool
) -> None:
    """The staged exit *did* run, just with fewer legs than configured because
    the position had fewer lots than legs (`allocate_leg_lots_floored` kept the
    largest-fraction legs, dropped the rest). Always WARNING — an expected,
    benign fallback, not a real-money-risk event like a full
    `exit_legs_collapsed`. Deliberately *not* on `TELEGRAM_ALLOWED_CATEGORIES`,
    so it stays a dashboard/DB record regardless of `mode`.
    """
    logger.info("exit_legs reduced for position %s: %s", position.id, why)
    send_alert(
        db,
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        severity=AlertSeverity.WARNING,
        category="exit_legs_reduced",
        message=(
            f"Staged exit for position {position.id} ran with fewer legs than "
            f"configured ({why})."
        ),
        mode=OrderMode.LIVE if is_live else OrderMode.PAPER,
        dedup_key=f"exit_legs_reduced:{position.id}",
    )


# --- carrier (whole-position) protective stop ------------------------------

# How far below the worst (lowest) leg stop the single whole-position carrier
# SL-LMT sits. Keeping it *below* every leg stop means the ~3s app poll always
# closes a leg on its own stop first, so the carrier only ever fires when the
# poll is dead (crash / broker disconnect) — it is a backstop, never a
# concurrent second exit. 2% of the worst leg stop is comfortably more than a
# poll cycle of option-premium drift, and small enough that a genuine
# disconnect gives up little extra.
_CARRIER_STOP_EXTRA_MARGIN_PCT = Decimal("0.02")


def build_carrier_stop_plan(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    legs: list[PositionExitLeg],
    option_contract: OptionContract,
    broker: BrokerPort,
    *,
    is_live: bool,
) -> StopPlan | None:
    """The single whole-position resting stop for a legged position. Created in
    BOTH modes so paper's data shape and the resize/cancel code path match
    live; only LIVE places the real broker SL-LMT (`place_protective_stop`).

    Trigger = worst (lowest) leg `stop_price` × (1 - `_CARRIER_STOP_EXTRA_MARGIN_PCT`).
    Returns `None` if no leg carries a `stop_price` (nothing to anchor to — the
    legs are then protected only by their own target/trail/structure/EOD
    backstops, same as a runner leg already is).

    A `StopPlan` on a position that ALSO has `position_exit_legs` is always a
    carrier: it is never an exit-decision input (`evaluate_open_position`
    branches to `evaluate_leg_position` before its own stop check), only a
    holder for `resting_order_id` / `resting_order_price` and a resize anchor.

    Idempotent, mirroring `build_position_exit_legs`'s own guard: if a carrier
    already exists for this position (a retried `_open_position_from_fill` via
    `_apply_resolved_pending_order` — `build_position_exit_legs` itself already
    returns the existing legs rather than duplicating them on that path), this
    returns the existing row unchanged rather than inserting a second one.
    `stop_plans.position_id` is DB-unique, so without this check a retry would
    raise `IntegrityError` (confirmed via a direct repro) instead of no-op'ing
    the way the rest of this entry-fill flow already does.
    """
    existing = db.query(StopPlan).filter(StopPlan.position_id == position.id).one_or_none()
    if existing is not None:
        return existing

    leg_stops = [_dec(lg.stop_price) for lg in legs if lg.stop_price is not None]
    if not leg_stops:
        return None
    trigger = min(leg_stops) * (Decimal("1") - _CARRIER_STOP_EXTRA_MARGIN_PCT)
    now = _utcnow()
    carrier = StopPlan(
        id=uuid.uuid4(),
        position_id=position.id,
        stop_price=float(trigger),
        qty=position.qty,
        structure_level=None,
        status=StopPlanStatus.CONFIRMED,
        created_at=now,
        updated_at=now,
    )
    db.add(carrier)
    db.flush()

    if is_live:
        from app.modules.execution_engine.paper.protective_stop import place_protective_stop

        place_protective_stop(db, trading_session, position, carrier, option_contract, broker)
    return carrier


def _sync_carrier_stop_after_leg_close(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    order_mode: OrderMode,
    broker: BrokerPort,
    *,
    position_now_flat: bool,
) -> None:
    """Keep the carrier stop in step as legs close. Flat position -> retire it
    (LIVE: cancel the broker order). Legs remain -> re-anchor the trigger to
    the worst *still-open* leg stop and shrink to the remaining qty (LIVE:
    `resize_resting_protective_stop`). Paper just keeps the `StopPlan` row
    honest. No-op when there is no carrier (no leg had a stop_price)."""
    carrier = db.query(StopPlan).filter(StopPlan.position_id == position.id).one_or_none()
    if carrier is None:
        return

    from app.modules.execution_engine.paper.protective_stop import (
        cancel_resting_protective_stop,
        resize_resting_protective_stop,
    )

    if position_now_flat:
        if order_mode == OrderMode.LIVE and carrier.resting_order_id is not None:
            cancel_resting_protective_stop(
                db, trading_session, position, carrier, carrier.resting_order_id, broker
            )
        return

    open_stops = [
        _dec(lg.stop_price)
        for lg in db.query(PositionExitLeg).filter(
            PositionExitLeg.position_id == position.id,
            PositionExitLeg.status == PositionExitLegStatus.OPEN,
        )
        if lg.stop_price is not None
    ]
    if not open_stops:
        return
    new_trigger = min(open_stops) * (Decimal("1") - _CARRIER_STOP_EXTRA_MARGIN_PCT)
    if order_mode == OrderMode.LIVE and carrier.resting_order_id is not None:
        resize_resting_protective_stop(
            db,
            trading_session,
            position,
            carrier,
            carrier.resting_order_id,
            new_trigger,
            position.qty,
            broker,
        )
    else:
        carrier.stop_price = float(new_trigger)
        carrier.qty = position.qty
        carrier.updated_at = _utcnow()
        db.add(carrier)
        db.flush()


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
    from app.modules.execution_engine.paper.service import (
        _MAX_EXIT_ORDER_ATTEMPTS,
        _resolve_order_pricing,
    )

    option_contract = db.get(OptionContract, position.option_contract_id)
    if option_contract is None:
        raise ValueError(f"unknown option_contract_id {position.option_contract_id}")
    instrument = db.get(Instrument, option_contract.instrument_id)
    if instrument is None:
        raise ValueError(f"unknown instrument for option_contract {option_contract.id}")

    exit_side = _opposite(SignalSide(position.side))
    now = _utcnow()
    # Per-leg retry, mirroring close_position's single-exit fix (206b7a0,
    # 2026-09-02): a LIVE leg exit order that comes back CANCELLED/REJECTED
    # must place a *fresh* attempt with a new key, not re-find the dead order
    # forever. Explicit key set (not a LIKE prefix) so `:0` can't be confused
    # with `:0:retryN` matching logic and leg indices never collide.
    base_key = f"exit:{position.id}:{leg.leg_index}"
    attempt_keys = [base_key] + [
        f"{base_key}:retry{n}" for n in range(1, _MAX_EXIT_ORDER_ATTEMPTS)
    ]
    exit_attempts = (
        db.query(Order)
        .filter(
            Order.position_id == position.id,
            Order.idempotency_key.in_(attempt_keys),
        )
        .order_by(Order.submitted_at.desc())
        .all()
    )
    exit_order = exit_attempts[0] if exit_attempts else None
    needs_new_attempt = exit_order is None or exit_order.status in (
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    )

    if needs_new_attempt:
        if len(exit_attempts) >= _MAX_EXIT_ORDER_ATTEMPTS:
            logger.error(
                "position %s leg %s: %d exit order attempts all cancelled/rejected -- "
                "giving up automatic retries, leaving leg OPEN for manual intervention",
                position.id,
                leg.leg_index,
                len(exit_attempts),
            )
            send_alert(
                db,
                workspace_id=trading_session.workspace_id,
                trading_session_id=trading_session.id,
                severity=AlertSeverity.CRITICAL,
                category="exit_order_attempts_exhausted",
                message=(
                    f"Position {position.id} leg {leg.leg_index}: {len(exit_attempts)} exit "
                    f"order attempts all cancelled/rejected; automatic retries exhausted -- "
                    f"needs manual square-off."
                ),
                mode=order_mode,
                dedup_key=f"exit_order_attempts_exhausted:{position.id}:{leg.leg_index}",
            )
            return None

        attempt_key = (
            base_key if exit_order is None else f"{base_key}:retry{len(exit_attempts)}"
        )
        limit_price, broker_order_type, domain_order_type = _resolve_order_pricing(
            order_mode, _dec(intended_price), exit_side, _dec(instrument.tick_size)
        )
        order_result = broker.place_order(
            OrderRequest(
                idempotency_key=attempt_key,
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
            idempotency_key=attempt_key,
            now=now,
            position_id=position.id,
            intended_exit_reason=exit_reason,
        )
        db.add(exit_order)
        db.flush()
        db.add(
            _new_order_event(
                exit_order.id, order_result, event_type="filled", now=now, include_raw_message=True
            )
        )

    # Either found among prior attempts or just created in the needs_new_attempt
    # block (the exhausted-retries case returned already).
    assert exit_order is not None
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

    # Carrier whole-position resting stop upkeep — after `position.qty` has been
    # decremented above, before finalising the position.
    _sync_carrier_stop_after_leg_close(
        db,
        trading_session,
        position,
        order_mode,
        broker,
        position_now_flat=remaining_open is None,
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


def finalize_all_open_legs_from_one_fill(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    exit_order: Order,
    exit_reason: ExitReason,
    order_mode: OrderMode,
    broker: BrokerPort,
) -> TradeOutcome | None:
    """Close every still-OPEN leg of a legged position against ONE already-
    filled `Order` row — used when a single broker-side event accounts for
    the whole remaining position at once:

    - the whole-position carrier stop firing, discovered late by
      `reconcile_pending_live_exit_orders` (`_apply_resolved_pending_exit_
      order`'s `is_protective_stop` branch — the carrier is a *single*
      resting SL-LMT for the whole remaining qty, so its fill closes every
      leg still open, not just one);
    - a recovered external fill for a legged position
      (`close_position_from_external_fill`'s auto-repair / manual-reconcile
      paths — a human closed the whole position directly at the broker).

    Unlike `close_all_open_legs` (EOD / margin-breach / manual square-off),
    this does **not** place any new broker order per leg — the fill already
    happened; every remaining leg is reconciled against it using the same
    `exit_order`/price for each, mirroring `close_all_open_legs`'s own
    "one price, applied to every remaining leg" contract.

    Retires the carrier `StopPlan` first (clears `resting_order_id`, marks
    `TRIGGERED`) so `_finalize_leg_and_maybe_position`'s own per-leg carrier-
    resize/cancel bookkeeping is a pure no-op for the rest of this call —
    there is nothing left to resize/cancel at the broker; the carrier stop
    is either the fill event itself or has already been superseded by it.

    Naturally idempotent: only currently-OPEN legs are touched, so calling
    this again after every leg is already CLOSED (a retried/duplicate
    resolution of the same fill) is a no-op that returns `None`.
    """
    carrier = db.query(StopPlan).filter(StopPlan.position_id == position.id).one_or_none()
    if carrier is not None and carrier.resting_order_id is not None:
        carrier.resting_order_id = None
        carrier.resting_order_price = None
        carrier.status = StopPlanStatus.TRIGGERED
        carrier.updated_at = _utcnow()
        db.add(carrier)
        db.flush()

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
        last_outcome = _finalize_leg_and_maybe_position(
            db, trading_session, position, leg, exit_order, exit_reason, order_mode, None, broker
        )
    return last_outcome


def finalize_leg_from_resolved_exit_order(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    exit_order: Order,
    exit_reason: ExitReason,
    order_mode: OrderMode,
    broker: BrokerPort,
) -> TradeOutcome | None:
    """Late-resolution counterpart to `_close_leg` for a single per-leg exit
    order (`exit:{position_id}:{leg_index}[:retryN]`), discovered
    filled/resolved by `reconcile_pending_live_exit_orders` well after
    `_close_leg_locked` originally placed it (the LIVE ack-timeout /
    async-fill case, same reasoning as the single-exit
    `_apply_resolved_pending_exit_order` this mirrors). Parses `leg_index`
    out of the order's own idempotency key — safe, since only
    `_close_leg_locked` ever constructs this exact key shape
    (`f"exit:{position.id}:{leg.leg_index}"` + an optional `:retryN`), and
    only for a legged position.

    Returns `None` (no-op) if the key can't be parsed, the leg doesn't
    exist, or the leg is already CLOSED (a retried/duplicate resolution of
    the same fill, or a leg that closed via some other path in the
    meantime) — naturally idempotent, same reasoning as
    `finalize_all_open_legs_from_one_fill`.
    """
    leg_index = _leg_index_from_idempotency_key(exit_order.idempotency_key)
    if leg_index is None:
        return None
    leg = (
        db.query(PositionExitLeg)
        .filter(
            PositionExitLeg.position_id == position.id,
            PositionExitLeg.leg_index == leg_index,
        )
        .one_or_none()
    )
    if leg is None or leg.status != PositionExitLegStatus.OPEN:
        return None
    return _finalize_leg_and_maybe_position(
        db, trading_session, position, leg, exit_order, exit_reason, order_mode, None, broker
    )


def _leg_index_from_idempotency_key(key: str) -> int | None:
    """`exit:{position_id}:{leg_index}` or `...:{leg_index}:retry{n}` ->
    `leg_index`; `None` if `key` isn't a per-leg exit key at all (a `stop:`
    carrier key, or a plain `exit:{position_id}` single-exit key with no leg
    segment — that shape never actually reaches a legged position's exit
    orders, see `close_position`'s own `position_has_exit_legs`
    early-return, but parsing defensively rather than assuming costs
    nothing here).
    """
    parts = key.split(":")
    if len(parts) < 3 or parts[0] != "exit":
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None
