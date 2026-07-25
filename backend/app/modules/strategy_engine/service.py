"""Shared plumbing every Strategy implementation reuses to turn a
`TradeProposal` into an audited Signal + TradeIntent and hand it to Risk
Service — this is the one place a Signal/TradeIntent pair gets created, so
every strategy (synthetic now, ORB/VWAP/EMA from Phase 4) goes through
identical bookkeeping.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.domain.audit.models import ActorType, EventCategory
from app.domain.risk.models import RiskDecision
from app.domain.session.models import TradingSession
from app.domain.strategy.models import (
    Signal,
    StrategyConfig,
    StrategyRun,
    SyntheticTradeOutcome,
    TradeIntent,
    TradeIntentStatus,
)
from app.modules.audit_service.service import record_event
from app.modules.risk_engine.service import evaluate_trade_intent, record_synthetic_outcome
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
        status=TradeIntentStatus.PENDING_RISK,
        created_at=now,
    )
    db.add(trade_intent)
    db.flush()

    return evaluate_trade_intent(db, trade_intent, trading_session, strategy_run)


def close_dispatched_trade_intent_synthetically(
    db: Session,
    trading_session: TradingSession,
    trade_intent: TradeIntent,
    rng: random.Random | None = None,
) -> SyntheticTradeOutcome | None:
    """Phase-2-only: closes a just-dispatched TradeIntent with a small
    synthetic P&L, sized as a fraction of the capital Risk already computed
    for it. Shared by both dispatch paths — auto-execute
    (`SyntheticStrategy.run_cycle`) and a human clicking Approve
    (`POST /trade-approvals/{id}/approve`) — so a dispatched position never
    sits open forever with no way to close it, silently occupying a
    concurrency slot and a same-strike lock for the rest of the session. See
    `SyntheticTradeOutcome`'s docstring for why this stand-in exists at all;
    Phase 3's real Execution Service replaces every call site of this
    function with real fill-driven exits.

    Returns `None` (without raising) if the intent isn't actually dispatched
    or has no RiskDecision to size the P&L against — closing a position
    badly is not a reason to leave the caller with an unhandled exception.
    """
    if trade_intent.status != TradeIntentStatus.DISPATCHED:
        return None

    risk_decision = (
        db.query(RiskDecision)
        .filter(RiskDecision.trade_intent_id == trade_intent.id)
        .order_by(RiskDecision.created_at.desc())
        .first()
    )
    if risk_decision is None:
        return None

    rng = rng or random.Random()
    pnl_pct = rng.gauss(0.01, 0.05)
    realized_pnl = round(float(risk_decision.capital_required) * pnl_pct, 2)
    return record_synthetic_outcome(db, trading_session, trade_intent, realized_pnl)
