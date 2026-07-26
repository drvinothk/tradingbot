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
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.modes.state_machine import transition_mode
from app.domain.audit.models import ActorType, EventCategory
from app.domain.broker.models import BrokerSyncState, ReconciliationRun, ReconciliationTrigger
from app.domain.execution.models import OrderSide, Position, PositionStatus
from app.domain.market.models import OptionContract
from app.domain.ops.models import AlertSeverity, SystemAlert
from app.domain.session.models import SafeMode, TradingSession, TransitionTriggerType
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.base.broker_port import BrokerPort

# Only these two modes ever escalate to reconciliation_lock — matches
# ALLOWED_TRANSITIONS in app.core.modes.transitions exactly.
_RECONCILIATION_LOCK_ELIGIBLE_MODES = frozenset(
    {SafeMode.PAPER_PLUS_GUARDED_LIVE, SafeMode.LIVE_ENABLED}
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _local_net_qty_by_symbol(
    db: Session, trading_session_id: uuid.UUID
) -> dict[str, tuple[int, uuid.UUID]]:
    """symbol -> (net signed qty, option_contract_id). Net rather than a
    simple count: a workspace could in principle hold offsetting positions
    on the same contract from different trade_intents (not expected given
    the same-strike lock, but reconciliation should reflect the true net
    exposure, not assume the lock always held).
    """
    result: dict[str, tuple[int, uuid.UUID]] = {}
    positions = (
        db.query(Position)
        .filter(
            Position.trading_session_id == trading_session_id,
            Position.status == PositionStatus.OPEN,
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

    local_by_symbol = _local_net_qty_by_symbol(db, trading_session.id)
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
        db.add(
            SystemAlert(
                id=uuid.uuid4(),
                workspace_id=trading_session.workspace_id,
                trading_session_id=trading_session.id,
                severity=AlertSeverity.CRITICAL,
                category="reconciliation_mismatch",
                message=f"Reconciliation found {len(mismatches)} local-vs-broker mismatch(es).",
                payload={"mismatches": mismatches},
                created_at=finished_at,
            )
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
