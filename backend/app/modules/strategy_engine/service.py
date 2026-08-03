"""Shared plumbing every Strategy implementation reuses to turn a
`TradeProposal` into an audited Signal + TradeIntent and hand it to Risk
Service — this is the one place a Signal/TradeIntent pair gets created, so
every strategy (synthetic now, ORB/VWAP/EMA from Phase 4) goes through
identical bookkeeping.

`submit_signal` also owns the AUTO-mode dispatch handoff: once
`risk_engine.service.evaluate_trade_intent` returns with the TradeIntent
marked DISPATCHED (auto-execute, all limits clear), this calls
`execution_engine.paper.service.dispatch_trade_intent` — deliberately
*after* `evaluate_trade_intent` has already released
`LOCK_RISK_EVALUATION_QUEUE`, so that lock and `dispatch_trade_intent`'s own
`LOCK_EXECUTION_SINGLETON` are never nested. The approval-required path's
equivalent call lives in `api.v1.strategies.approve_trade_approval` instead,
since that's a separate human-initiated request, not part of this cycle.

`expire_stale_pending_approvals` is the proactive half of the approval
workflow the build plan describes ("a background Scheduler job expires
anything left pending past expires_at") — `api.v1.strategies.approve_trade_approval`
only catches a stale approval lazily, as a side effect of someone clicking
Approve on it after the window closed. Without this, an approval nobody
ever clicks on sits `pending` forever instead of actually expiring on its
own; `execution_engine.paper.position_manager.PositionManager` calls this
once per poll cycle for its trading_session, alongside the EOD square-off
and reconciliation checks it already runs there.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.locking import LOCK_EXECUTION_SINGLETON, advisory_lock
from app.domain.audit.models import ActorType, EventCategory
from app.domain.risk.models import RiskDecision
from app.domain.session.models import TradingSession
from app.domain.strategy.models import (
    ApprovalStatus,
    PendingTradeApproval,
    Signal,
    StrategyConfig,
    StrategyRun,
    TradeIntent,
    TradeIntentStatus,
)
from app.modules.audit_service.service import record_event
from app.modules.execution_engine.paper.service import dispatch_trade_intent
from app.modules.risk_engine.service import evaluate_trade_intent
from app.modules.strategy_engine.interface import TradeProposal


def _utcnow() -> datetime:
    return datetime.now(UTC)


def submit_signal(
    db: Session,
    strategy_run: StrategyRun,
    trading_session: TradingSession,
    strategy_config: StrategyConfig,
    proposal: TradeProposal,
) -> RiskDecision:
    now = _utcnow()

    signal = Signal(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        strategy_config_id=strategy_config.id,
        strategy_run_id=strategy_run.id,
        trading_session_id=trading_session.id,
        option_contract_id=proposal.option_contract_id,
        side=proposal.side,
        entry_price=proposal.entry_price,
        stop_price=proposal.stop_price,
        target_price=proposal.target_price,
        qty_lots=proposal.qty_lots,
        trail_activation_fraction=proposal.trail_activation_fraction,
        trail_lock_fraction=proposal.trail_lock_fraction,
        structure_level=proposal.structure_level,
        payload=proposal.payload,
        generated_at=now,
    )
    db.add(signal)
    db.flush()

    record_event(
        db,
        workspace_id=trading_session.workspace_id,
        actor_type=ActorType.SYSTEM,
        event_category=EventCategory.SIGNAL_GENERATION,
        event_type="signal.generated",
        entity_type="signal",
        entity_id=signal.id,
        trading_session_id=trading_session.id,
        strategy_config_id=strategy_config.id,
        payload={
            "option_contract_id": str(proposal.option_contract_id),
            "side": proposal.side.value,
            "qty_lots": proposal.qty_lots,
            "entry_price": proposal.entry_price,
            "stop_price": proposal.stop_price,
            "target_price": proposal.target_price,
        },
    )

    # idempotency_key is one-to-one with the signal that produced it — a
    # strategy calling submit_signal twice for the same signal (it can't;
    # each call mints a new signal) is not the scenario this guards. What it
    # guards is a retried submit_signal call after a crash between the
    # Signal flush above and this TradeIntent insert never producing two
    # trade_intents for the same signal.
    trade_intent = TradeIntent(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        signal_id=signal.id,
        strategy_run_id=strategy_run.id,
        trading_session_id=trading_session.id,
        option_contract_id=proposal.option_contract_id,
        idempotency_key=f"signal:{signal.id}",
        side=proposal.side,
        qty_lots=proposal.qty_lots,
        entry_price=proposal.entry_price,
        stop_price=proposal.stop_price,
        target_price=proposal.target_price,
        trail_activation_fraction=proposal.trail_activation_fraction,
        trail_lock_fraction=proposal.trail_lock_fraction,
        structure_level=proposal.structure_level,
        status=TradeIntentStatus.PENDING_RISK,
        created_at=now,
    )
    db.add(trade_intent)
    db.flush()

    decision = evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)

    # trade_intent.status is mutated in place by evaluate_trade_intent, so
    # this reads the up-to-date value without a re-query. Approval-required
    # dispatch happens separately, from api.v1.strategies.approve_trade_approval.
    if trade_intent.status == TradeIntentStatus.DISPATCHED:
        dispatch_trade_intent(db, trading_session, trade_intent)

    return decision


def expire_stale_pending_approvals(
    db: Session, trading_session: TradingSession
) -> list[PendingTradeApproval]:
    """Finds every still-`pending` `PendingTradeApproval` for this session
    whose `expires_at` has passed and expires it — both the approval and its
    TradeIntent, audited the same way `approve_trade_approval`'s lazy check
    does. An un-acted-on approval must never silently fire later once
    conditions have moved on; this is what makes that actually true instead
    of true-only-if-someone-happens-to-click-it-afterward.

    Wrapped in LOCK_EXECUTION_SINGLETON, same lock
    api.v1.strategies.approve_trade_approval/reject_trade_approval already
    take on this exact table (a Phase 4 QC fix, after a real deadlock from
    two concurrent Approve clicks racing an unlocked check-then-act) - this
    function is called every PositionManager poll cycle and was the one
    remaining unlocked writer to pending_trade_approvals, able to
    interleave with an in-flight Approve/Reject and desync
    TradeIntentStatus from what actually executed.
    """
    now = _utcnow()
    with advisory_lock(db, LOCK_EXECUTION_SINGLETON):
        stale = (
            db.query(PendingTradeApproval)
            .join(StrategyRun, PendingTradeApproval.strategy_run_id == StrategyRun.id)
            .filter(
                StrategyRun.trading_session_id == trading_session.id,
                PendingTradeApproval.status == ApprovalStatus.PENDING,
                PendingTradeApproval.expires_at < now,
            )
            .all()
        )

        expired: list[PendingTradeApproval] = []
        for approval in stale:
            trade_intent = db.get(TradeIntent, approval.trade_intent_id)
            if trade_intent is None:
                continue

            approval.status = ApprovalStatus.EXPIRED
            trade_intent.status = TradeIntentStatus.EXPIRED
            db.add(approval)
            db.add(trade_intent)
            db.flush()

            record_event(
                db,
                workspace_id=trading_session.workspace_id,
                actor_type=ActorType.SYSTEM,
                event_category=EventCategory.RISK_DECISION,
                event_type="pending_trade_approval.expired",
                entity_type="trade_intent",
                entity_id=trade_intent.id,
                trading_session_id=trading_session.id,
            )
            expired.append(approval)

    return expired
