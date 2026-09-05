"""Read-only visibility into the real Order/Position lifecycle Phase 3
introduces — `GET /orders` and `GET /positions`, both scoped to a caller-
supplied `trading_session_id` the requesting user actually owns (same
workspace-scoping discipline every other lookup in this codebase follows).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.v1._common import get_session_or_404 as _get_session_or_404
from app.core.db.base import utcnow as _utcnow
from app.core.db.session import get_db
from app.core.pnl import signed_pnl
from app.core.security.rbac import require_permission
from app.domain.audit.models import ActorType, EventCategory
from app.domain.execution.models import (
    ExitReason,
    Order,
    OrderSide,
    Position,
    PositionExitLeg,
    PositionExitLegStatus,
    PositionStatus,
    StopPlan,
    TradeOutcome,
    TrailPlan,
)
from app.domain.identity.models import User
from app.domain.market.models import OptionContract
from app.domain.session.models import TradingSession
from app.domain.strategy.models import StrategyConfig, StrategyRun, TradeIntent
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.base.contracts import OrderSide as _ContractOrderSide
from app.modules.broker_adapter.base.contracts import TradeFill
from app.modules.execution_engine.paper.service import close_position_from_external_fill
from app.modules.market_data.freshness import (
    TICK_THRESHOLDS,
    FreshnessState,
    classify_age,
    latest_ticks_by_contract,
)
from app.modules.scheduler.eod_square_off import (
    UnresolvableOptionContractError,
    run_single_position_square_off,
)

router = APIRouter(tags=["execution"])


# Relocated to app.modules.market_data.freshness.latest_ticks_by_contract
# (2026-09-05, so risk_engine.service's Guard 1 check can reuse the same
# batched lookup without the service layer importing from this api layer).
# Kept as a bare re-export: this module's own call site below and
# test_api_execution_and_reports.py both still reference the old name.
_latest_ticks = latest_ticks_by_contract


class OrderOut(BaseModel):
    id: uuid.UUID
    trading_session_id: uuid.UUID
    option_contract_id: uuid.UUID
    trade_intent_id: uuid.UUID | None
    position_id: uuid.UUID | None
    mode: str
    side: str
    order_type: str
    qty: int
    status: str
    filled_qty: int
    avg_fill_price: float | None
    broker_order_id: str
    submitted_at: datetime
    # The rest are additive, read-only lookups joined in below -- none of
    # this changes what an Order *is*, they just save the frontend from
    # needing a second round-trip to resolve option_contract_id/
    # trade_intent_id into anything human-readable. `None` whenever the
    # underlying join has nothing (e.g. an exit order, which has no
    # trade_intent_id, so no strategy_type).
    contract_symbol: str | None = None
    strike: float | None = None
    expiry_date: date | None = None
    option_type: str | None = None
    strategy_type: str | None = None
    # 2026-09-04: the *config's* own name (e.g. "OI_Volume_Conviction"), not
    # just its strategy_type -- two configs of the same type (e.g. "Test"
    # and "Test 4", both oi_volume_confirmed) were otherwise visually
    # indistinguishable in the trade table, both rendering the exact same
    # friendlyTradeLabel. Same join, same None-for-exit-order caveat as
    # strategy_type above.
    strategy_name: str | None = None
    # Order.intended_exit_reason -- picked up automatically by
    # model_validate(order) below since the column name matches, same as
    # mode/side/order_type/status above; declared here only for the response
    # schema. `None` for an entry order, a pre-2026-08-25 row, or the LIVE
    # resting protective stop (which never sets it -- see
    # execution_engine.paper.protective_stop.place_protective_stop; its own
    # order_type='sl_limit' is the frontend's signal for that case instead).
    intended_exit_reason: str | None = None

    model_config = {"from_attributes": True}


class PositionLegOut(BaseModel):
    leg_index: int
    kind: str
    qty: int
    status: str
    stop_price: float | None = None
    target_price: float | None = None
    trail_stop_price: float | None = None
    exit_reason: str | None = None
    realized_pnl: float | None = None
    # PositionExitLeg.slippage -- same signed_pnl formula as the net
    # exit_slippage below, scoped to this one leg. None until the leg closes.
    slippage: float | None = None
    closed_at: datetime | None = None


class PositionOut(BaseModel):
    id: uuid.UUID
    trading_session_id: uuid.UUID
    option_contract_id: uuid.UUID
    trade_intent_id: uuid.UUID
    side: str
    qty: int
    entry_price: float
    status: str
    opened_at: datetime
    closed_at: datetime | None
    # Additive, same reasoning as OrderOut above -- resolves what Today's
    # Trades needs (contract identity, strategy, target/stop/trail, live
    # price, P&L) from data that already exists elsewhere, none of it newly
    # computed/derived beyond the P&L arithmetic itself.
    contract_symbol: str | None = None
    strike: float | None = None
    expiry_date: date | None = None
    option_type: str | None = None
    strategy_type: str | None = None
    # 2026-09-04: see OrderOut.strategy_name's own comment -- identical
    # reasoning, same ambiguity fixed on the position side.
    strategy_name: str | None = None
    target_price: float | None = None
    stop_price: float | None = None
    # The trailed stop level once a trail has activated (StopPlan.stop_price
    # itself never moves -- see that model's own docstring) -- `None` until
    # TrailPlan.status advances past INACTIVE.
    trail_stop_price: float | None = None
    # Last known traded price from `quote_ticks` -- a DB read, never a fresh
    # broker call (see `_latest_ticks`'s own docstring). `None` for a closed
    # position (exit_price/realized_pnl below cover that case instead) or an
    # open one with no tick yet.
    # Open-side slippage (Position.entry_slippage), set once at fill time --
    # positive means a favorable entry fill relative to the intended entry
    # price. Picked up automatically by PositionOut.model_validate(position)
    # below since the column name matches; declared here only for the
    # response schema.
    entry_slippage: float | None = None
    ltp: float | None = None
    # Freshness of `ltp` itself, classified via the same
    # `market_data.freshness` module every other price read in this codebase
    # already goes through (see that module's own docstring) -- `ltp_stale`
    # is `True` once the tick is older than `TICK_THRESHOLDS.degraded_after_
    # seconds` (i.e. not FreshnessState.LIVE), so the frontend can show a
    # staleness indicator instead of silently trusting an arbitrarily old
    # tick for a manual square-off decision. Both `None` when `ltp` itself
    # is `None` -- there's no age to classify.
    ltp_stale: bool | None = None
    ltp_age_seconds: float | None = None
    unrealized_pnl: float | None = None
    exit_price: float | None = None
    realized_pnl: float | None = None
    # Net TradeOutcome.slippage across every closed leg (single value for a
    # single-exit position) -- None while the position is still open (no
    # TradeOutcome row exists yet).
    exit_slippage: float | None = None
    # How the position actually closed (target/stop/trail/manual/eod/...) --
    # `TradeOutcome.exit_reason`, `None` for an open position (nothing has
    # closed it yet) or one with no `TradeOutcome` row at all.
    exit_reason: str | None = None
    # The entry (opening) order's mode -- what actually got fired to the
    # broker when this position was opened, not the session's or strategy's
    # *current* config, which can drift after the fact (a strategy's
    # force_paper override can flip after a position already opened). This
    # is the ground truth for bucketing a position as Live vs Paper.
    mode: str | None = None
    # Multi-leg (staged) exit: per-leg detail when this position has
    # `position_exit_legs` (empty for a normal single-exit position).
    # `realized_pnl`/`slippage`/`exit_reason` above are the net across all
    # legs for a staged position (QC finding 3 — no join fan-out).
    legs: list[PositionLegOut] = []

    model_config = {"from_attributes": True}


def _order_out(
    order: Order,
    contract: OptionContract | None,
    strategy_type: str | None,
    strategy_name: str | None,
) -> OrderOut:
    out = OrderOut.model_validate(order)
    if contract is not None:
        out.contract_symbol = contract.symbol
        out.strike = float(contract.strike)
        out.expiry_date = contract.expiry_date
        out.option_type = str(contract.option_type)
    out.strategy_type = strategy_type
    out.strategy_name = strategy_name
    return out


@router.get("/orders", response_model=list[OrderOut])
def list_orders(
    trading_session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.view")),
) -> list[OrderOut]:
    trading_session = _get_session_or_404(db, user, trading_session_id)
    rows = (
        db.query(Order, OptionContract, StrategyConfig.strategy_type, StrategyConfig.name)
        .outerjoin(OptionContract, Order.option_contract_id == OptionContract.id)
        .outerjoin(TradeIntent, Order.trade_intent_id == TradeIntent.id)
        .outerjoin(StrategyRun, TradeIntent.strategy_run_id == StrategyRun.id)
        .outerjoin(StrategyConfig, StrategyRun.strategy_config_id == StrategyConfig.id)
        .filter(Order.trading_session_id == trading_session.id)
        .order_by(Order.submitted_at.desc())
        .all()
    )
    return [
        _order_out(order, contract, strategy_type, strategy_name)
        for order, contract, strategy_type, strategy_name in rows
    ]


@router.get("/positions", response_model=list[PositionOut])
def list_positions(
    trading_session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.view")),
) -> list[PositionOut]:
    trading_session = _get_session_or_404(db, user, trading_session_id)
    rows = (
        db.query(
            Position,
            OptionContract,
            StrategyConfig.strategy_type,
            StrategyConfig.name,
            TradeIntent,
            StopPlan,
            TrailPlan,
            Order.mode,
        )
        .join(OptionContract, Position.option_contract_id == OptionContract.id)
        .outerjoin(TradeIntent, Position.trade_intent_id == TradeIntent.id)
        .outerjoin(StrategyRun, TradeIntent.strategy_run_id == StrategyRun.id)
        .outerjoin(StrategyConfig, StrategyRun.strategy_config_id == StrategyConfig.id)
        .outerjoin(StopPlan, StopPlan.position_id == Position.id)
        .outerjoin(TrailPlan, TrailPlan.position_id == Position.id)
        # opening_order_id is non-nullable, but an inner join here would
        # silently drop a position if its opening Order row were ever
        # missing -- outerjoin so a data-integrity gap surfaces as mode=None
        # (falls back safely in the frontend) rather than hiding the whole
        # position from the list.
        .outerjoin(Order, Position.opening_order_id == Order.id)
        .filter(Position.trading_session_id == trading_session.id)
        .order_by(Position.opened_at.desc())
        .all()
    )

    # `TradeOutcome` and `PositionExitLeg` are 1:N with Position for a staged
    # (multi-leg) exit, so they are batch-loaded and grouped here rather than
    # joined into `rows` above, which would fan a staged position out into
    # one result row per leg (QC finding 3).
    position_ids = [position.id for position, *_rest in rows]
    outcomes_by_position: dict[uuid.UUID, list[TradeOutcome]] = {}
    legs_by_position: dict[uuid.UUID, list[PositionExitLeg]] = {}
    if position_ids:
        for oc in (
            db.query(TradeOutcome).filter(TradeOutcome.position_id.in_(position_ids)).all()
        ):
            outcomes_by_position.setdefault(oc.position_id, []).append(oc)
        for lg in (
            db.query(PositionExitLeg)
            .filter(PositionExitLeg.position_id.in_(position_ids))
            .order_by(PositionExitLeg.leg_index)
            .all()
        ):
            legs_by_position.setdefault(lg.position_id, []).append(lg)

    # One batched round-trip for every open position's latest tick, instead
    # of one query per position in the loop below -- see `_latest_ticks`'s
    # own docstring.
    open_contract_ids = [
        position.option_contract_id
        for position, *_rest in rows
        if position.status == PositionStatus.OPEN
    ]
    latest_ticks = _latest_ticks(db, open_contract_ids)
    now = _utcnow()

    result: list[PositionOut] = []
    for (
        position,
        contract,
        strategy_type,
        strategy_name,
        trade_intent,
        stop_plan,
        trail_plan,
        opening_order_mode,
    ) in rows:
        out = PositionOut.model_validate(position)
        out.contract_symbol = contract.symbol
        out.strike = float(contract.strike)
        out.expiry_date = contract.expiry_date
        out.option_type = str(contract.option_type)
        out.strategy_type = strategy_type
        out.strategy_name = strategy_name
        out.mode = str(opening_order_mode) if opening_order_mode is not None else None
        if trade_intent is not None:
            out.target_price = float(trade_intent.target_price)
        if stop_plan is not None:
            out.stop_price = float(stop_plan.stop_price)
        if trail_plan is not None and trail_plan.current_stop_price is not None:
            out.trail_stop_price = float(trail_plan.current_stop_price)

        position_legs = legs_by_position.get(position.id, [])
        out.legs = [
            PositionLegOut(
                leg_index=lg.leg_index,
                kind=lg.kind,
                qty=lg.qty,
                status=str(lg.status),
                stop_price=float(lg.stop_price) if lg.stop_price is not None else None,
                target_price=float(lg.target_price) if lg.target_price is not None else None,
                trail_stop_price=(
                    float(lg.trail_current_stop_price)
                    if lg.trail_current_stop_price is not None
                    else None
                ),
                exit_reason=str(lg.exit_reason) if lg.exit_reason is not None else None,
                realized_pnl=(
                    float(lg.realized_pnl) if lg.realized_pnl is not None else None
                ),
                slippage=float(lg.slippage) if lg.slippage is not None else None,
                closed_at=lg.closed_at,
            )
            for lg in position_legs
        ]
        if position_legs:
            # Staged position: stop/target shown are the first still-open
            # leg's (what a manual square-off decision cares about), and
            # the trailed level is the tightest across active legs.
            open_leg = next(
                (lg for lg in position_legs if lg.status == PositionExitLegStatus.OPEN), None
            )
            if open_leg is not None:
                if open_leg.stop_price is not None:
                    out.stop_price = float(open_leg.stop_price)
                if open_leg.target_price is not None:
                    out.target_price = float(open_leg.target_price)
            trail_levels = [
                float(lg.trail_current_stop_price)
                for lg in position_legs
                if lg.trail_current_stop_price is not None
            ]
            if trail_levels:
                out.trail_stop_price = (
                    max(trail_levels)
                    if position.side == "buy"
                    else min(trail_levels)
                )

        if position.status == PositionStatus.OPEN:
            tick = latest_ticks.get(position.option_contract_id)
            if tick is not None:
                ltp, ts = tick
                out.ltp = ltp
                out.ltp_age_seconds = max((now - ts).total_seconds(), 0.0)
                out.ltp_stale = classify_age(ts, now, TICK_THRESHOLDS) != FreshnessState.LIVE
                # entry_price is a Numeric/Decimal column -- passed through
                # directly rather than round-tripped via float first (see
                # app.core.pnl.signed_pnl's own docstring and CLAUDE.md's
                # Decimal-vs-float rule).
                out.unrealized_pnl = float(
                    signed_pnl(position.entry_price, ltp, position.qty, position.side)
                )
        else:
            position_outcomes = outcomes_by_position.get(position.id, [])
            if position_outcomes:
                out.realized_pnl = sum(float(o.realized_pnl) for o in position_outcomes)
                out.exit_slippage = sum(float(o.slippage) for o in position_outcomes)
                # A single-exit position has exactly one outcome; a staged
                # one has N — report the last leg's fill as the exit price
                # and either its lone reason or a "staged" marker.
                last = max(position_outcomes, key=lambda o: o.closed_at)
                out.exit_price = float(last.exit_price)
                if len(position_outcomes) == 1:
                    out.exit_reason = (
                        last.exit_reason.value
                        if hasattr(last.exit_reason, "value")
                        else last.exit_reason
                    )
                else:
                    out.exit_reason = "staged"

        result.append(out)
    return result


@router.post("/positions/{position_id}/square-off")
def square_off_position(
    position_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.stop")),
) -> dict:
    """The narrower sibling of `POST /sessions/{id}/square-off`
    (`sessions.manual_square_off`), which flattens every open position in a
    session — this closes exactly one. Reuses the identical
    resolve-broker -> price -> `close_position` chain `scheduler
    .eod_square_off` already established for EOD/margin-breach square-off
    (`run_single_position_square_off`), so this endpoint invents no new
    locking, idempotency, or fill logic: `close_position` still runs under
    its own `LOCK_EXECUTION_SINGLETON` and the same `exit:{position.id}`
    idempotency key every other exit path uses, and the broker is still
    resolved per-position via `resolve_broker_for_position` (never a single
    broker assumed for the whole session — see that helper's own
    docstring), so a live-opened position closes through the real broker
    and a paper one through the mock, regardless of the session's current
    mode. Gated on `session.stop`, the same permission bar
    `manual_square_off`/`trigger_kill_switch` already use for this class of
    forced-exit action.

    Pre-checks the position is open *before* attempting the close (belt on
    top of `close_position`'s own idempotent no-op) so an already-closed
    position gets a clean 409 rather than a silent no-op response. A
    `MANUAL_OVERRIDE` audit event is written unconditionally with the
    outcome either way — a user explicitly requesting this is worth
    recording even when the close itself doesn't complete (e.g. exit order
    left unfilled for reconciliation, same as `close_position`'s own
    `SystemAlert` for that case) -- distinct from the `SYSTEM`-actor
    `position.closed` event `close_position` itself always writes on a
    successful close.

    `run_single_position_square_off` can fail two genuinely different ways
    -- an exit order that didn't fill synchronously (a normal timing
    outcome; reconciliation/retry will resolve it) and an unresolvable
    `option_contract_id` (a data-integrity problem retrying will never fix)
    -- see `UnresolvableOptionContractError`'s own docstring. Both are
    surfaced as `success: false` responses (not raised as 5xxs, consistent
    with this endpoint's existing "always tell the user what happened"
    shape), distinguished by `reason` so the frontend/user gets an accurate
    message instead of one misleading "wait and retry" string for both.
    """
    position = (
        db.query(Position)
        .filter(Position.id == position_id, Position.workspace_id == user.workspace_id)
        .one_or_none()
    )
    if position is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    if position.status != PositionStatus.OPEN:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"position is {position.status}, not open"
        )

    trading_session = db.get(TradingSession, position.trading_session_id)
    if trading_session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trading session not found")

    try:
        outcome = run_single_position_square_off(
            db, None, trading_session, position, ExitReason.MANUAL
        )
    except UnresolvableOptionContractError as exc:
        record_event(
            db,
            workspace_id=user.workspace_id,
            actor_type=ActorType.USER,
            actor_id=user.id,
            event_category=EventCategory.MANUAL_OVERRIDE,
            event_type="position.manual_square_off_requested",
            entity_type="position",
            entity_id=position.id,
            trading_session_id=trading_session.id,
            payload={
                "success": False,
                "reason": "unresolvable_option_contract",
                "option_contract_id": str(exc.option_contract_id),
            },
        )
        db.commit()
        return {
            "success": False,
            "position_id": str(position.id),
            "reason": "unresolvable_option_contract",
            "detail": (
                f"position references option_contract_id {exc.option_contract_id}, which "
                "no longer resolves to a real option contract -- a data-integrity problem, "
                "not a timing issue; reconciliation/retry will not fix this on its own"
            ),
        }

    record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        event_category=EventCategory.MANUAL_OVERRIDE,
        event_type="position.manual_square_off_requested",
        entity_type="position",
        entity_id=position.id,
        trading_session_id=trading_session.id,
        payload={
            "success": outcome is not None,
            "realized_pnl": outcome.realized_pnl if outcome is not None else None,
            "exit_price": outcome.exit_price if outcome is not None else None,
        },
    )
    db.commit()

    if outcome is None:
        return {
            "success": False,
            "position_id": str(position.id),
            "reason": "not_filled_synchronously",
            "detail": (
                "exit order did not fill synchronously; "
                "position left open for reconciliation/retry"
            ),
        }
    return {
        "success": True,
        "position_id": str(position.id),
        "exit_price": outcome.exit_price,
        "realized_pnl": outcome.realized_pnl,
        "slippage": outcome.slippage,
        "exit_reason": outcome.exit_reason.value
        if hasattr(outcome.exit_reason, "value")
        else outcome.exit_reason,
        "closed_at": outcome.closed_at.isoformat(),
    }


class ManualReconcileRequest(BaseModel):
    exit_price: float


@router.post("/positions/{position_id}/manual-reconcile")
def manual_reconcile_position(
    position_id: uuid.UUID,
    body: ManualReconcileRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("risk.override")),
) -> dict:
    """Fallback path, built 2026-09-02, for a position stuck OPEN that
    neither `close_position`'s own retry logic nor reconciliation's
    auto-repair (`reconciliation.service._attempt_auto_repair`, which tries
    `BrokerPort.get_recent_trades` first -- always prefer that over this)
    could resolve on their own -- e.g. the broker's order history doesn't
    have a matching fill (already rolled off, or the position was closed
    through a route that never generated a matching trade record). A human
    who has independently confirmed the real exit price (typically by
    looking directly at the broker's own app or contract note) can close
    the local record with it here, same effect as the one-off SQL
    correction the 2026-09-02 incident needed by hand, through the UI with
    a proper audit trail instead.

    Gated on `risk.override` -- the same permission bar
    `recover_from_reconciliation_lock` already uses for this class of
    "human is directly correcting system state" action. Always closes the
    position's *entire* remaining qty (`position.qty`) -- a partial manual
    correction is out of scope; a genuinely partial real-world exit belongs
    on the existing multi-leg exit path (`exit_legs.py`), not this one-shot
    repair tool.

    Can 409 for a second reason beyond "already closed" (checked above): a
    race lost against `close_position`'s own retry or reconciliation's
    auto-repair, both of which can close this exact position between this
    endpoint's own OPEN check and `close_position_from_external_fill`'s
    lock acquisition -- see that function's own 2026-09-02 QC-follow-up
    docstring paragraph.
    """
    position = (
        db.query(Position)
        .filter(Position.id == position_id, Position.workspace_id == user.workspace_id)
        .one_or_none()
    )
    if position is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Position not found")
    if position.status != PositionStatus.OPEN:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"position is {position.status}, not open"
        )
    if body.exit_price <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "exit_price must be positive")

    trading_session = db.get(TradingSession, position.trading_session_id)
    if trading_session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trading session not found")

    option_contract = db.get(OptionContract, position.option_contract_id)
    if option_contract is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"position references option_contract_id {position.option_contract_id}, which no "
            "longer resolves -- a data-integrity problem, not something this endpoint can fix",
        )

    # TradeFill.side is app.modules.broker_adapter.base.contracts.OrderSide
    # -- a deliberately separate type from the domain OrderSide used for
    # `position.side` above (see that enum's own docstring on why this
    # codebase keeps them distinct); same "buy"/"sell" string values, so a
    # plain construction from the domain side's value is correct.
    closing_side = (
        _ContractOrderSide.SELL if position.side == OrderSide.BUY else _ContractOrderSide.BUY
    )
    fill = TradeFill(
        broker_order_id="",
        contract_symbol=option_contract.symbol,
        side=closing_side,
        qty=position.qty,
        avg_price=body.exit_price,
        ts=_utcnow(),
    )
    outcome = close_position_from_external_fill(
        db, trading_session, position, fill, exit_reason=ExitReason.MANUAL
    )
    if outcome is None:
        # Lost a race with another closer (close_position's own retry, or
        # reconciliation's auto-repair) between the OPEN check above and
        # close_position_from_external_fill's own lock acquisition -- the
        # position is already closed, just not by this request.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "position was closed by another process in the meantime -- refresh and check its "
            "current state before retrying",
        )

    record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        event_category=EventCategory.MANUAL_OVERRIDE,
        event_type="position.manual_reconcile",
        entity_type="position",
        entity_id=position.id,
        trading_session_id=trading_session.id,
        payload={"exit_price": body.exit_price, "realized_pnl": outcome.realized_pnl},
    )
    db.commit()

    return {
        "success": True,
        "position_id": str(position.id),
        # float(...) -- TradeOutcome.exit_price/realized_pnl are Numeric
        # columns; returned raw they serialize as JSON strings, not numbers
        # (the same latent quirk square_off_position's own response has,
        # just never asserted numerically there -- see CLAUDE.md's
        # "Decimal vs float" rule).
        "exit_price": float(outcome.exit_price),
        "realized_pnl": float(outcome.realized_pnl),
        "exit_reason": outcome.exit_reason.value
        if hasattr(outcome.exit_reason, "value")
        else outcome.exit_reason,
        "closed_at": outcome.closed_at.isoformat(),
    }
