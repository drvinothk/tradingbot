"""Risk Service. `evaluate_trade_intent` is the one place a TradeIntent gets
approved or rejected — every check runs serialized under
`LOCK_RISK_EVALUATION_QUEUE` (the same advisory-lock pattern the Execution
singleton and mode transitions use) because the concurrency cap, daily trade
count, budget-vs-committed-capital, and same-strike lock are all
check-then-act sequences that would otherwise race if two strategies'
intents were evaluated in parallel.

This module deliberately never imports `app.modules.execution_engine` —
marking a TradeIntent `DISPATCHED` here is as far as Risk Service's
responsibility goes; the caller (`strategy_engine.service.submit_signal` for
the AUTO path, `api.v1.strategies.approve_trade_approval` for
approval-required) is what actually calls
`execution_engine.paper.service.dispatch_trade_intent` next, outside this
module's `LOCK_RISK_EVALUATION_QUEUE` scope. Keeping this one-directional
(execution_engine imports risk_engine for `record_trade_outcome_effects`,
never the other way) avoids a circular import between the two.

Two P&L-driven checks — daily_loss_cap and daily_target_profit — read
`trading_sessions.cumulative_realized_pnl`/`consecutive_losses`, updated by
`record_trade_outcome_effects` below whenever a real `Position` closes (see
`app.modules.execution_engine.paper.service.close_position`). Phase 2's
`record_synthetic_outcome`/`SyntheticTradeOutcome` stand-in — the only way
these counters had data to evaluate before a real Execution Service
existed — is gone; this is its real replacement, same triggers, fed by an
actual fill instead of a random P&L.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_ as sa_or
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.locking import LOCK_RISK_EVALUATION_QUEUE, advisory_lock
from app.core.modes.state_machine import enter_kill_switch
from app.domain.audit.models import ActorType, EventCategory
from app.domain.execution.models import Position, PositionStatus
from app.domain.identity.models import User
from app.domain.market.models import Instrument, OptionContract, OptionType
from app.domain.ops.models import AlertSeverity, SystemAlert
from app.domain.risk.models import RiskDecision, RiskDecisionOutcome, RiskLimitConfig
from app.domain.session.models import (
    EntriesPausedReason,
    FundingMode,
    SafeMode,
    TradingSession,
    TransitionTriggerType,
)
from app.domain.strategy.models import (
    ApprovalStatus,
    ExecutionMode,
    PendingTradeApproval,
    SignalSide,
    StrategyRun,
    TradeIntent,
    TradeIntentStatus,
)
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.base.errors import BrokerError
from app.modules.broker_adapter.composition import get_execution_broker

logger = logging.getLogger("app.risk_engine")

# Stub only — no real broker margin API exists until Phase 5. MTF's actual
# leverage terms are account-specific and broker-supplied; this constant
# exists purely so capital_required is funding-mode-aware from the start per
# the locked architectural decision, not to model real margin math.
MTF_STUB_LEVERAGE_FACTOR = Decimal("5")

# Governance-limit rejection reasons that should raise a visible alert (every
# rejection blocks that trade by definition, but these specifically indicate
# a limit is currently breached / actively blocking further trading, not
# just "this one trade didn't clear pre-trade checks").
_ALERT_WORTHY_REASON_PREFIXES = (
    "mode_blocks_new_entries",
    "entries_paused",
    "same_strike_locked",
    "max_concurrent_positions_reached",
    "max_trades_per_day_reached",
    "consecutive_loss_pause_active",
    "budget_exceeded",
    "per_trade_lot_cap_exceeded",
    "margin_check_failed",
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _dec(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True)
class PreTradeAnalytics:
    capital_required: float
    breakeven_price: float
    pnl_scenarios: dict[str, float] = field(default_factory=dict)


def get_active_risk_limit_config(db: Session, workspace_id: uuid.UUID) -> RiskLimitConfig:
    """Returns the current active version, lazily seeding version 1 from
    `settings.risk_defaults` on first use — `RiskDefaults` are the system
    defaults a workspace's limits start from, not read directly by Risk
    Service checks once a DB-backed config exists.
    """
    config = (
        db.query(RiskLimitConfig)
        .filter(RiskLimitConfig.workspace_id == workspace_id, RiskLimitConfig.is_active.is_(True))
        .order_by(RiskLimitConfig.version.desc())
        .first()
    )
    if config is not None:
        return config

    defaults = get_settings().risk_defaults
    config = RiskLimitConfig(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        version=1,
        is_active=True,
        max_concurrent_positions=defaults.max_concurrent_positions,
        max_trades_per_day=defaults.max_trades_per_day,
        consecutive_loss_pause_threshold=defaults.consecutive_loss_pause_threshold,
        daily_loss_cap=defaults.daily_loss_cap,
        daily_target_profit=defaults.daily_target_profit,
        per_trade_lot_cap=defaults.per_trade_lot_cap,
    )
    db.add(config)
    db.flush()
    return config


def create_new_risk_limit_config_version(
    db: Session,
    workspace_id: uuid.UUID,
    *,
    actor_user: User,
    reason: str = "",
    **overrides: int | float,
) -> RiskLimitConfig:
    """Deactivates the current version and creates version+1 with any
    overridden fields, carrying the rest forward unchanged. Not wired to a
    Phase 2 API route (not in the build plan's Phase 2 endpoint list) — exists
    so "versioned" is a real, testable property of this table now rather than
    a schema comment, ready for an admin endpoint whenever one is built.

    Not concurrency-safe as written: the read-current / deactivate / insert
    sequence below is an unlocked check-then-act, unlike every other
    check-then-act in this module (which all run under
    LOCK_RISK_EVALUATION_QUEUE). Harmless today because nothing calls this
    concurrently — there is no API route yet — but whoever adds the admin
    endpoint must wrap this in an advisory lock (or reuse
    LOCK_RISK_EVALUATION_QUEUE) before two concurrent edits can race into two
    same-version active rows.
    """
    current = get_active_risk_limit_config(db, workspace_id)
    current.is_active = False
    db.add(current)
    db.flush()

    fields: dict[str, int | float] = {
        "max_concurrent_positions": current.max_concurrent_positions,
        "max_trades_per_day": current.max_trades_per_day,
        "consecutive_loss_pause_threshold": current.consecutive_loss_pause_threshold,
        "daily_loss_cap": float(current.daily_loss_cap),
        "daily_target_profit": float(current.daily_target_profit),
        "per_trade_lot_cap": current.per_trade_lot_cap,
    }
    fields.update(overrides)

    new_config = RiskLimitConfig(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        version=current.version + 1,
        is_active=True,
        **fields,
    )
    db.add(new_config)
    db.flush()

    record_event(
        db,
        workspace_id=workspace_id,
        actor_type=ActorType.USER,
        actor_id=actor_user.id,
        event_category=EventCategory.MANUAL_OVERRIDE,
        event_type="risk_limit_config.new_version",
        entity_type="risk_limit_config",
        entity_id=new_config.id,
        payload={"version": new_config.version, "reason": reason, **fields},
    )
    return new_config


def compute_pre_trade_analytics(
    db: Session,
    option_contract: OptionContract,
    *,
    side: SignalSide,
    qty_lots: int,
    entry_price: float,
    stop_price: float,
    target_price: float,
    funding_mode: FundingMode,
) -> PreTradeAnalytics:
    """Capital required = premium x lot_size x qty (lots), funding-mode-aware;
    breakeven = strike +/- premium depending on CE/PE; P&L scenarios at stop,
    breakeven, target, and one stretch scenario the same distance beyond
    target as entry-to-target. Lot size is always read server-side from
    `option_contract.instrument_id -> instruments.lot_size`, never trusted
    from the caller — `qty_lots` is a *count of lots*, not raw quantity, so
    "wrong lot size" can't enter through this path at all.
    """
    instrument = db.get(Instrument, option_contract.instrument_id)
    if instrument is None:
        raise ValueError(f"unknown instrument for option_contract {option_contract.id}")
    lot_size = Decimal(instrument.lot_size)
    lots = Decimal(qty_lots)
    sign = Decimal("1") if side == SignalSide.BUY else Decimal("-1")

    capital_required = _dec(entry_price) * lot_size * lots
    if funding_mode == FundingMode.MTF:
        capital_required = capital_required / MTF_STUB_LEVERAGE_FACTOR

    # breakeven_price is in *underlying* terms (strike +/- premium) — the
    # classic held-to-expiry breakeven, on a completely different price
    # scale than entry/stop/target (which are option *premium* levels for
    # this same-day scalp). It is not a price this position could ever be
    # "closed at" intraday, so it is reported as its own field, not plugged
    # into the premium-delta P&L formula below.
    if OptionType(option_contract.option_type) == OptionType.CE:
        breakeven_price = _dec(option_contract.strike) + _dec(entry_price)
    else:
        breakeven_price = _dec(option_contract.strike) - _dec(entry_price)

    def _pnl_at(price: Decimal) -> float:
        return float((price - _dec(entry_price)) * lot_size * lots * sign)

    stretch_price = _dec(target_price) + (_dec(target_price) - _dec(entry_price))

    pnl_scenarios = {
        "at_stop": _pnl_at(_dec(stop_price)),
        # By definition — exiting at the same premium paid is a scratch
        # trade, 0 P&L, regardless of side/lot size.
        "at_breakeven": 0.0,
        "at_target": _pnl_at(_dec(target_price)),
        "stretch": _pnl_at(stretch_price),
    }

    return PreTradeAnalytics(
        capital_required=float(capital_required),
        breakeven_price=float(breakeven_price),
        pnl_scenarios=pnl_scenarios,
    )


def _open_trade_intents_query(db: Session, trading_session_id: uuid.UUID):
    """Dispatched TradeIntents whose resulting Position is still open, or
    that are DISPATCHED but haven't reached `dispatch_trade_intent` yet (the
    brief in-transaction window between Risk marking DISPATCHED and the
    caller invoking Execution) — the real proxy for "currently open
    position" that replaces Phase 2's SyntheticTradeOutcome stand-in.
    """
    return (
        db.query(TradeIntent)
        .outerjoin(Position, Position.trade_intent_id == TradeIntent.id)
        .filter(
            TradeIntent.trading_session_id == trading_session_id,
            TradeIntent.status == TradeIntentStatus.DISPATCHED,
            sa_or(Position.id.is_(None), Position.status == PositionStatus.OPEN),
        )
    )


def _open_committed_capital(db: Session, trading_session_id: uuid.UUID) -> Decimal:
    rows = (
        db.query(RiskDecision.capital_required)
        .join(TradeIntent, RiskDecision.trade_intent_id == TradeIntent.id)
        .outerjoin(Position, Position.trade_intent_id == TradeIntent.id)
        .filter(
            TradeIntent.trading_session_id == trading_session_id,
            TradeIntent.status == TradeIntentStatus.DISPATCHED,
            RiskDecision.decision == RiskDecisionOutcome.APPROVED,
            sa_or(Position.id.is_(None), Position.status == PositionStatus.OPEN),
        )
        .all()
    )
    return sum((_dec(row[0]) for row in rows), Decimal("0"))


def _same_strike_locked(
    db: Session, trading_session_id: uuid.UUID, option_contract_id: uuid.UUID
) -> bool:
    locked = (
        db.query(TradeIntent.id)
        .outerjoin(Position, Position.trade_intent_id == TradeIntent.id)
        .filter(
            TradeIntent.trading_session_id == trading_session_id,
            TradeIntent.option_contract_id == option_contract_id,
            TradeIntent.status.in_(
                [TradeIntentStatus.PENDING_APPROVAL, TradeIntentStatus.DISPATCHED]
            ),
            sa_or(Position.id.is_(None), Position.status == PositionStatus.OPEN),
        )
        .first()
    )
    return locked is not None


def _is_alert_worthy(reasons: list[str]) -> bool:
    return any(
        reason.startswith(prefix)
        for reason in reasons
        for prefix in _ALERT_WORTHY_REASON_PREFIXES
    )


def _check_margin(capital_required: Decimal, trading_session: TradingSession) -> bool:
    """Real broker-backed margin check (Phase 5+) via `BrokerPort.get_margin`
    — replaces the old fixed-placeholder stub. Uses `get_execution_broker`,
    same as every other execution-facing call site, so this always checks
    against the persistent paper mock today (`MockBrokerAdapter.get_margin`
    returns a generous synthetic figure, so paper mode stays capital-
    unconstrained same as before) and against the real Shoonya adapter once
    Phase 6 makes it the execution broker.

    Fails closed (rejects) on a `BrokerError` rather than treating it as
    `margin_ok=True` — `MockBrokerAdapter` never raises `BrokerError`
    (see `broker_adapter/base/errors.py`'s own docstring), so this only
    matters once a real broker adapter is the execution broker, at which
    point "couldn't confirm funds" should block the trade, not silently
    wave it through.
    """
    if capital_required <= 0:
        return False
    broker = get_execution_broker(trading_session)
    try:
        margin = broker.get_margin()
    except BrokerError:
        logger.exception("get_margin failed during pre-trade check; failing closed")
        return False
    return capital_required <= _dec(margin.available_margin)


def evaluate_trade_intent(
    db: Session,
    trade_intent: TradeIntent,
    trading_session: TradingSession,
    strategy_run: StrategyRun,
    *,
    approval_window: timedelta = timedelta(minutes=5),
) -> RiskDecision:
    with advisory_lock(db, LOCK_RISK_EVALUATION_QUEUE):
        option_contract = db.get(OptionContract, trade_intent.option_contract_id)
        if option_contract is None:
            raise ValueError(f"unknown option_contract_id {trade_intent.option_contract_id}")

        risk_config = get_active_risk_limit_config(db, trading_session.workspace_id)

        analytics = compute_pre_trade_analytics(
            db,
            option_contract,
            side=SignalSide(trade_intent.side),
            qty_lots=trade_intent.qty_lots,
            entry_price=float(trade_intent.entry_price),
            stop_price=float(trade_intent.stop_price),
            target_price=float(trade_intent.target_price),
            funding_mode=FundingMode(trading_session.funding_mode),
        )

        reasons: list[str] = []

        current_mode = SafeMode(trading_session.mode)
        if current_mode in (
            SafeMode.KILL_SWITCH,
            SafeMode.RECONCILIATION_LOCK,
            SafeMode.DEGRADED_MODE,
        ):
            reasons.append(f"mode_blocks_new_entries:{current_mode.value}")

        if trading_session.entries_paused_reason is not None:
            reasons.append(f"entries_paused:{trading_session.entries_paused_reason}")

        if _same_strike_locked(db, trading_session.id, trade_intent.option_contract_id):
            reasons.append("same_strike_locked")

        open_count = _open_trade_intents_query(db, trading_session.id).count()
        if open_count >= risk_config.max_concurrent_positions:
            reasons.append("max_concurrent_positions_reached")

        dispatched_today = (
            db.query(TradeIntent)
            .filter(
                TradeIntent.trading_session_id == trading_session.id,
                TradeIntent.status == TradeIntentStatus.DISPATCHED,
            )
            .count()
        )
        if dispatched_today >= risk_config.max_trades_per_day:
            reasons.append("max_trades_per_day_reached")

        if trading_session.consecutive_losses >= risk_config.consecutive_loss_pause_threshold:
            reasons.append("consecutive_loss_pause_active")

        if trade_intent.qty_lots > risk_config.per_trade_lot_cap:
            reasons.append("per_trade_lot_cap_exceeded")

        margin_ok = _check_margin(_dec(analytics.capital_required), trading_session)
        if not margin_ok:
            reasons.append("margin_check_failed")

        open_committed = _open_committed_capital(db, trading_session.id)
        projected_committed = open_committed + _dec(analytics.capital_required)
        if projected_committed > _dec(trading_session.budget_amount):
            reasons.append("budget_exceeded")

        outcome = RiskDecisionOutcome.REJECTED if reasons else RiskDecisionOutcome.APPROVED

        decision = RiskDecision(
            id=uuid.uuid4(),
            workspace_id=trading_session.workspace_id,
            trade_intent_id=trade_intent.id,
            risk_limit_config_id=risk_config.id,
            decision=outcome,
            reasons=reasons,
            checked_margin=margin_ok,
            funding_mode=trading_session.funding_mode,
            capital_required=analytics.capital_required,
            breakeven_price=analytics.breakeven_price,
            pnl_scenarios=analytics.pnl_scenarios,
            created_at=_utcnow(),
        )
        db.add(decision)

        if outcome == RiskDecisionOutcome.REJECTED:
            trade_intent.status = TradeIntentStatus.RISK_REJECTED
            db.add(trade_intent)
            db.flush()

            record_event(
                db,
                workspace_id=trading_session.workspace_id,
                actor_type=ActorType.SYSTEM,
                event_category=EventCategory.RISK_DECISION,
                event_type="risk_decision.rejected",
                entity_type="trade_intent",
                entity_id=trade_intent.id,
                trading_session_id=trading_session.id,
                strategy_config_id=strategy_run.strategy_config_id,
                payload={"reasons": reasons, "capital_required": analytics.capital_required},
            )

            if _is_alert_worthy(reasons):
                db.add(
                    SystemAlert(
                        id=uuid.uuid4(),
                        workspace_id=trading_session.workspace_id,
                        trading_session_id=trading_session.id,
                        severity=AlertSeverity.WARNING,
                        category="risk_limit_breach",
                        message=f"TradeIntent {trade_intent.id} rejected: {', '.join(reasons)}",
                        payload={"trade_intent_id": str(trade_intent.id), "reasons": reasons},
                        created_at=_utcnow(),
                    )
                )
        elif strategy_run.execution_mode == ExecutionMode.AUTO:
            trade_intent.status = TradeIntentStatus.DISPATCHED
            trade_intent.dispatched_at = _utcnow()
            db.add(trade_intent)
            db.flush()

            record_event(
                db,
                workspace_id=trading_session.workspace_id,
                actor_type=ActorType.SYSTEM,
                event_category=EventCategory.RISK_DECISION,
                event_type="risk_decision.approved.dispatched",
                entity_type="trade_intent",
                entity_id=trade_intent.id,
                trading_session_id=trading_session.id,
                strategy_config_id=strategy_run.strategy_config_id,
                payload={"capital_required": analytics.capital_required},
            )
        else:
            trade_intent.status = TradeIntentStatus.PENDING_APPROVAL
            db.add(trade_intent)
            db.flush()

            db.add(
                PendingTradeApproval(
                    id=uuid.uuid4(),
                    trade_intent_id=trade_intent.id,
                    strategy_run_id=strategy_run.id,
                    status=ApprovalStatus.PENDING,
                    capital_required=analytics.capital_required,
                    breakeven_price=analytics.breakeven_price,
                    pnl_scenarios=analytics.pnl_scenarios,
                    expires_at=_utcnow() + approval_window,
                )
            )

            record_event(
                db,
                workspace_id=trading_session.workspace_id,
                actor_type=ActorType.SYSTEM,
                event_category=EventCategory.RISK_DECISION,
                event_type="risk_decision.approved.pending_approval",
                entity_type="trade_intent",
                entity_id=trade_intent.id,
                trading_session_id=trading_session.id,
                strategy_config_id=strategy_run.strategy_config_id,
                payload={"capital_required": analytics.capital_required},
            )

        db.flush()
        return decision


def record_trade_outcome_effects(
    db: Session,
    trading_session: TradingSession,
    realized_pnl: float,
) -> None:
    """Called by `execution_engine.paper.service.close_position` right after
    it writes a real `TradeOutcome` row — this function owns only the
    session-level *effects* of that P&L (running totals + the two triggers
    below), not the outcome row itself, since that now belongs to the
    execution domain. Replaces Phase 2's `record_synthetic_outcome`
    (formerly also responsible for creating the Phase-2-only
    `SyntheticTradeOutcome` row) with the same triggers described in the
    build plan's "Daily trading plan" section: a loss-cap breach escalates
    straight to kill_switch (no soft step-down), a target-profit hit sets
    `entries_paused_reason` without touching the safety-mode state machine.

    Runs under `LOCK_RISK_EVALUATION_QUEUE` (not `LOCK_EXECUTION_SINGLETON`,
    which the caller already holds) — deliberately a *different* lock than
    the caller's, since these running totals are read by
    `evaluate_trade_intent`'s own checks under this same lock and must stay
    serialized against concurrent risk evaluations, not against concurrent
    dispatches.
    """
    with advisory_lock(db, LOCK_RISK_EVALUATION_QUEUE):
        new_cumulative = _dec(trading_session.cumulative_realized_pnl) + _dec(realized_pnl)
        trading_session.cumulative_realized_pnl = float(new_cumulative)
        trading_session.consecutive_losses = (
            trading_session.consecutive_losses + 1 if realized_pnl < 0 else 0
        )
        db.add(trading_session)
        db.flush()

        record_event(
            db,
            workspace_id=trading_session.workspace_id,
            actor_type=ActorType.SYSTEM,
            event_category=EventCategory.RISK_DECISION,
            event_type="trade_outcome.session_effects_applied",
            trading_session_id=trading_session.id,
            payload={
                "realized_pnl": float(realized_pnl),
                "cumulative_realized_pnl": float(new_cumulative),
                "consecutive_losses": trading_session.consecutive_losses,
            },
        )

        if new_cumulative <= -_dec(trading_session.daily_loss_cap):
            enter_kill_switch(
                db,
                trading_session,
                TransitionTriggerType.RISK,
                reason=f"daily loss cap breached: cumulative_realized_pnl={new_cumulative}",
            )
            db.add(
                SystemAlert(
                    id=uuid.uuid4(),
                    workspace_id=trading_session.workspace_id,
                    trading_session_id=trading_session.id,
                    severity=AlertSeverity.CRITICAL,
                    category="daily_loss_cap_breached",
                    message=(
                        "Daily loss cap breached — kill_switch engaged. "
                        f"cumulative_realized_pnl={new_cumulative}"
                    ),
                    payload={"cumulative_realized_pnl": float(new_cumulative)},
                    created_at=_utcnow(),
                )
            )
        elif (
            trading_session.entries_paused_reason is None
            and new_cumulative >= _dec(trading_session.daily_target_profit)
        ):
            trading_session.entries_paused_reason = EntriesPausedReason.DAILY_TARGET_REACHED
            db.add(trading_session)
            db.flush()

            record_event(
                db,
                workspace_id=trading_session.workspace_id,
                actor_type=ActorType.SYSTEM,
                event_category=EventCategory.RISK_DECISION,
                event_type="entries_paused.daily_target_reached",
                trading_session_id=trading_session.id,
                payload={"cumulative_realized_pnl": float(new_cumulative)},
            )
            db.add(
                SystemAlert(
                    id=uuid.uuid4(),
                    workspace_id=trading_session.workspace_id,
                    trading_session_id=trading_session.id,
                    severity=AlertSeverity.INFO,
                    category="daily_target_reached",
                    message=(
                        "Daily target profit reached — new entries paused. "
                        f"cumulative_realized_pnl={new_cumulative}"
                    ),
                    payload={"cumulative_realized_pnl": float(new_cumulative)},
                    created_at=_utcnow(),
                )
            )

        db.flush()
