"""Reconciliation Service, built now against the paper-vs-local case so
Phase 6's real-broker case is additive, not a rewrite: `run_reconciliation`
diffs the local `positions` table (grouped by contract symbol, net signed
qty) against `broker.get_positions()` — a genuine broker-side position book
even in paper mode, since `execution_engine.paper.service` dispatches
through the same `BrokerPort.place_order`/`get_positions()` calls a real
adapter would (see the Phase 3 plan's "Key design decision"). Any mismatch
is recorded in `broker_sync_states`, logged as a `ReconciliationRun`, and
raises a `SystemAlert` — and, matching the state-machine's transition table
exactly (`ALLOWED_TRANSITIONS` only wires `RECONCILIATION_LOCK` in from
`paper_plus_guarded_live`/`live_enabled`), escalates to `reconciliation_lock`
only from those two modes. A `paper_only` session has no live money at risk,
so a mismatch there is flagged and left running, not blocked.

**2026-08-19**: per-strategy paper/live graduation means a single session
can now hold open positions in both modes at once. `run_reconciliation`
itself only ever compares against *one* broker per call, so
`_local_net_qty_by_symbol` is now scoped to the matching `Order.mode` —
otherwise the other mode's positions would show up as phantom local-only
mismatches. `run_full_reconciliation` is the account-wide entry point that
runs both passes (paper always; live whenever a real broker is connected)
so periodic/manual/startup callers actually cover both books instead of
whichever one a session-level, no-strategy-context broker resolution
happens to prefer (see that function's own docstring).
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.db.base import utcnow as _utcnow
from app.core.modes.state_machine import transition_mode
from app.domain.audit.models import ActorType, EventCategory
from app.domain.broker.models import BrokerSyncState, ReconciliationRun, ReconciliationTrigger
from app.domain.execution.models import Order, OrderMode, OrderSide, Position, PositionStatus
from app.domain.market.models import OptionContract
from app.domain.ops.models import AlertSeverity
from app.domain.session.models import SafeMode, TradingSession, TransitionTriggerType
from app.modules.alerting.manager import send_alert
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.composition import (
    get_broker,
    get_execution_mock,
    is_execution_broker_connected,
    is_execution_broker_live,
)

# Only these two modes ever escalate to reconciliation_lock — matches
# ALLOWED_TRANSITIONS in app.core.modes.transitions exactly.
_RECONCILIATION_LOCK_ELIGIBLE_MODES = frozenset(
    {SafeMode.PAPER_PLUS_GUARDED_LIVE, SafeMode.LIVE_ENABLED}
)


def _local_net_qty_by_symbol(
    db: Session, trading_session_id: uuid.UUID, order_mode: OrderMode
) -> dict[str, tuple[int, uuid.UUID]]:
    """symbol -> (net signed qty, option_contract_id), for open positions
    whose *opening* order was placed in `order_mode` only. Net rather than
    a simple count: a workspace could in principle hold offsetting
    positions on the same contract from different trade_intents (not
    expected given the same-strike lock, but reconciliation should reflect
    the true net exposure, not assume the lock always held).

    **Mode-scoped since 2026-08-19**: a session can now hold both paper and
    live positions at once (per-strategy graduation). Without this filter,
    a live-only comparison (real broker vs. every local position) would see
    paper positions as phantom local-only holdings, and vice versa for a
    paper-only comparison — a false mismatch on a real-money session.
    `Position.opening_order_id` is non-nullable, so every position has
    exactly one unambiguous mode to join through.
    """
    result: dict[str, tuple[int, uuid.UUID]] = {}
    positions = (
        db.query(Position)
        .join(Order, Order.id == Position.opening_order_id)
        .filter(
            Position.trading_session_id == trading_session_id,
            Position.status == PositionStatus.OPEN,
            Order.mode == order_mode,
        )
        .all()
    )
    for position in positions:
        option_contract = db.get(OptionContract, position.option_contract_id)
        if option_contract is None:
            continue
        signed_qty = position.qty if position.side == OrderSide.BUY else -position.qty
        existing_qty, _ = result.get(option_contract.symbol, (0, option_contract.id))
        result[option_contract.symbol] = (existing_qty + signed_qty, option_contract.id)
    return result


def run_reconciliation(
    db: Session,
    broker: BrokerPort,
    trading_session: TradingSession,
    trigger_type: ReconciliationTrigger,
) -> ReconciliationRun:
    started_at = _utcnow()

    order_mode = OrderMode.LIVE if is_execution_broker_live(broker) else OrderMode.PAPER
    local_by_symbol = _local_net_qty_by_symbol(db, trading_session.id, order_mode)
    broker_by_symbol = {p.contract_symbol: p.qty for p in broker.get_positions()}

    mismatches: list[dict[str, object]] = []
    for symbol in set(local_by_symbol) | set(broker_by_symbol):
        local_qty, option_contract_id = local_by_symbol.get(symbol, (0, None))
        broker_qty = broker_by_symbol.get(symbol, 0)
        is_mismatched = local_qty != broker_qty

        if option_contract_id is not None:
            sync_state = (
                db.query(BrokerSyncState)
                .filter(
                    BrokerSyncState.trading_session_id == trading_session.id,
                    BrokerSyncState.option_contract_id == option_contract_id,
                )
                .one_or_none()
            )
            if sync_state is None:
                sync_state = BrokerSyncState(
                    id=uuid.uuid4(),
                    workspace_id=trading_session.workspace_id,
                    trading_session_id=trading_session.id,
                    option_contract_id=option_contract_id,
                    local_qty=local_qty,
                    broker_qty=broker_qty,
                    is_mismatched=is_mismatched,
                    checked_at=_utcnow(),
                )
            else:
                sync_state.local_qty = local_qty
                sync_state.broker_qty = broker_qty
                sync_state.is_mismatched = is_mismatched
                sync_state.checked_at = _utcnow()
            db.add(sync_state)
            db.flush()

        if is_mismatched:
            mismatches.append(
                {
                    "symbol": symbol,
                    "local_qty": local_qty,
                    "broker_qty": broker_qty,
                    "option_contract_id": str(option_contract_id) if option_contract_id else None,
                }
            )

    finished_at = _utcnow()
    action_taken = "none"

    if mismatches:
        send_alert(
            db,
            workspace_id=trading_session.workspace_id,
            trading_session_id=trading_session.id,
            severity=AlertSeverity.CRITICAL,
            category="reconciliation_mismatch",
            message=f"Reconciliation found {len(mismatches)} local-vs-broker mismatch(es).",
            payload={"mismatches": mismatches},
            # order_mode (computed above) is which book this pass actually
            # reconciled -- a paper-book mismatch stays DB-only (no live
            # money at risk, per this module's own docstring), only a
            # live-book mismatch pushes to Telegram.
            mode=order_mode,
            dedup_key=f"reconciliation_mismatch:{trading_session.id}:{order_mode.value}",
        )

        current_mode = SafeMode(trading_session.mode)
        if current_mode in _RECONCILIATION_LOCK_ELIGIBLE_MODES:
            transition_mode(
                db,
                trading_session,
                SafeMode.RECONCILIATION_LOCK,
                TransitionTriggerType.RECONCILIATION,
                reason=f"reconciliation mismatch: {mismatches}",
            )
            action_taken = "reconciliation_lock_entered"
        else:
            action_taken = "alert_raised"

        record_event(
            db,
            workspace_id=trading_session.workspace_id,
            actor_type=ActorType.SYSTEM,
            event_category=EventCategory.BROKER_RECONCILIATION,
            event_type="reconciliation.mismatch_detected",
            trading_session_id=trading_session.id,
            payload={"mismatches": mismatches, "action_taken": action_taken},
        )

    run = ReconciliationRun(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        trigger_type=trigger_type,
        mismatches_found=len(mismatches),
        action_taken=action_taken,
        detail={"mismatches": mismatches},
        started_at=started_at,
        finished_at=finished_at,
    )
    db.add(run)
    db.flush()
    return run


def run_full_reconciliation(
    db: Session,
    trading_session: TradingSession,
    trigger_type: ReconciliationTrigger,
) -> list[ReconciliationRun]:
    """The account-wide reconciliation entry point — always runs the paper
    pass (against the persistent execution mock) and additionally runs the
    live pass (against whatever real broker is currently connected)
    whenever one is. Use this instead of calling `run_reconciliation`
    directly with a single, session-resolved broker for any "check this
    whole session" call site (periodic polling, manual trigger, startup
    recovery) — `get_execution_broker(trading_session)` with no
    `strategy_run` only ever resolves the real broker when the *entire*
    session is `live_enabled`; in `paper_plus_guarded_live` (per-strategy
    graduation) it always resolves the mock, so a single-broker call would
    silently never check the real broker's book at all while a
    live-graduated strategy could be holding real positions.

    Deliberately unconditional on the paper pass (no skipping it just
    because this session currently has zero known local paper positions) —
    an orphan stray paper position is exactly the kind of thing
    reconciliation exists to catch, symmetric to why the live pass always
    runs when connected regardless of known local live positions. This also
    preserves the existing "always writes at least one heartbeat run, even
    when clean" behavior callers already rely on.

    The two event-triggered calls inside `dispatch_trade_intent`/
    `close_position` deliberately keep calling `run_reconciliation` directly
    with the one broker just used for that dispatch/close — each is already
    correctly scoped to the one mode that just changed, and doesn't need to
    re-check the other side at that exact instant.
    """
    runs = [run_reconciliation(db, get_execution_mock(), trading_session, trigger_type)]
    if is_execution_broker_connected():
        runs.append(run_reconciliation(db, get_broker(), trading_session, trigger_type))
    return runs
