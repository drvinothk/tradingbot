"""Read-only visibility into the real Order/Position lifecycle Phase 3
introduces — `GET /orders` and `GET /positions`, both scoped to a caller-
supplied `trading_session_id` the requesting user actually owns (same
workspace-scoping discipline every other lookup in this codebase follows).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.core.pnl import signed_pnl
from app.core.security.rbac import require_permission
from app.domain.audit.models import ActorType, EventCategory
from app.domain.execution.models import (
    ExitReason,
    Order,
    Position,
    PositionStatus,
    StopPlan,
    TradeOutcome,
    TrailPlan,
)
from app.domain.identity.models import User
from app.domain.market.models import OptionContract, QuoteTick
from app.domain.session.models import TradingSession
from app.domain.strategy.models import StrategyConfig, StrategyRun, TradeIntent
from app.modules.audit_service.service import record_event
from app.modules.market_data.freshness import TICK_THRESHOLDS, FreshnessState, classify_age
from app.modules.scheduler.eod_square_off import (
    UnresolvableOptionContractError,
    run_single_position_square_off,
)

router = APIRouter(tags=["execution"])


def _latest_ticks(
    db: Session, option_contract_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, tuple[float, datetime]]:
    """Most recent `(ltp, ts)` already persisted per contract by market-data
    ingestion -- a plain DB read, never a fresh broker call, so polling
    `GET /positions` every few seconds from the frontend can never itself
    trigger broker traffic (a real rate-limit concern in this codebase's own
    history -- see CLAUDE.md's "rate-limiter capacity fix" entry).

    Batched into one `DISTINCT ON` round-trip over every open position's
    `option_contract_id` at once, reusing `ix_quote_ticks_option_contract_ts`
    -- replaces a previous per-position query that ran once per open
    position on every `GET /positions` poll (an N+1 that scaled with open
    position count, not a fixed cost). Contracts with no tick at all (e.g.
    right after a restart, before ingestion resubscribes) are simply absent
    from the returned dict.
    """
    ids = list(option_contract_ids)
    if not ids:
        return {}
    rows = (
        db.query(QuoteTick.option_contract_id, QuoteTick.ltp, QuoteTick.ts)
        .filter(QuoteTick.option_contract_id.in_(ids))
        .order_by(QuoteTick.option_contract_id, QuoteTick.ts.desc())
        .distinct(QuoteTick.option_contract_id)
        .all()
    )
    return {row.option_contract_id: (float(row.ltp), row.ts) for row in rows}


def _get_session_or_404(db: Session, user: User, session_id: uuid.UUID) -> TradingSession:
    trading_session = (
        db.query(TradingSession)
        .filter(
            TradingSession.id == session_id,
            TradingSession.workspace_id == user.workspace_id,
        )
        .one_or_none()
    )
    if trading_session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trading session not found")
    return trading_session


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

    model_config = {"from_attributes": True}


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

    model_config = {"from_attributes": True}


def _order_out(
    order: Order,
    contract: OptionContract | None,
    strategy_type: str | None,
) -> OrderOut:
    out = OrderOut.model_validate(order)
    if contract is not None:
        out.contract_symbol = contract.symbol
        out.strike = float(contract.strike)
        out.expiry_date = contract.expiry_date
        out.option_type = str(contract.option_type)
    out.strategy_type = strategy_type
    return out


@router.get("/orders", response_model=list[OrderOut])
def list_orders(
    trading_session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.view")),
) -> list[OrderOut]:
    trading_session = _get_session_or_404(db, user, trading_session_id)
    rows = (
        db.query(Order, OptionContract, StrategyConfig.strategy_type)
        .outerjoin(OptionContract, Order.option_contract_id == OptionContract.id)
        .outerjoin(TradeIntent, Order.trade_intent_id == TradeIntent.id)
        .outerjoin(StrategyRun, TradeIntent.strategy_run_id == StrategyRun.id)
        .outerjoin(StrategyConfig, StrategyRun.strategy_config_id == StrategyConfig.id)
        .filter(Order.trading_session_id == trading_session.id)
        .order_by(Order.submitted_at.desc())
        .all()
    )
    return [_order_out(order, contract, strategy_type) for order, contract, strategy_type in rows]


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
            TradeIntent,
            StopPlan,
            TrailPlan,
            TradeOutcome,
        )
        .join(OptionContract, Position.option_contract_id == OptionContract.id)
        .outerjoin(TradeIntent, Position.trade_intent_id == TradeIntent.id)
        .outerjoin(StrategyRun, TradeIntent.strategy_run_id == StrategyRun.id)
        .outerjoin(StrategyConfig, StrategyRun.strategy_config_id == StrategyConfig.id)
        .outerjoin(StopPlan, StopPlan.position_id == Position.id)
        .outerjoin(TrailPlan, TrailPlan.position_id == Position.id)
        .outerjoin(TradeOutcome, TradeOutcome.position_id == Position.id)
        .filter(Position.trading_session_id == trading_session.id)
        .order_by(Position.opened_at.desc())
        .all()
    )

    # One batched round-trip for every open position's latest tick, instead
    # of one query per position in the loop below -- see `_latest_ticks`'s
    # own docstring.
    open_contract_ids = [
        position.option_contract_id
        for position, *_rest in rows
        if position.status == PositionStatus.OPEN
    ]
    latest_ticks = _latest_ticks(db, open_contract_ids)
    now = datetime.now(UTC)

    result: list[PositionOut] = []
    for position, contract, strategy_type, trade_intent, stop_plan, trail_plan, outcome in rows:
        out = PositionOut.model_validate(position)
        out.contract_symbol = contract.symbol
        out.strike = float(contract.strike)
        out.expiry_date = contract.expiry_date
        out.option_type = str(contract.option_type)
        out.strategy_type = strategy_type
        if trade_intent is not None:
            out.target_price = float(trade_intent.target_price)
        if stop_plan is not None:
            out.stop_price = float(stop_plan.stop_price)
        if trail_plan is not None and trail_plan.current_stop_price is not None:
            out.trail_stop_price = float(trail_plan.current_stop_price)

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
        elif outcome is not None:
            out.exit_price = float(outcome.exit_price)
            out.realized_pnl = float(outcome.realized_pnl)

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
