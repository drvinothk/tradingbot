"""Paper Execution Service — the real Order -> fill -> Position ->
StopPlan/TrailPlan -> TradeOutcome lifecycle that Phase 3 introduces to
replace Phase 2's `close_dispatched_trade_intent_synthetically`/
`record_synthetic_outcome` stand-in.

Calls `BrokerPort.place_order`/`get_positions` against whichever adapter
`app.modules.broker_adapter.composition.get_execution_broker` resolves to
(the mock for paper-routed sessions/strategies, the real connected broker
for genuinely live ones — see that function's own routing table; separate
from what `get_broker()` — the market-data accessor — currently holds)
rather than simulating fills purely from tick data — this reuses Phase 1's
already-built order/position
simulation and gives Reconciliation Service a genuine broker-side state to
diff local `positions` against. See the Phase 3 plan's "Key design
decision" note for the full reasoning; Phase 6's real live-order path will
extend `get_execution_broker` with graduation gating, not rewrite this
module. `get_execution_broker` is deliberately separate from `get_broker`
so that connecting Shoonya for real market data (Phase 5) can never, by
itself, cause a paper trade to place a real order — see
`broker_adapter/composition.py`'s own docstring for the incident that
motivated the split.

Callers (not this module): `strategy_engine.service.submit_signal` calls
`dispatch_trade_intent` right after `risk_engine.service.evaluate_trade_intent`
returns an AUTO-mode approval; `api.v1.strategies.approve_trade_approval`
calls it for the approval-required path. Neither call happens *inside*
`evaluate_trade_intent`'s own `LOCK_RISK_EVALUATION_QUEUE` scope — keeping
Risk Service's lock and this module's `LOCK_EXECUTION_SINGLETON` disjoint in
every call path avoids ever nesting the two locks.

Both `dispatch_trade_intent` and `close_position` run an event-triggered
`reconciliation.service.run_reconciliation` pass just before returning,
still inside their own `LOCK_EXECUTION_SINGLETON` scope — per the build
plan, Reconciliation Service is "event-triggered + polling", and
`PositionManager` only covers the polling half. Nesting is safe here: a
mismatch can escalate to `reconciliation_lock` via `transition_mode`, which
acquires the *same* `LOCK_EXECUTION_SINGLETON` again — `core/locking.py`'s
transaction-scoped advisory locks are reentrant/stacked per transaction (a
second `pg_advisory_xact_lock(key)` call on a key this same
session/transaction already holds returns immediately, no-op), so this
cannot self-deadlock; it only would if some other call path acquired
`LOCK_RISK_EVALUATION_QUEUE` before `LOCK_EXECUTION_SINGLETON` and the two
crossed with this one, which nothing in this codebase does.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.db.base import utcnow as _utcnow
from app.core.db.session import SessionFactory
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
    OrderType,
    Position,
    PositionStatus,
    StopPlan,
    StopPlanStatus,
    TradeOutcome,
    TrailPlan,
    TrailPlanStatus,
)
from app.domain.market.models import Instrument, OptionContract
from app.domain.ops.models import AlertSeverity
from app.domain.session.models import TradingSession
from app.domain.strategy.models import SignalSide, StrategyRun, TradeIntent, TradeIntentStatus
from app.modules.alerting.manager import send_alert
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.base.contracts import (
    BrokerOrderStatus,
    OrderRequest,
    OrderResult,
    Tick,
)
from app.modules.broker_adapter.base.contracts import OrderType as BrokerOrderType
from app.modules.broker_adapter.base.errors import ConfigurationError
from app.modules.broker_adapter.composition import (
    get_broker,
    get_execution_broker,
    is_execution_broker_live,
    unwrap_broker,
)
from app.modules.broker_adapter.preflight import run_preflight_checks
from app.modules.execution_engine.paper.order_helpers import (
    _apply_slippage,
    _dec,
    _map_status,
    _new_order,
    _new_order_event,
    _opposite,
    _round_to_tick,
    _to_broker_side,
    _to_domain_side,
)
from app.modules.execution_engine.paper.protective_stop import (
    CancelOutcome,
    cancel_resting_protective_stop,
    place_protective_stop,
    sync_resting_protective_stop,
)
from app.modules.market_data.freshness import (
    OPTION_CHAIN_THRESHOLDS,
    FreshnessState,
    classify_option_chain,
    ensure_fresh_option_chain,
    fresh_tick_or_none,
    latest_snapshot_tick,
)
from app.modules.market_data.providers.base import BaseMarketDataProvider
from app.modules.reconciliation.service import run_reconciliation
from app.modules.risk_engine.service import record_trade_outcome_effects
from app.modules.strategy_engine.common_rules import BAR_TIMEFRAME, get_recent_completed_bars

logger = logging.getLogger("app.execution_engine.paper.service")

# Generic Phase-3 trailing rule, used when a TradeIntent doesn't specify its
# own (Phase 4's per-strategy override — see _open_position_from_fill).
# Activates once unrealized profit reaches this fraction of the
# entry->target distance; once active, the stop trails to lock in this
# fraction of favorable movement beyond the activation point, monotonically
# tightening only.
TRAIL_ACTIVATION_FRACTION = Decimal("0.5")
TRAIL_LOCK_FRACTION = Decimal("0.5")

# Phase 4: generic spread-blowout exit — the same threshold for every
# strategy regardless of structure_level (unlike the trail, this isn't
# per-method). A wider threshold than StrikeRankingConfig.max_spread_pct
# (0.15, an *entry* filter) deliberately: an open position shouldn't be
# force-exited by the same bar the entry filter would merely have scored
# lower, only once liquidity has genuinely dried up.
SPREAD_BLOWOUT_PCT = Decimal("0.30")


def _structure_break_confirmed_by_bar_close(
    db: Session, instrument_id: uuid.UUID, buffered_level: Decimal, favorable: bool
) -> bool:
    """The persistence timer alone can still confirm on pure intra-bar noise
    that fully recovers by the time the bar closes -- live-observed
    2026-08-24 (a 120s-persistence VWAP Pullback trade still confirmed
    structure_break on a live tick, PnL still positive, consistent with the
    breach never actually being real). Requires the latest COMPLETED
    underlying bar (`BAR_TIMEFRAME` -- the one timeframe every strategy's
    own entry logic already confirms against via `touch_and_confirm`) to
    have actually closed beyond `buffered_level`, not just touched it
    mid-bar -- the same close-not-wick discipline entries already use,
    applied symmetrically to exits. No completed bar yet returns False
    (fail-safe: don't confirm without real bar evidence, keep the candidate
    active and re-check next poll).

    No staleness check on the bar itself — trusts whatever `price_bars`'
    latest row is. If underlying ingestion ever stalled, a stale bar's old
    close could wrongly gate the decision either way; `market_data.
    freshness` already has the tooling for this elsewhere
    (`classify_latest_tick`) but wiring it into this specific check is
    separate, not-yet-done scope, flagged here rather than silently assumed
    fine.
    """
    bars = get_recent_completed_bars(db, instrument_id, BAR_TIMEFRAME, limit=1)
    if not bars:
        return False
    close = _dec(bars[0].close)
    return close < buffered_level if favorable else close > buffered_level


def _raise_if_option_chain_stale(
    db: Session, trading_session: TradingSession, option_contract: OptionContract
) -> None:
    """2026-08-26: moved here from `broker_adapter.preflight.run_preflight_
    checks` -- that module talking to `market_data.freshness` was the one
    import making the `broker_adapter`/`market_data` package relationship
    bidirectional, when both packages' own docstrings claim one-directional
    layering. This module already depends on `market_data` elsewhere, so
    the check belongs here, called alongside `run_preflight_checks` at each
    real-dispatch call site rather than inside it.
    """
    freshness = classify_option_chain(
        db, option_contract.instrument_id, option_contract.expiry_date
    )
    if freshness in (FreshnessState.STALE, FreshnessState.DEAD):
        raise ConfigurationError(
            f"Pre-flight failed for trading_session {trading_session.id}: option chain for "
            f"{option_contract.symbol} is {freshness.value} -- refusing real dispatch."
        )


class _ResolvedOrderPricing(NamedTuple):
    limit_price: float
    broker_order_type: BrokerOrderType
    domain_order_type: OrderType


def _resolve_order_pricing(
    order_mode: OrderMode, price: Decimal, side: SignalSide, tick_size: Decimal
) -> _ResolvedOrderPricing:
    """order_type: **live-corrected 2026-08-19** -- a real Shoonya PlaceOrder
    call rejects order_type=MARKET outright ("ALGO_CHK: MKT Order type not
    allowed for API order", live-confirmed). PAPER keeps MARKET unchanged
    (existing contract/tests expect it; the mock doesn't care). LIVE sends a
    real LIMIT order, priced with its own dedicated buffer
    (`AppSettings.live_limit_order_buffer_pct`) rather than paper's
    `fill_slippage_pct` (which defaults to 0.0 and exists for a different
    purpose -- realistic paper fill simulation, not "will a real limit order
    actually execute despite LTP moving between decision and placement").
    Shared by `dispatch_trade_intent`'s entry order and `close_position`'s
    exit order -- previously duplicated identically in both.
    """
    if order_mode == OrderMode.LIVE:
        buffer_pct = _dec(get_settings().app.live_limit_order_buffer_pct)
        buffered_price = _apply_slippage(price, side, buffer_pct)
        limit_price = float(_round_to_tick(buffered_price, tick_size, side))
        return _ResolvedOrderPricing(limit_price, BrokerOrderType.LIMIT, OrderType.LIMIT)

    slippage_pct = _dec(get_settings().paper_trading.fill_slippage_pct)
    limit_price = float(_apply_slippage(price, side, slippage_pct))
    return _ResolvedOrderPricing(limit_price, BrokerOrderType.MARKET, OrderType.MARKET)


def dispatch_trade_intent(
    db: Session,
    trading_session: TradingSession,
    trade_intent: TradeIntent,
    broker: BrokerPort | None = None,
) -> Order:
    """Idempotency-before-dispatch: a repeated call for the same TradeIntent
    (retry after a crash between the broker call and the commit, or a
    duplicate call from a confused caller) returns the existing `Order`
    rather than placing a second one — the check happens first, inside the
    same `LOCK_EXECUTION_SINGLETON` scope as the insert, so two concurrent
    callers can't both pass it.
    """
    # order_mode is decided purely by `is_execution_broker_live(broker)` --
    # whatever broker is actually about to place this order, real or mock,
    # explicitly passed in or resolved here via `get_execution_broker`.
    # 2026-08-20 fix: this used to also require "broker wasn't explicitly
    # passed in", on the theory that an explicit `broker=` could only ever
    # be a test fake that `is_execution_broker_live`'s plain `isinstance`
    # check can't correctly classify. That's true for the test suite's own
    # `_FakeLiveBroker`-style doubles (still correctly read as live because
    # they aren't `MockBrokerAdapter` instances -- see that class's own
    # docstring) but false for `eod_square_off.py`'s callers and
    # `PositionManager`'s stop/target/trail loop, both real production code
    # paths that pre-resolve the correct broker once (to reuse it for
    # pricing) and then pass it in explicitly -- a genuinely live position's
    # exit was being force-tagged PAPER by this guard, skipping real PnL/
    # loss-cap/kill_switch effects and placing a MARKET order live Shoonya
    # rejects. See `is_execution_broker_live`'s own docstring: every
    # broker/test-double actually used across this codebase either really
    # is a `MockBrokerAdapter` (tagged paper, correctly) or really isn't
    # (tagged live, correctly) -- nothing in this codebase constructs a
    # broker-shaped object that is genuinely mock-backed but fails the
    # `isinstance` check, so the extra guard was never protecting a real
    # case, only miscategorizing this one.
    strategy_run = db.get(StrategyRun, trade_intent.strategy_run_id)
    broker = broker or get_execution_broker(trading_session, strategy_run)
    order_mode = OrderMode.LIVE if is_execution_broker_live(broker) else OrderMode.PAPER

    with advisory_lock(db, LOCK_EXECUTION_SINGLETON):
        existing = (
            db.query(Order)
            .filter(Order.idempotency_key == trade_intent.idempotency_key)
            .one_or_none()
        )
        if existing is not None:
            return existing

        option_contract = db.get(OptionContract, trade_intent.option_contract_id)
        if option_contract is None:
            raise ValueError(f"unknown option_contract_id {trade_intent.option_contract_id}")
        instrument = db.get(Instrument, option_contract.instrument_id)
        if instrument is None:
            raise ValueError(f"unknown instrument for option_contract {option_contract.id}")

        side = SignalSide(trade_intent.side)
        qty = trade_intent.qty_lots * instrument.lot_size
        now = _utcnow()

        if order_mode == OrderMode.LIVE:
            run_preflight_checks(broker, trading_session=trading_session)
            _raise_if_option_chain_stale(db, trading_session, option_contract)

        # trade_intent.entry_price is already the strategy's real,
        # REST-option-chain-derived proposed price (see every strategy's
        # rank_from_latest_snapshot() call) -- pass it through as the fill
        # basis instead of leaving MockBrokerAdapter to fill at its own
        # independent synthetic price.
        #
        limit_price, broker_order_type, domain_order_type = _resolve_order_pricing(
            order_mode, _dec(trade_intent.entry_price), side, _dec(instrument.tick_size)
        )

        order_result = broker.place_order(
            OrderRequest(
                idempotency_key=trade_intent.idempotency_key,
                contract_symbol=option_contract.symbol,
                side=_to_broker_side(side),
                order_type=broker_order_type,
                qty=qty,
                limit_price=limit_price,
                lot_size=instrument.lot_size,
                tag=f"session:{trading_session.id}",
            )
        )

        order = _new_order(
            trading_session,
            option_contract,
            order_result,
            mode=order_mode,
            side=_to_domain_side(side),
            order_type=domain_order_type,
            qty=qty,
            idempotency_key=trade_intent.idempotency_key,
            now=now,
            trade_intent_id=trade_intent.id,
        )
        db.add(order)
        db.flush()

        db.add(
            _new_order_event(
                order.id,
                order_result,
                event_type="filled" if order.status == OrderStatus.FILLED else "submitted",
                now=now,
                include_raw_message=True,
            )
        )

        record_event(
            db,
            workspace_id=trading_session.workspace_id,
            actor_type=ActorType.SYSTEM,
            event_category=EventCategory.ORDER_LIFECYCLE,
            event_type="order.dispatched",
            entity_type="order",
            entity_id=order.id,
            trading_session_id=trading_session.id,
            payload={
                "trade_intent_id": str(trade_intent.id),
                "qty": qty,
                "status": order.status.value,
            },
        )

        if order.status == OrderStatus.FILLED:
            _open_position_from_fill(
                db, trading_session, trade_intent, option_contract, order, side, broker
            )
        elif order.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
            # 2026-08-20, live incident: a synchronously-rejected order left
            # TradeIntent.status stuck at DISPATCHED forever -- nothing here
            # ever moved it to a terminal state, which permanently blocked
            # `_same_strike_locked` for this exact strategy+contract until a
            # manual DB fix. Same terminal-status reasoning as
            # `_apply_resolved_pending_order`'s identical fix for the
            # asynchronous case (a pending order later discovered rejected)
            # -- see that function's own docstring for why EXPIRED, not a
            # new status.
            trade_intent.status = TradeIntentStatus.EXPIRED
            db.add(trade_intent)

            if order.status == OrderStatus.REJECTED:
                # 2026-08-25: the broker's real rejection reason
                # (order_result.raw_message, e.g. "margin insufficient")
                # was already being returned here but silently discarded --
                # never stored, never surfaced. Alerted (not just an
                # OrderEvent row, per this file's own newly-added
                # raw_message field above) since a rejection is something
                # the user explicitly wants to know about, not just audit.
                send_alert(
                    db,
                    workspace_id=trading_session.workspace_id,
                    trading_session_id=trading_session.id,
                    severity=AlertSeverity.CRITICAL,
                    category="order_rejected",
                    message=(
                        f"Order for {option_contract.symbol} rejected by broker: "
                        f"{order_result.raw_message or 'no reason given'}"
                    ),
                    mode=order.mode,
                    dedup_key=f"order_rejected:{trade_intent.id}",
                )

        db.flush()
        run_reconciliation(db, broker, trading_session, ReconciliationTrigger.EVENT)
        return order


def _open_position_from_fill(
    db: Session,
    trading_session: TradingSession,
    trade_intent: TradeIntent,
    option_contract: OptionContract,
    order: Order,
    side: SignalSide,
    broker: BrokerPort,
) -> Position:
    now = _utcnow()
    entry_price = _dec(order.avg_fill_price)

    position = Position(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        option_contract_id=option_contract.id,
        trade_intent_id=trade_intent.id,
        opening_order_id=order.id,
        side=_to_domain_side(side),
        qty=order.filled_qty,
        entry_price=float(entry_price),
        status=PositionStatus.OPEN,
        opened_at=now,
    )
    db.add(position)
    db.flush()

    # StopPlan.qty is meant to be recomputed on every fill event touching
    # this position — with the mock adapter's synchronous full-fill
    # behavior there's only ever one fill event this phase, so this always
    # equals order.filled_qty; the recompute path is designed for
    # partial-fill-capable brokers (Phase 5+), not exercised until then.
    stop_plan = StopPlan(
        id=uuid.uuid4(),
        position_id=position.id,
        stop_price=float(trade_intent.stop_price),
        qty=position.qty,
        structure_level=trade_intent.structure_level,
        structure_break_buffer=trade_intent.structure_break_buffer,
        structure_break_persistence_seconds=trade_intent.structure_break_persistence_seconds,
        status=StopPlanStatus.CONFIRMED,
        created_at=now,
        updated_at=now,
    )

    # Per-method trailing (Phase 4): a strategy that supplied its own
    # activation/lock fractions on the TradeIntent overrides the generic
    # Phase-3 0.5/0.5 rule; None (SyntheticStrategy, and any strategy that
    # doesn't set them) falls back to it unchanged.
    activation_fraction = (
        _dec(trade_intent.trail_activation_fraction)
        if trade_intent.trail_activation_fraction is not None
        else TRAIL_ACTIVATION_FRACTION
    )
    lock_fraction = (
        _dec(trade_intent.trail_lock_fraction)
        if trade_intent.trail_lock_fraction is not None
        else TRAIL_LOCK_FRACTION
    )
    activation_distance = (
        abs(_dec(trade_intent.target_price) - _dec(trade_intent.entry_price)) * activation_fraction
    )
    activation_price = (
        entry_price + activation_distance
        if side == SignalSide.BUY
        else entry_price - activation_distance
    )
    trail_plan = TrailPlan(
        id=uuid.uuid4(),
        position_id=position.id,
        trail_type="generic_activation_lock",
        activation_price=float(activation_price),
        trail_value=float(lock_fraction),
        current_stop_price=None,
        status=TrailPlanStatus.INACTIVE,
        updated_at=now,
    )
    db.add_all([stop_plan, trail_plan])
    db.flush()

    # LIVE-only crash-resilience layer (bracket/cover orders confirmed
    # unavailable for options on Shoonya -- see the build plan's bracket-
    # order research; this is the "Hard SL with Local Target" design that
    # replaced it). `order` is this position's own opening order, so its
    # `mode` *is* the per-position live/paper signal -- same source of
    # truth as `broker_adapter.composition._position_opened_live`, just
    # read directly since it's already in hand here rather than re-fetched.
    # PAPER positions are untouched: today's pure local-monitoring stays
    # the only mechanism, unchanged.
    if order.mode == OrderMode.LIVE:
        place_protective_stop(db, trading_session, position, stop_plan, option_contract, broker)

    record_event(
        db,
        workspace_id=trading_session.workspace_id,
        actor_type=ActorType.SYSTEM,
        event_category=EventCategory.ORDER_LIFECYCLE,
        event_type="position.opened",
        entity_type="position",
        entity_id=position.id,
        trading_session_id=trading_session.id,
        payload={"qty": position.qty, "entry_price": float(entry_price)},
    )

    # Sleep inhibitor: "has an open position" half of the two overlapping
    # lifecycles core/sleep_inhibitor.py's own docstring describes — the
    # other half (actively scanning) is acquired/released in
    # api.v1.strategies.start_strategy/stop_strategy. Released in
    # close_position below.
    get_sleep_inhibitor().acquire(f"position:{position.id}")

    # Deliberately does NOT subscribe this position's option-contract symbol
    # for live pricing here — see PositionManager._ensure_symbol_subscribed's
    # own docstring. Doing it in this function (called directly, with a
    # test-owned `broker=`, from unit/integration tests all over this
    # codebase) would repeat the exact `ensure_ingestion_running`-touches-
    # production-`session_scope` trap this file's own module docstring
    # already warns about for PositionManager itself: a real, live bug in an
    # earlier version of this change spawned MarketDataIngestionService
    # background threads against the *real* dev database from ordinary test
    # runs (found via a QC pass — 3,400+ stray quote_ticks rows in the dev
    # DB). PositionManager subscribes for its own tracked positions instead,
    # directly on whichever market_data_provider it was given — no DB
    # session, no registry singleton, nothing for a test's own broker/db
    # fixtures to accidentally bypass.

    return position


# 2026-08-20, live incident: a real Shoonya LIVE LIMIT order (Test 1,
# NIFTY25AUG26C24250) filled at the broker but was never detected locally --
# `dispatch_trade_intent` only ever creates a Position when `place_order()`
# returns FILLED *synchronously*, which held for every order this system had
# ever placed until LIVE orders switched from MARKET to LIMIT (2026-08-19):
# a LIMIT order can legitimately sit "pending" at the broker and fill later,
# and nothing re-checked a pending order afterward. The position sat with no
# local Position/StopPlan -- invisible to PositionManager's stop/target/
# trail checks *and* its EOD square-off sweep, both of which only ever
# iterate the `positions` table -- until reconciliation's own poll caught
# the broker-vs-local qty mismatch and locked the session, correctly, but
# reconciliation is deliberately alert-only and never self-heals (see that
# module's own docstring). Fixed live via a hand-reconstructed Position/
# StopPlan/TrailPlan against the user-confirmed real fill; this function is
# the permanent fix so a human never has to do that again.
_TERMINAL_BROKER_ORDER_STATUSES = frozenset(
    {BrokerOrderStatus.FILLED, BrokerOrderStatus.REJECTED, BrokerOrderStatus.CANCELLED}
)


def reconcile_pending_live_orders(
    db: Session,
    trading_session: TradingSession,
    *,
    allow_rest_fallback: bool,
    broker: BrokerPort | None = None,
) -> None:
    """Two-layer detection for a LIVE entry order that didn't resolve
    synchronously at dispatch, called every `PositionManager` cycle:

    1. **Fast path, every call, free**: check
       `ShoonyaBrokerAdapter.peek_cached_order_update` — a cache the
       adapter's WS `on_order_update` push (see `ws_client.py`) populates
       in the background, no REST call. Near-instant when it works.
    2. **Safety net, only when `allow_rest_fallback` is True**: an
       unconditional `broker.get_order_status` REST call regardless of
       what step 1 found (or didn't) — deliberately *not* gated on
       whether WS looked healthy, since ticks and order-update pushes are
       different message types on the same socket and there is no
       reliable way to self-diagnose the order-update channel specifically
       (it's event-driven/sparse, unlike the continuous tick stream
       `market_data.freshness` can watch for staleness). The caller is
       expected to pass `allow_rest_fallback=True` on a deliberately
       conservative, flat cadence (~30s), independent of any WS-health
       signal — see `PositionManager._run_cycle`'s own call site.

    Only ever finds rows for LIVE orders — `MockBrokerAdapter` always
    fills synchronously, so a PAPER order is never left PENDING for this
    to pick up.

    `broker`, like `dispatch_trade_intent`/`close_position`'s own param of
    the same name, is an explicit test-injection override — every existing
    test in this module passes its own `MockBrokerAdapter()` rather than
    relying on the process-wide singleton. Production (`PositionManager`)
    never passes it, so each order still resolves its own broker via
    `get_execution_broker`, same per-order resolution `resolve_broker_for_
    position` already established for open positions.
    """
    pending_orders = (
        db.query(Order)
        .filter(
            Order.trading_session_id == trading_session.id,
            Order.mode == OrderMode.LIVE,
            Order.trade_intent_id.is_not(None),
            Order.status.in_(
                [OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED]
            ),
        )
        .all()
    )
    for order in pending_orders:
        trade_intent = db.get(TradeIntent, order.trade_intent_id)
        if trade_intent is None:
            continue
        try:
            if broker is not None:
                resolved_broker = broker
            else:
                strategy_run = db.get(StrategyRun, trade_intent.strategy_run_id)
                resolved_broker = get_execution_broker(trading_session, strategy_run, order=order)

            result = _peek_order_update(resolved_broker, order.broker_order_id)
            if result is None and allow_rest_fallback:
                result = resolved_broker.get_order_status(order.broker_order_id)
        except Exception:  # noqa: BLE001 - a PositionManager cycle must never die
            # crash-looped here on a real live order 2026-08-25: the only
            # exception ever expected from a real broker call is
            # BrokerError, but a resolution bug meant this could receive
            # the *mock* adapter for a real order and raise a bare
            # KeyError instead -- uncaught, that took down every other
            # position's handling for the rest of this cycle too, not
            # just this one order's reconciliation. The resolution bug is
            # fixed (see get_execution_broker's `order` param), but this
            # whole block -- resolution included, not just the REST call --
            # is still real-money-adjacent I/O and deserves the same
            # "never raises" discipline as place_protective_stop/
            # cancel_resting_protective_stop/sync_resting_protective_stop
            # already have. Resolution itself can legitimately raise too
            # (e.g. `ConfigurationError` if Shoonya isn't connected yet
            # right after a restart) and must not crash the cycle either.
            logger.exception(
                "reconciling pending order %s (broker_order_id=%s) failed",
                order.id,
                order.broker_order_id,
            )
            continue

        if result is None or result.status not in _TERMINAL_BROKER_ORDER_STATUSES:
            continue

        _apply_resolved_pending_order(
            db, trading_session, trade_intent, order, result, resolved_broker
        )
        db.flush()


def _peek_order_update(broker: BrokerPort, broker_order_id: str) -> OrderResult | None:
    """Reaches past `_AuthAwareBroker`'s wrap (via `unwrap_broker`, same
    helper `api.v1.shoonya`'s diagnostics use) to `peek_cached_order_update`
    — Shoonya-specific, not part of `BrokerPort`, since no other adapter has
    a WS order-update push to cache from.
    """
    inner = unwrap_broker(broker)
    peek = getattr(inner, "peek_cached_order_update", None)
    if not callable(peek):
        return None
    return peek(broker_order_id)  # type: ignore[no-any-return]


def _apply_resolved_pending_order(
    db: Session,
    trading_session: TradingSession,
    trade_intent: TradeIntent,
    order: Order,
    result: OrderResult,
    broker: BrokerPort,
) -> None:
    now = _utcnow()
    order.status = _map_status(result.status)
    order.filled_qty = result.filled_qty
    order.avg_fill_price = result.avg_fill_price
    order.updated_at = now
    db.add(order)

    db.add(
        _new_order_event(
            order.id,
            result,
            event_type="filled" if order.status == OrderStatus.FILLED else "resolved",
            now=now,
            source="reconcile_pending_live_orders",
        )
    )

    if order.status == OrderStatus.FILLED:
        option_contract = db.get(OptionContract, order.option_contract_id)
        if option_contract is not None:
            side = SignalSide(trade_intent.side)
            _open_position_from_fill(
                db, trading_session, trade_intent, option_contract, order, side, broker
            )
    else:
        # REJECTED/CANCELLED discovered after the fact: TradeIntent.status
        # must not stay DISPATCHED forever once the broker has definitively
        # said this order never became a position, or `_same_strike_locked`
        # blocks this exact strategy+contract permanently — the identical
        # stuck-TradeIntent incident this same night required a manual DB
        # fix for. EXPIRED is a slight semantic stretch (that status
        # otherwise means a PENDING_APPROVAL timeout) but is the closest
        # existing terminal status meaning "this never executed, treat as
        # gone" — no TradeIntentStatus exists yet for "broker rejected it
        # after dispatch".
        trade_intent.status = TradeIntentStatus.EXPIRED
        db.add(trade_intent)

    record_event(
        db,
        workspace_id=trading_session.workspace_id,
        actor_type=ActorType.SYSTEM,
        event_category=EventCategory.ORDER_LIFECYCLE,
        event_type="order.reconciled",
        entity_type="order",
        entity_id=order.id,
        trading_session_id=trading_session.id,
        payload={"status": order.status.value, "filled_qty": order.filled_qty},
    )


def resolve_broker_for_position(
    db: Session, trading_session: TradingSession, position: Position
) -> BrokerPort:
    """Resolves the correct broker for one specific position's own
    strategy — must be called per-position, never once for a whole batch
    of positions, since different open positions in the same session can
    belong to differently-configured strategies (one `force_paper`, one
    genuinely graduated live) whose orders must never share a single
    broker resolution.

    **Live bug fixed 2026-08-19**: `PositionManager._run_cycle` used to
    resolve one broker via `get_execution_broker(trading_session)` (no
    `strategy_run`) for the *entire* cycle and reuse it to evaluate/close
    every open position regardless of which strategy opened it —
    `close_position`'s own fallback resolution below had the identical
    gap. Once a session reaches `live_enabled`, every open position's
    close attempt routed to the *real* Shoonya broker, including positions
    opened by a strategy explicitly marked `force_paper`. Confirmed live:
    a genuine `force_paper` strategy's paper position retried a real
    `PlaceOrder` call against the live account every ~4s, saved only by an
    unrelated Shoonya-side rejection (`ALGO_CHK: MKT Order type not
    allowed for API order`), not by anything in this codebase.
    `eod_square_off._square_off_all_open_positions` (shared by EOD and
    margin-breach square-off) had the same shape — a single broker applied
    to every position being force-closed — and is fixed the same way, via
    this same helper.
    """
    strategy_run: StrategyRun | None = None
    trade_intent = db.get(TradeIntent, position.trade_intent_id)
    if trade_intent is not None:
        strategy_run = db.get(StrategyRun, trade_intent.strategy_run_id)
    return get_execution_broker(trading_session, strategy_run, position=position)


def close_position(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    exit_reason: ExitReason,
    intended_price: float,
    broker: BrokerPort | None = None,
) -> TradeOutcome | None:
    """Idempotent no-op (returns `None`) if the position is already closed —
    stop/target/trail checks and EOD square-off can all race to close the
    same position (e.g. price crosses stop right at cutoff_time); the second
    caller must not double-exit. `intended_price` is the price level that
    justified this exit (stop_price/target_price/the trail's current stop,
    or the current market price for EOD/manual) — it's what `slippage` is
    measured against, not a duplicate of the actual fill price.
    """
    # position-aware resolution: a position genuinely opened live must
    # always be closeable for real, regardless of current SafeMode
    # (kill_switch/degraded_mode exist to stop *new* risk, not strand
    # *existing* real risk) -- see get_execution_broker's own docstring.
    # order_mode: see the identical, identically-reasoned fix in
    # dispatch_trade_intent -- decided purely by `is_execution_broker_live
    # (broker)`, not by whether the caller passed `broker=` explicitly.
    # `eod_square_off.py`'s callers (EOD/margin-breach/manual square-off)
    # and `PositionManager`'s stop/target/trail loop (`evaluate_open_
    # position`) both pre-resolve the correct broker once, to reuse it for
    # pricing, and pass it in here explicitly -- the old `broker_was_
    # provided` guard force-tagged every one of those exits PAPER even when
    # the broker actually resolved was the real one.
    broker = broker or resolve_broker_for_position(db, trading_session, position)
    order_mode = OrderMode.LIVE if is_execution_broker_live(broker) else OrderMode.PAPER

    with advisory_lock(db, LOCK_EXECUTION_SINGLETON):
        if position.status != PositionStatus.OPEN:
            return None

        option_contract = db.get(OptionContract, position.option_contract_id)
        if option_contract is None:
            raise ValueError(f"unknown option_contract_id {position.option_contract_id}")
        instrument = db.get(Instrument, option_contract.instrument_id)
        if instrument is None:
            raise ValueError(f"unknown instrument for option_contract {option_contract.id}")

        entry_side = SignalSide(position.side)
        exit_side = _opposite(entry_side)
        now = _utcnow()
        exit_idempotency_key = f"exit:{position.id}"

        # LIVE-only resting-protective-stop safety invariant: never have
        # both a resting SL-LMT and a fresh exit order active for the same
        # position at once. `evaluate_open_position`'s own STOP check
        # already skips itself while a resting order exists (see that
        # function's own comment), so `exit_reason` reaching here is never
        # STOP for a position with one -- every other exit reason must
        # cancel the resting order first.
        stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one_or_none()
        if stop_plan is not None and stop_plan.resting_order_id is not None:
            cancel_outcome = cancel_resting_protective_stop(
                db, trading_session, position, stop_plan, stop_plan.resting_order_id, broker
            )
            if cancel_outcome is CancelOutcome.ALREADY_FILLED:
                # Reality beat us to it -- the resting stop fired before
                # our cancel reached the broker. Finalize as STOP using
                # its own real fill data instead of placing a redundant
                # second exit order.
                stop_order = (
                    db.query(Order)
                    .filter(Order.idempotency_key == f"stop:{position.id}")
                    .one_or_none()
                )
                # Same defensive guard `_apply_resolved_pending_exit_order`
                # already applies before calling `_finalize_position_close`
                # -- a malformed FILLED response with no fill price would
                # otherwise crash on `_dec(None)` rather than leaving the
                # position OPEN for the next cycle to sort out.
                if stop_order is None or stop_order.avg_fill_price is None:
                    return None
                outcome = _finalize_position_close(
                    db, trading_session, position, stop_order, ExitReason.STOP, order_mode, None
                )
                db.flush()
                run_reconciliation(db, broker, trading_session, ReconciliationTrigger.EVENT)
                return outcome
            if cancel_outcome is CancelOutcome.FAILED:
                # Can't confirm the resting stop is gone -- never place a
                # second live exit order while that's ambiguous; the
                # SystemAlert already raised inside the helper covers
                # alerting, and the position stays OPEN for the next
                # cycle/manual intervention to retry.
                return None
            # cancel_outcome is CancelOutcome.CANCELLED -- fall through to
            # the existing exit-order placement logic below, unchanged.

        exit_order = (
            db.query(Order).filter(Order.idempotency_key == exit_idempotency_key).one_or_none()
        )
        if exit_order is None:
            # intended_price is already the price level that justified this
            # exit (stop/target/trail/structure-break/spread-blowout, or the
            # current market price for EOD/manual) -- pass it through as the
            # fill basis, same reasoning as the entry side in
            # dispatch_trade_intent, instead of leaving MockBrokerAdapter to
            # fill at its own independent synthetic price.
            if order_mode == OrderMode.LIVE:
                run_preflight_checks(broker, trading_session=trading_session)
                _raise_if_option_chain_stale(db, trading_session, option_contract)

            # order_type: same 2026-08-19 live-correction as
            # dispatch_trade_intent's own entry order -- see
            # _resolve_order_pricing's own docstring for the real Shoonya
            # rejection this fixes and why the buffer setting is a separate
            # one from paper's fill_slippage_pct.
            exit_limit_price, exit_broker_order_type, exit_domain_order_type = (
                _resolve_order_pricing(
                    order_mode, _dec(intended_price), exit_side, _dec(instrument.tick_size)
                )
            )

            order_result = broker.place_order(
                OrderRequest(
                    idempotency_key=exit_idempotency_key,
                    contract_symbol=option_contract.symbol,
                    side=_to_broker_side(exit_side),
                    order_type=exit_broker_order_type,
                    qty=position.qty,
                    limit_price=exit_limit_price,
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
                order_type=exit_domain_order_type,
                qty=position.qty,
                idempotency_key=exit_idempotency_key,
                now=now,
                position_id=position.id,
                # The caller's real reason for this exit, captured now --
                # before it's known whether this order will fill
                # synchronously below or needs to be picked up later by
                # reconcile_pending_live_exit_orders. See this column's own
                # docstring on Order for why.
                intended_exit_reason=exit_reason,
            )
            db.add(exit_order)
            db.flush()
            db.add(
                _new_order_event(exit_order.id, order_result, event_type="filled", now=now)
            )

        # Narrowly scoped: only a non-FILLED (or price-less) exit order hits
        # this — normal exits, the only path reachable in production today
        # (MockBrokerAdapter always fills; Shoonya's real place_order falls
        # back to get_order_status on an ack timeout), are unaffected. Left
        # OPEN rather than marked CLOSED off no/partial fill data, so the
        # next PositionManager cycle or a manual reconcile can still see and
        # retry it — no new state machine, reuses the existing SystemAlert
        # pattern every other hard-stop condition in this codebase already
        # uses.
        if exit_order.status != OrderStatus.FILLED or exit_order.avg_fill_price is None:
            logger.error(
                "exit order for position %s did not fill (status=%s) — "
                "leaving position OPEN for reconciliation/retry",
                position.id,
                exit_order.status,
            )
            send_alert(
                db,
                workspace_id=trading_session.workspace_id,
                trading_session_id=trading_session.id,
                severity=AlertSeverity.CRITICAL,
                category="exit_order_unfilled",
                message=(
                    f"Exit order for position {position.id} did not fill "
                    f"(status={exit_order.status}); position left OPEN."
                ),
                mode=order_mode,
                dedup_key=f"exit_order_unfilled:{position.id}",
            )
            return None

        outcome = _finalize_position_close(
            db, trading_session, position, exit_order, exit_reason, order_mode, intended_price
        )
        db.flush()
        run_reconciliation(db, broker, trading_session, ReconciliationTrigger.EVENT)
        return outcome


def _finalize_position_close(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    exit_order: Order,
    exit_reason: ExitReason,
    order_mode: OrderMode,
    intended_price: float | None,
) -> TradeOutcome:
    """PnL/slippage, StopPlan/TrailPlan updates, TradeOutcome, and risk
    effects for a *filled* exit order — shared by `close_position` (the
    synchronous, common case) and `_apply_resolved_pending_exit_order` (an
    exit order that didn't fill synchronously, discovered filled later by
    `reconcile_pending_live_exit_orders`) so the two paths can never
    silently diverge. `intended_price` is the price level that justified
    the exit (see `close_position`'s own docstring) — `None` for the
    reconciled-late case, where the original trigger price was never
    persisted anywhere and there's nothing honest to measure slippage
    against; slippage is 0 in that case, not a fabricated number.
    """
    now = _utcnow()
    entry_side = SignalSide(position.side)
    exit_price = _dec(exit_order.avg_fill_price)
    entry_price = _dec(position.entry_price)
    qty = Decimal(position.qty)
    # Same shared sign convention `risk_engine.service
    # .compute_pre_trade_analytics` and `api.v1.execution`'s unrealized P&L
    # use — see app.core.pnl.signed_pnl's own docstring for why this used
    # to be hand-copied in three places.
    realized_pnl = signed_pnl(entry_price, exit_price, qty, entry_side)
    slippage = (
        signed_pnl(intended_price, exit_price, qty, entry_side)
        if intended_price is not None
        else Decimal("0")
    )

    position.status = PositionStatus.CLOSED
    position.closed_at = now
    position.closing_order_id = exit_order.id
    db.add(position)

    # Releases this position's half of the sleep inhibitor's reference
    # count — see the matching acquire in _open_position_from_fill.
    get_sleep_inhibitor().release(f"position:{position.id}")

    # No matching unsubscribe call here — see _open_position_from_fill's
    # own comment for why this module never touches market-data
    # subscriptions at all. PositionManager owns that lifecycle.

    stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one_or_none()
    if stop_plan is not None and stop_plan.status not in (
        StopPlanStatus.TRIGGERED,
        StopPlanStatus.CANCELLED,
    ):
        stop_plan.status = (
            StopPlanStatus.TRIGGERED if exit_reason == ExitReason.STOP else StopPlanStatus.CANCELLED
        )
        stop_plan.updated_at = now
        # Clears the resting-protective-stop bookkeeping for the path that
        # actually reaches `_finalize_position_close` -- but this is *not*
        # the only place that has to: a late-discovered async fill that
        # `_apply_resolved_pending_exit_order` handles via its own
        # `elif is_protective_stop` branch, and `close_position`'s own
        # CANCELLED-then-no-refill path, both close a position without ever
        # calling this function, and clear the same triple independently.
        # See those two call sites if this field's own semantics ever
        # change -- all three need to move together.
        stop_plan.resting_order_id = None
        stop_plan.resting_order_price = None
        db.add(stop_plan)

    trail_plan = db.query(TrailPlan).filter(TrailPlan.position_id == position.id).one_or_none()
    if trail_plan is not None and trail_plan.status != TrailPlanStatus.TRIGGERED:
        if exit_reason == ExitReason.TRAIL:
            trail_plan.status = TrailPlanStatus.TRIGGERED
            trail_plan.updated_at = now
            db.add(trail_plan)

    outcome = TradeOutcome(
        id=uuid.uuid4(),
        workspace_id=trading_session.workspace_id,
        trading_session_id=trading_session.id,
        position_id=position.id,
        trade_intent_id=position.trade_intent_id,
        entry_price=float(entry_price),
        exit_price=float(exit_price),
        qty=position.qty,
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
        event_type="position.closed",
        entity_type="position",
        entity_id=position.id,
        trading_session_id=trading_session.id,
        payload={
            "exit_reason": exit_reason.value,
            "realized_pnl": float(realized_pnl),
            "slippage": float(slippage),
        },
    )

    record_trade_outcome_effects(
        db, trading_session, float(realized_pnl), is_live=(order_mode == OrderMode.LIVE)
    )

    return outcome


def reconcile_pending_live_exit_orders(
    db: Session,
    trading_session: TradingSession,
    *,
    allow_rest_fallback: bool,
    broker: BrokerPort | None = None,
) -> None:
    """Exit-side counterpart to `reconcile_pending_live_orders` (2026-08-20)
    -- an exit order that doesn't fill synchronously in `close_position`
    already leaves the position correctly OPEN (see that function's own
    "leaving position OPEN for reconciliation/retry" comment and its
    `exit_order_unfilled` SystemAlert), but nothing ever actually
    implemented that retry until now. Same two-layer WS-cache + throttled-
    REST-fallback detection shape and the same `allow_rest_fallback`
    cadence contract as the entry-side function — see that one's own
    docstring for the full reasoning (not repeated here).

    Only ever finds rows for LIVE exit orders — a PAPER exit always fills
    synchronously via `MockBrokerAdapter`, same reasoning as the entry
    side. `broker` is the same explicit test-injection override as
    `reconcile_pending_live_orders`'s own param of the same name —
    production never passes it, each order resolves its own broker via
    `resolve_broker_for_position` instead.
    """
    pending_orders = (
        db.query(Order)
        .filter(
            Order.trading_session_id == trading_session.id,
            Order.mode == OrderMode.LIVE,
            Order.position_id.is_not(None),
            Order.status.in_(
                [OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED]
            ),
        )
        .all()
    )
    for order in pending_orders:
        position = db.get(Position, order.position_id)
        if position is None or position.status != PositionStatus.OPEN:
            continue
        try:
            resolved_broker = broker or resolve_broker_for_position(db, trading_session, position)

            result = _peek_order_update(resolved_broker, order.broker_order_id)
            if result is None and allow_rest_fallback:
                result = resolved_broker.get_order_status(order.broker_order_id)
        except Exception:  # noqa: BLE001 - a PositionManager cycle must never die
            # 2026-08-26: widened from a narrow `except BrokerError` around
            # only the REST call to wrap `resolve_broker_for_position` too,
            # mirroring `reconcile_pending_live_orders`'s own identical fix
            # (2026-08-19, see that function's own docstring). Same failure
            # mode, same root cause: right after a restart with Shoonya not
            # yet reconnected, `resolve_broker_for_position` ->
            # `get_execution_broker` raises `ConfigurationError` for a
            # genuinely-live position -- deliberately *not* a `BrokerError`
            # subclass, so the old narrow catch never saw it, and this
            # aborted the entire `_run_cycle` every ~3s: skipping
            # `run_eod_square_off` and `run_full_reconciliation` for every
            # other position that cycle too, not just this order's own
            # reconciliation, and freezing the REST-poll-fallback throttle
            # at whatever state it was in when the failure started. Catching
            # `ConfigurationError` here is deliberately scoped to this
            # status-*polling* loop, not a dispatch decision -- it does not
            # relax `ConfigurationError`'s own contract elsewhere (it must
            # still propagate and abort a *new* live dispatch attempt); this
            # loop just can't let one order's transient broker-resolution
            # failure take down every other position's handling for the
            # rest of the cycle, same reasoning the entry-side fix already
            # established.
            logger.exception(
                "reconciling pending exit order %s (broker_order_id=%s) failed",
                order.id,
                order.broker_order_id,
            )
            continue

        if result is None or result.status not in _TERMINAL_BROKER_ORDER_STATUSES:
            continue

        _apply_resolved_pending_exit_order(db, trading_session, position, order, result)
        db.flush()


def _apply_resolved_pending_exit_order(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    order: Order,
    result: OrderResult,
) -> None:
    now = _utcnow()
    order.status = _map_status(result.status)
    order.filled_qty = result.filled_qty
    order.avg_fill_price = result.avg_fill_price
    order.updated_at = now
    db.add(order)

    db.add(
        _new_order_event(
            order.id,
            result,
            event_type="filled" if order.status == OrderStatus.FILLED else "resolved",
            now=now,
            source="reconcile_pending_live_exit_orders",
        )
    )

    # `stop:` orders are this position's own resting protective SL-LMT
    # (see `place_protective_stop`) -- distinct idempotency-key prefix
    # from the `exit:` orders `close_position` places, so its own fill is
    # always a genuine STOP exit regardless of anything else. Otherwise,
    # `close_position` already recorded the caller's real reason on
    # `intended_exit_reason` at placement time (2026-08-25) -- use it
    # instead of defaulting to the generic `RECONCILED`, which is now only
    # the honest answer for an order that genuinely never had one recorded
    # (e.g. a row from before this field existed). See `ExitReason
    # .RECONCILED`'s own docstring for the full incident this closes.
    is_protective_stop = order.idempotency_key.startswith("stop:")
    resolved_exit_reason: ExitReason | None = None

    if order.status == OrderStatus.FILLED and order.avg_fill_price is not None:
        if is_protective_stop:
            exit_reason = ExitReason.STOP
        elif order.intended_exit_reason is not None:
            exit_reason = ExitReason(order.intended_exit_reason)
        else:
            exit_reason = ExitReason.RECONCILED
        resolved_exit_reason = exit_reason
        _finalize_position_close(
            db,
            trading_session,
            position,
            order,
            exit_reason,
            OrderMode(order.mode),
            None,
        )
    elif is_protective_stop:
        # The resting stop resolved to CANCELLED/REJECTED without this app
        # having initiated the cancel itself -- `close_position`'s own
        # `cancel_resting_protective_stop` already clears
        # `resting_order_id` synchronously when *it* cancels one, so this
        # only fires for a broker-unilateral cancel/rejection discovered
        # here later. Position is untouched either way -- just stop
        # pointing at a dead order.
        stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one_or_none()
        if stop_plan is not None and stop_plan.resting_order_id == order.broker_order_id:
            stop_plan.resting_order_id = None
            stop_plan.resting_order_price = None
            stop_plan.updated_at = now
            db.add(stop_plan)
    # else: still REJECTED/CANCELLED discovered late for a normal ("exit:")
    # order -- position correctly stays OPEN, close_position's own
    # exit_order_unfilled SystemAlert already covers alerting; nothing
    # further to do beyond recording the terminal order state itself,
    # above.

    record_event(
        db,
        workspace_id=trading_session.workspace_id,
        actor_type=ActorType.SYSTEM,
        event_category=EventCategory.ORDER_LIFECYCLE,
        event_type="order.reconciled",
        entity_type="order",
        entity_id=order.id,
        trading_session_id=trading_session.id,
        payload={
            "status": order.status.value,
            "filled_qty": order.filled_qty,
            **(
                {"exit_reason": resolved_exit_reason.value}
                if resolved_exit_reason is not None
                else {}
            ),
        },
    )


def evaluate_open_position(
    db: Session,
    trading_session: TradingSession,
    position: Position,
    tick_price: float,
    broker: BrokerPort | None = None,
    bid: float | None = None,
    ask: float | None = None,
    underlying_price: float | None = None,
) -> TradeOutcome | None:
    """Checks stop/target/structure-break/spread-blowout/trail against
    `tick_price` (plus, for the two Phase 4 checks, the option's own live
    `bid`/`ask` and the *underlying's* current price) and closes the position
    if triggered; otherwise advances the trail plan per the generic Phase-3
    rule. Called by `PositionManager` on every price poll. By contrast,
    `scheduler.eod_square_off` is *not* routed through here — EOD is an
    unconditional force-close regardless of where price sits.

    `bid`/`ask`/`underlying_price` are optional: a position whose
    `stop_plan.structure_level` is null (any strategy that doesn't set one,
    e.g. SyntheticStrategy) simply never triggers the structure-break check
    regardless, and callers that don't pass bid/ask (existing tests) just
    skip the spread-blowout check the same way.

    Structure-break confirmation (candidate/confirm/reclaim): a bare
    `underlying < structure_level` on a single raw tick, with zero debounce,
    was confirmed live to kill ~65%+ of trades within seconds across every
    real strategy on pure tick noise, not genuine reversals (see project
    memory `project_structure_break_noise_bug_2026_08_21`). The breach must
    now clear `structure_level` by more than `stop_plan.structure_break_
    buffer` (an ATR-scaled margin, frozen at signal time) *and* persist for
    at least `stop_plan.structure_break_persistence_seconds`
    (`structure_break_candidate_since`, stamped on the first breaching tick
    and cleared the instant price reclaims the level, is what measures
    persistence across `PositionManager`'s ~3s poll cycle — not a literal
    timer/thread). Either field being null (old rows, any strategy that
    doesn't opt in) falls back to a zero buffer and zero persistence, i.e.
    today's exact prior instant-exit behavior — this is deliberately
    byte-identical for anything not opted in, so the fix is safe to ship
    even with positions already open.

    Once persistence elapses (only when `structure_break_persistence_
    seconds > 0`, i.e. a strategy that opted in), `_structure_break_
    confirmed_by_bar_close` additionally requires the latest *completed*
    underlying bar to have actually closed beyond the buffered level before
    confirming — a live-tick timer alone can still fire on pure intra-bar
    noise that recovers by the time the bar closes (live-observed
    2026-08-24: a 120s-persistence trade still confirmed this way, exit PnL
    still positive). If the latest bar closed back inside the level, the
    candidate stays active and is re-checked next poll rather than firing.
    """
    if position.status != PositionStatus.OPEN:
        return None

    trade_intent = db.get(TradeIntent, position.trade_intent_id)
    if trade_intent is None:
        return None
    stop_plan = db.query(StopPlan).filter(StopPlan.position_id == position.id).one_or_none()
    trail_plan = db.query(TrailPlan).filter(TrailPlan.position_id == position.id).one_or_none()
    if stop_plan is None:
        return None

    side = SignalSide(position.side)
    price = _dec(tick_price)
    stop_price = _dec(stop_plan.stop_price)
    target_price = _dec(trade_intent.target_price)
    favorable = side == SignalSide.BUY

    # 1. Stop hit (checked first — capital preservation takes priority over
    # a target that happens to be hit the same tick, which can't actually
    # occur for a sane stop < entry < target but is checked in this order
    # regardless, for defense-in-depth).
    #
    # Skipped entirely when a LIVE resting protective SL-LMT already exists
    # (`stop_plan.resting_order_id` set, see `place_protective_stop`) --
    # the broker is now the authoritative stop mechanism for this position,
    # and this local check firing at the same time would race
    # `close_position` against `reconcile_pending_live_exit_orders`'s own
    # async discovery of that same order's fill, risking a double-exit
    # (the exact invariant the resting-stop design must never violate). A
    # position whose protective placement failed (`resting_order_id` still
    # `None`, the documented fallback) keeps this check exactly as today.
    hit_stop = price <= stop_price if favorable else price >= stop_price
    if hit_stop and stop_plan.resting_order_id is None:
        return close_position(
            db, trading_session, position, ExitReason.STOP, float(stop_price), broker=broker
        )

    # 2. Target hit.
    hit_target = price >= target_price if favorable else price <= target_price
    if hit_target:
        return close_position(
            db, trading_session, position, ExitReason.TARGET, float(target_price), broker=broker
        )

    # 3. Structure break: the underlying-index level (opening-range boundary
    # / pullback extreme / EMA9) that justified this setup has been crossed
    # unfavorably — exit even though the option premium hasn't hit its own
    # stop yet. Skipped when either side of the comparison is unavailable
    # (no structure_level set, or no underlying_price supplied). See this
    # function's own docstring for the candidate/confirm/reclaim design.
    if stop_plan.structure_level is not None and underlying_price is not None:
        structure_level = _dec(stop_plan.structure_level)
        underlying = _dec(underlying_price)
        buffer = _dec(stop_plan.structure_break_buffer or 0)
        persistence_seconds = float(stop_plan.structure_break_persistence_seconds or 0)

        buffered_level = structure_level - buffer if favorable else structure_level + buffer
        breached = underlying < buffered_level if favorable else underlying > buffered_level

        if breached:
            if stop_plan.structure_break_candidate_since is None:
                stop_plan.structure_break_candidate_since = _utcnow()
                stop_plan.structure_break_candidate_extreme = underlying_price
            else:
                prior_extreme = _dec(stop_plan.structure_break_candidate_extreme)
                worse = (
                    underlying < prior_extreme if favorable else underlying > prior_extreme
                )
                if worse:
                    stop_plan.structure_break_candidate_extreme = underlying_price
            db.add(stop_plan)
            db.flush()

            elapsed = (_utcnow() - stop_plan.structure_break_candidate_since).total_seconds()
            if elapsed >= persistence_seconds:
                if persistence_seconds > 0:
                    option_contract = db.get(OptionContract, position.option_contract_id)
                    confirmed = option_contract is not None and (
                        _structure_break_confirmed_by_bar_close(
                            db, option_contract.instrument_id, buffered_level, favorable
                        )
                    )
                else:
                    # Not opted in (persistence unset) -- preserve the exact
                    # original single-tick instant-exit behavior.
                    confirmed = True
                if confirmed:
                    return close_position(
                        db,
                        trading_session,
                        position,
                        ExitReason.STRUCTURE_BREAK,
                        float(price),
                        broker=broker,
                    )
        elif stop_plan.structure_break_candidate_since is not None:
            # Reclaimed — price came back inside the buffered level before
            # persistence was satisfied. Cancel the candidate rather than
            # let a stale timestamp confirm a break on some later, unrelated
            # breach.
            stop_plan.structure_break_candidate_since = None
            stop_plan.structure_break_candidate_extreme = None
            db.add(stop_plan)
            db.flush()

    # 4. Spread blowout: the option's own liquidity has dried up past a
    # tradeable width — exit at the current price rather than risk being
    # stuck in an illiquid contract waiting for stop/target. Generic
    # (SPREAD_BLOWOUT_PCT), not per-strategy, and skipped when bid/ask aren't
    # supplied (existing tests that only pass tick_price).
    if bid is not None and ask is not None and price > 0:
        spread_pct = _dec(ask - bid) / price
        if spread_pct > SPREAD_BLOWOUT_PCT:
            return close_position(
                db,
                trading_session,
                position,
                ExitReason.SPREAD_BLOWOUT,
                float(price),
                broker=broker,
            )

    # 5. Trail: activate once favorable move reaches the activation price;
    # once active, tighten (never loosen) an independent trailing stop
    # (`trail_plan.current_stop_price`) to lock in TRAIL_LOCK_FRACTION of
    # movement beyond activation, and exit if price pulls back through it.
    # Deliberately never writes the trailed level back onto
    # `stop_plan.stop_price` — that stays the original mandatory stop for
    # the life of the position (checked in step 1 above), so a STOP exit and
    # a TRAIL exit stay distinguishable outcomes instead of the trail
    # silently turning every later exit into a "stop hit".
    if trail_plan is not None and trail_plan.status != TrailPlanStatus.TRIGGERED:
        activation_price = _dec(trail_plan.activation_price)
        activated = price >= activation_price if favorable else price <= activation_price

        if activated:
            gain_beyond_activation = (
                (price - activation_price) if favorable else (activation_price - price)
            )
            locked_gain = gain_beyond_activation * _dec(trail_plan.trail_value)
            new_trail_stop = (
                activation_price + locked_gain if favorable else activation_price - locked_gain
            )

            current = (
                _dec(trail_plan.current_stop_price)
                if trail_plan.current_stop_price is not None
                else None
            )
            tightened = current is None or (
                new_trail_stop > current if favorable else new_trail_stop < current
            )
            if tightened:
                trail_plan.current_stop_price = float(new_trail_stop)
                trail_plan.status = TrailPlanStatus.ACTIVE
                trail_plan.updated_at = _utcnow()
                db.add(trail_plan)
                db.flush()
            else:
                new_trail_stop = current if current is not None else new_trail_stop

            # Strict inequality: on the very tick the trail activates (or
            # tightens), new_trail_stop is derived from this same price, so
            # they can be equal — a <= here would fire a spurious exit on
            # the activation tick itself instead of only once price later
            # actually pulls back through the trailed level.
            hit_trail = price < new_trail_stop if favorable else price > new_trail_stop
            if hit_trail:
                return close_position(
                    db,
                    trading_session,
                    position,
                    ExitReason.TRAIL,
                    float(new_trail_stop),
                    broker=broker,
                )

            # LIVE-only TSL: keep the resting protective stop's own
            # trigger/limit in step with whichever level is now the
            # current protective floor. Checked every cycle the trail is
            # active (not just when `tightened` fired this cycle) so a
            # previously failed sync keeps retrying — see
            # `sync_resting_protective_stop`'s own docstring for the
            # fallback when a `ModifyOrder` call is rejected. Skipped on
            # the same cycle `hit_trail` fires above (position is closing
            # anyway, `close_position`'s own Path B cancels the resting
            # order right after this returns).
            if stop_plan.resting_order_id is not None:
                sync_resting_protective_stop(
                    db,
                    trading_session,
                    position,
                    stop_plan,
                    stop_plan.resting_order_id,
                    new_trail_stop,
                    broker,
                )

    return None


def current_contract_price(
    db: Session,
    option_contract: OptionContract,
    broker: BrokerPort,
    *,
    market_data_provider: BaseMarketDataProvider | None = None,
    session_factory: SessionFactory | None = None,
) -> Tick:
    """The current price for one option contract, preferring a live WS tick
    and otherwise falling back to the same REST-based `OptionChainSnapshot`
    every strategy's own `rank_from_latest_snapshot` already reads
    (refreshed via `market_data.freshness.ensure_fresh_option_chain`,
    threshold-gated -- not a REST call every poll) rather than
    `broker.get_quote()`. That fallback matters: for a paper-routed position
    `get_execution_broker()` resolves the mock, whose `get_quote()` returns
    its own synthetic, strategy-independent price -- the exact price-source
    mismatch this whole change exists to close (a genuinely live position's
    real broker quote wouldn't have this problem, but this function doesn't
    know which case it's in without checking, so it always prefers the
    snapshot). Shared by
    `PositionManager._run_cycle` (open-position stop/target/trail pricing)
    and `scheduler.eod_square_off._square_off_all_open_positions` (forced
    square-off pricing) so the fallback chain lives in exactly one place.

    This function only *reads* `market_data_provider`'s cache -- it never
    subscribes. `PositionManager` calls its own idempotent
    `_ensure_symbol_subscribed` first, so its live-tick branch has a real
    chance of succeeding (today, or automatically once a future per-contract
    WS fix lands, with no re-work needed here). `eod_square_off.py` doesn't
    subscribe before calling this -- a one-shot square-off subscribing
    right before immediately reading wouldn't have a tick ready anyway -- so
    its live-tick branch is normally a harmless no-op, straight to the
    REST-snapshot fallback.

    Only ever falls through to `broker.get_quote()` as an absolute last
    resort, matching `evaluate_open_position`'s own "never leave a stop
    check silently unevaluated" discipline -- always returns *something*.
    """
    if market_data_provider is not None:
        fresh_tick = fresh_tick_or_none(
            market_data_provider.get_latest_tick(option_contract.symbol), _utcnow()
        )
        if fresh_tick is not None:
            return fresh_tick

    freshness_state = ensure_fresh_option_chain(
        db,
        get_broker(),
        option_contract.instrument_id,
        option_contract.expiry_date,
        thresholds=OPTION_CHAIN_THRESHOLDS,
        session_factory=session_factory,
    )
    if freshness_state not in (FreshnessState.STALE, FreshnessState.DEAD):
        snapshot_tick = latest_snapshot_tick(
            db, option_contract.instrument_id, option_contract.expiry_date, option_contract.symbol
        )
        if snapshot_tick is not None:
            return snapshot_tick

    logger.warning(
        "no live tick or usable option-chain snapshot for %s; falling back to "
        "broker.get_quote as a last resort",
        option_contract.symbol,
    )
    return broker.get_quote(option_contract.symbol)
