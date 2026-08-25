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
from app.core.clock import to_ist
from app.core.locking import LOCK_RISK_EVALUATION_QUEUE, advisory_lock
from app.core.modes.state_machine import enter_kill_switch
from app.core.pnl import signed_pnl
from app.domain.audit.models import ActorType, EventCategory
from app.domain.execution.models import Order, OrderMode, Position, PositionStatus
from app.domain.identity.models import User
from app.domain.market.models import Instrument, OptionContract, OptionType, QuoteTick
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
    StrategyConfig,
    StrategyRun,
    TradeIntent,
    TradeIntentStatus,
)
from app.modules.alerting.manager import send_alert
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.base.errors import BrokerError
from app.modules.broker_adapter.composition import get_execution_broker, is_strategy_routed_live
from app.modules.market_data.freshness import PRICE_DRIFT_TOLERANCE_PCT, check_price_drift

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


def _is_tick_aligned(price: Decimal, tick_size: Decimal) -> bool:
    if tick_size <= 0:
        return True
    return price % tick_size == 0


def _friendly_trade_intent_label(
    db: Session,
    trade_intent: TradeIntent,
    strategy_run: StrategyRun,
    option_contract: OptionContract,
) -> str:
    """Human-readable context ("ORB BANKNIFTY 2026-08-20 09:42") for
    SystemAlert message text -- a bare UUID gives an operator nothing to
    act on without a DB lookup. Purely a message-text convenience: the
    real UUID stays the system's actual identifier everywhere else (FKs,
    `entity_id`, `payload["trade_intent_id"]`), unchanged. Falls back to
    the option contract's own symbol/a generic label if a lookup somehow
    misses -- should never happen given the FK relationships involved, but
    a friendlier message text is not worth a 500.
    """
    strategy_config = db.get(StrategyConfig, strategy_run.strategy_config_id)
    strategy_label = strategy_config.strategy_type.upper() if strategy_config else "strategy"
    instrument = db.get(Instrument, option_contract.instrument_id)
    instrument_label = instrument.symbol if instrument else option_contract.symbol
    ts_label = to_ist(trade_intent.created_at).strftime("%Y-%m-%d %H:%M")
    return f"{strategy_label} {instrument_label} {ts_label}"


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
        # Same shared sign convention `execution_engine.paper.service` and
        # `api.v1.execution` use — see app.core.pnl.signed_pnl's own
        # docstring for why this used to be hand-copied in three places.
        return float(signed_pnl(entry_price, price, lot_size * lots, side))

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

    **Live-only, 2026-08-19**: `max_concurrent_positions` exists to protect
    real capital, so a busy paper session must never count toward it — same
    "session-wide check must be live-scoped" bug shape as `max_trades_per_
    day`/`budget_exceeded`/`same_strike_locked`, all fixed the same day.
    `Order.id.is_(None)` (via the outer join) keeps counting a trade_intent
    still in that brief pre-dispatch window — it might yet turn out live —
    excluded only once its own `Order` confirms `PAPER`.
    """
    return (
        db.query(TradeIntent)
        .outerjoin(Position, Position.trade_intent_id == TradeIntent.id)
        .outerjoin(Order, Order.trade_intent_id == TradeIntent.id)
        .filter(
            TradeIntent.trading_session_id == trading_session_id,
            TradeIntent.status == TradeIntentStatus.DISPATCHED,
            sa_or(Position.id.is_(None), Position.status == PositionStatus.OPEN),
            sa_or(Order.mode == OrderMode.LIVE, Order.id.is_(None)),
        )
    )


def _open_committed_capital(db: Session, trading_session_id: uuid.UUID) -> Decimal:
    """**Live-only, 2026-08-19**: same reasoning as `_open_trade_intents_
    query` above — `budget_amount` protects real capital, so existing paper
    commitments must never count toward it.
    """
    rows = (
        db.query(RiskDecision.capital_required)
        .join(TradeIntent, RiskDecision.trade_intent_id == TradeIntent.id)
        .outerjoin(Position, Position.trade_intent_id == TradeIntent.id)
        .outerjoin(Order, Order.trade_intent_id == TradeIntent.id)
        .filter(
            TradeIntent.trading_session_id == trading_session_id,
            TradeIntent.status == TradeIntentStatus.DISPATCHED,
            RiskDecision.decision == RiskDecisionOutcome.APPROVED,
            sa_or(Position.id.is_(None), Position.status == PositionStatus.OPEN),
            sa_or(Order.mode == OrderMode.LIVE, Order.id.is_(None)),
        )
        .all()
    )
    return sum((_dec(row[0]) for row in rows), Decimal("0"))


def _same_strike_locked(
    db: Session,
    trading_session_id: uuid.UUID,
    strategy_config_id: uuid.UUID,
    option_contract_id: uuid.UUID,
) -> bool:
    """**Rescoped to per-strategy, 2026-08-19**: used to be session-wide —
    any other strategy's pending/open trade_intent on the exact same
    contract would lock a new one out, live-confirmed against two paper
    strategies (Test 4, Test) independently proposing the same strike the
    same day this was rescoped. Explicit product decision: different
    strategies are allowed to independently trade the identical contract —
    each position already has its own dedicated `StopPlan`/`TrailPlan`/
    `TradeOutcome` row (never shared across positions), so per-strategy
    SL/TSL/target tracking was already structurally guaranteed before this
    change; `reconciliation.service._local_net_qty_by_symbol` already nets
    by symbol across every open local position regardless of strategy, so
    two strategies sharing a contract (same mode) net correctly against the
    broker's own combined position with no false mismatch. **2026-08-19,
    same day, later**: netting across strategies was fine, but netting
    across *modes* wasn't — a session holding both a paper and a live
    position (now structurally possible) got compared against only one
    broker at a time, misreading the other mode's positions as phantom
    mismatches. Fixed via mode-scoped netting + `run_full_reconciliation`
    (see that module). One narrow, accepted limitation remains: if the
    *same* contract is ever held by both a paper and a live strategy at
    once, `BrokerSyncState`'s stored snapshot row (keyed only by session +
    contract, no mode dimension) reflects whichever pass ran last — alerts
    and escalation still fire correctly for both, only the persisted
    display/audit snapshot loses per-mode granularity for that one
    contract. Still scoped to *this* strategy, though: a single strategy
    must not be able to double-enter its own already-open/pending position
    on a contract (a duplicate/glitchy signal guard), in either paper or
    live mode.
    """
    locked = (
        db.query(TradeIntent.id)
        .join(StrategyRun, StrategyRun.id == TradeIntent.strategy_run_id)
        .outerjoin(Position, Position.trade_intent_id == TradeIntent.id)
        .filter(
            TradeIntent.trading_session_id == trading_session_id,
            TradeIntent.option_contract_id == option_contract_id,
            StrategyRun.strategy_config_id == strategy_config_id,
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


def _check_margin(
    capital_required: Decimal, trading_session: TradingSession, strategy_run: StrategyRun
) -> bool:
    """Real broker-backed margin check (Phase 5+) via `BrokerPort.get_margin`
    — replaces the old fixed-placeholder stub. Uses `get_execution_broker`,
    same as every other execution-facing call site, so this always checks
    against the persistent paper mock today (`MockBrokerAdapter.get_margin`
    returns a generous synthetic figure, so paper mode stays capital-
    unconstrained same as before) and against the real Shoonya adapter once
    Phase 6 makes it the execution broker.

    **Live-corrected 2026-08-18**: `strategy_run` must be passed through to
    `get_execution_broker`, not omitted — omitting it was a real bug. Per
    that function's own docstring, its `FORCE_PAPER` override (a per-
    strategy "stay on paper even though the session itself is live"
    restriction) only ever applies when `strategy_run` is given; without
    it, a `force_paper` strategy running inside a `live_enabled` session
    had its pre-trade margin checked against the *real* Shoonya account
    regardless — the account's real (often insufficient) cash could reject
    a trade that was never going to touch real money in the first place.
    Passing `strategy_run` routes a paper-mode strategy to
    `MockBrokerAdapter` here exactly as it already does for the actual
    order dispatch, so "paper trading ignores margin" is a natural
    consequence of correct broker resolution, not a separate bypass.

    Fails closed (rejects) on a `BrokerError` rather than treating it as
    `margin_ok=True` — `MockBrokerAdapter` never raises `BrokerError`
    (see `broker_adapter/base/errors.py`'s own docstring), so this only
    matters once a real broker adapter is the execution broker, at which
    point "couldn't confirm funds" should block the trade, not silently
    wave it through.
    """
    if capital_required <= 0:
        return False
    broker = get_execution_broker(trading_session, strategy_run)
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

        # DAILY_TARGET_REACHED is P&L-driven (live-only after
        # record_trade_outcome_effects's own 2026-08-19 fix below), so it
        # must not block a paper-routed intent just because the live side
        # hit its target -- same "session-wide check must be live-scoped"
        # shape as everything else fixed today. Any other reason (today
        # just the not-yet-wired-up ADMIN_PAUSE) is a deliberate
        # operational control and stays universal, same as
        # kill_switch/degraded_mode/reconciliation_lock above.
        if trading_session.entries_paused_reason is not None and (
            trading_session.entries_paused_reason != EntriesPausedReason.DAILY_TARGET_REACHED
            or is_strategy_routed_live(trading_session, strategy_run)
        ):
            reasons.append(f"entries_paused:{trading_session.entries_paused_reason}")

        if _same_strike_locked(
            db, trading_session.id, strategy_run.strategy_config_id, trade_intent.option_contract_id
        ):
            reasons.append("same_strike_locked")

        # 2026-08-19: gated on is_strategy_routed_live, same reasoning as
        # max_trades_per_day/budget_exceeded below -- max_concurrent_
        # positions protects real capital, so a busy paper session must
        # never count toward it, and a paper-routed intent must never be
        # blocked by pre-existing *live* congestion either (paper isn't
        # sharing real capacity). _open_trade_intents_query is itself
        # live-only now too (see its own docstring).
        if is_strategy_routed_live(trading_session, strategy_run):
            open_count = _open_trade_intents_query(db, trading_session.id).count()
            if open_count >= risk_config.max_concurrent_positions:
                reasons.append("max_concurrent_positions_reached")

        # 2026-08-12: real gap found and fixed — max_trades_per_day used to
        # count DISPATCHED trade_intents across the *whole session*, with no
        # paper/live distinction at all. That's the wrong rail for a paper
        # session: it's meant to cap real-money exposure per strategy per
        # day, not throttle how many times the paper-testing loop itself can
        # prove a strategy's entry logic fires — live-found when 5 earlier
        # trades from a since-stopped batch of strategies silently blocked
        # every signal from a completely different set of strategies for the
        # rest of the day, on a PAPER_ONLY session, with paper capital never
        # actually at risk. `paper_only` now has no cap at all; every
        # live-capable mode keeps the cap, scoped per `strategy_config_id`
        # rather than per session, so one strategy hitting its daily cap
        # doesn't block every other strategy running in the same session,
        # and a restart of the same strategy (a new `strategy_run` row, same
        # `strategy_config_id`) doesn't reset it.
        #
        # 2026-08-19: that first fix wasn't enough — `current_mode !=
        # PAPER_ONLY` is still session-wide, not per-strategy, the same bug
        # shape found twice already today (the margin pre-check, then
        # PositionManager's own broker resolution). Two real, opposite
        # incidents same day: a `force_paper` strategy got capped by trades
        # that were never real money (over-restrictive — this session's own
        # `paper_plus_guarded_live`/`live_enabled` transition capped a
        # strategy explicitly held back to paper), and a strategy that
        # dispatched several genuinely-paper trades earlier (session was
        # still `paper_only`, or the strategy itself was still
        # `force_paper` at the time) then had its very first *live* signal
        # blocked, because those earlier paper dispatches had already used
        # up the day's count (under-restrictive protection — the cap exists
        # to protect real capital, and here it did the opposite: hid behind
        # a count made entirely of trades that never touched real money).
        # `is_strategy_routed_live` (the same predicate `get_execution_
        # broker` itself routes on) fixes the first; requiring the counted
        # `Order.mode == LIVE` (not just any DISPATCHED trade_intent) fixes
        # the second — a strategy's earlier paper-era dispatches (however
        # many) never contribute to its live-era cap.
        if is_strategy_routed_live(trading_session, strategy_run):
            dispatched_today = (
                db.query(TradeIntent)
                .join(StrategyRun, StrategyRun.id == TradeIntent.strategy_run_id)
                .join(Order, Order.trade_intent_id == TradeIntent.id)
                .filter(
                    TradeIntent.trading_session_id == trading_session.id,
                    StrategyRun.strategy_config_id == strategy_run.strategy_config_id,
                    TradeIntent.status == TradeIntentStatus.DISPATCHED,
                    Order.mode == OrderMode.LIVE,
                )
                .count()
            )
            if dispatched_today >= risk_config.max_trades_per_day:
                reasons.append("max_trades_per_day_reached")

        # 2026-08-19: gated on is_strategy_routed_live -- consecutive_losses
        # is now only ever incremented by genuinely-live closes (see
        # record_trade_outcome_effects's own fix), so this stays 0 on a
        # pure-paper day regardless; the explicit gate matters on a *mixed*
        # day, where a live-side losing streak must not stop a paper
        # strategy from continuing to test.
        if is_strategy_routed_live(trading_session, strategy_run) and (
            trading_session.consecutive_losses >= risk_config.consecutive_loss_pause_threshold
        ):
            reasons.append("consecutive_loss_pause_active")

        if trade_intent.qty_lots > risk_config.per_trade_lot_cap:
            reasons.append("per_trade_lot_cap_exceeded")

        # Safe against every current test/live-paper path, not just
        # untested: MockBrokerAdapter's option premiums are seeded as whole
        # numbers (base = 50.0 + crc32(symbol) % 200) and never "stepped"
        # for options specifically (get_quote/get_option_chain always pass
        # step=False; only a subscribed *underlying* symbol steps, via the
        # streaming loop) — and compute_stop_target's clean 0.9/1.15-style
        # multipliers preserve exact 0.05-tick alignment for any whole-
        # number entry price. This only starts doing real rejecting work
        # once Shoonya's genuinely fractional premiums flow through
        # execution (Phase 6) — worth re-verifying then.
        instrument = db.get(Instrument, option_contract.instrument_id)
        if instrument is not None:
            for label, price in (
                ("entry", trade_intent.entry_price),
                ("stop", trade_intent.stop_price),
                ("target", trade_intent.target_price),
            ):
                if not _is_tick_aligned(_dec(price), _dec(instrument.tick_size)):
                    reasons.append(f"tick_size_violation:{label}")

            if instrument.freeze_qty is not None:
                raw_qty = trade_intent.qty_lots * instrument.lot_size
                if raw_qty > instrument.freeze_qty:
                    reasons.append("freeze_qty_exceeded")

        # AUTO-mode equivalent of the manual-approval price-drift re-check
        # (api.v1.strategies.approve_trade_approval) — same shared helper,
        # same tolerance. Closes the asymmetry where a human's Approve click
        # was re-validated against the latest tick but an AUTO-dispatched
        # intent, generated from a proposal that may itself be a cycle or
        # more old by the time this evaluation runs, never was.
        latest_tick = (
            db.query(QuoteTick)
            .filter(QuoteTick.option_contract_id == trade_intent.option_contract_id)
            .order_by(QuoteTick.ts.desc())
            .first()
        )
        if latest_tick is not None and check_price_drift(
            float(latest_tick.ltp),
            float(trade_intent.entry_price),
            tolerance_pct=PRICE_DRIFT_TOLERANCE_PCT,
        ):
            reasons.append("price_drift_exceeded")

        margin_ok = _check_margin(_dec(analytics.capital_required), trading_session, strategy_run)
        if not margin_ok:
            reasons.append("margin_check_failed")

        # 2026-08-19: gated on is_strategy_routed_live -- a paper intent's
        # own capital_required must never be checked against the real
        # budget at all (not just "existing paper commitments excluded" —
        # _open_committed_capital already handles that side, see its own
        # docstring), same reasoning as max_concurrent_positions above.
        if is_strategy_routed_live(trading_session, strategy_run):
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
                label = _friendly_trade_intent_label(
                    db, trade_intent, strategy_run, option_contract
                )
                db.add(
                    SystemAlert(
                        id=uuid.uuid4(),
                        workspace_id=trading_session.workspace_id,
                        trading_session_id=trading_session.id,
                        severity=AlertSeverity.WARNING,
                        category="risk_limit_breach",
                        message=(
                            f"TradeIntent for {label} ({trade_intent.id}) rejected: "
                            f"{', '.join(reasons)}"
                        ),
                        payload={"trade_intent_id": str(trade_intent.id), "reasons": reasons},
                        created_at=_utcnow(),
                    )
                )
        elif (
            strategy_run.execution_mode == ExecutionMode.AUTO
            # Paper trades always auto-dispatch, regardless of the
            # strategy's configured execution_mode -- approval-required
            # exists to gate real-money risk, and a paper trade carries
            # none. Uses the same is_strategy_routed_live predicate this
            # function already gates its risk caps on (2026-08-19 fix,
            # see that function's own docstring) so this stays consistent
            # with every other paper-vs-live decision in this module.
            or not is_strategy_routed_live(trading_session, strategy_run)
        ):
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
    *,
    is_live: bool,
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

    **`is_live` required, 2026-08-19**: this used to run unconditionally
    for every closed position, paper or live, with no distinction — the
    most severe bug found in that day's audit. `cumulative_realized_pnl`/
    `consecutive_losses` drive a *real* `kill_switch` and `entries_paused_
    reason`; a losing streak of pure paper trades could trip both, halting
    the entire session (paper and live) over a "loss" that never touched
    real money, and paper profit accumulating toward `daily_target_profit`
    could pause live entries the same way. `is_live=False` now returns
    immediately, before the lock and before any write — paper P&L is
    already fully captured per-trade in `TradeOutcome` (symbol, strategy,
    entry/exit, realized_pnl, timestamps), which is what every "evaluate
    the strategies" query in this project has always actually used; no
    separate paper-side running total was needed.

    Runs under `LOCK_RISK_EVALUATION_QUEUE` (not `LOCK_EXECUTION_SINGLETON`,
    which the caller already holds) — deliberately a *different* lock than
    the caller's, since these running totals are read by
    `evaluate_trade_intent`'s own checks under this same lock and must stay
    serialized against concurrent risk evaluations, not against concurrent
    dispatches.
    """
    if not is_live:
        return
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
            send_alert(
                db,
                workspace_id=trading_session.workspace_id,
                trading_session_id=trading_session.id,
                severity=AlertSeverity.CRITICAL,
                category="daily_loss_cap_breached",
                message=(
                    "Daily loss cap breached — kill_switch engaged. "
                    f"cumulative_realized_pnl={new_cumulative}"
                ),
                payload={"cumulative_realized_pnl": float(new_cumulative)},
                # This whole function returns early unless is_live (see its
                # own docstring) -- paper trades never reach here at all.
                mode=OrderMode.LIVE,
                dedup_key=f"daily_loss_cap_breached:{trading_session.id}",
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
