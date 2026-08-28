"""Execution domain: the real Order/OrderEvent/Position/StopPlan/TrailPlan/
TradeOutcome lifecycle that Phase 3 introduces to replace Phase 2's
`SyntheticTradeOutcome` stand-in (see `app.modules.strategy_engine.service`'s
and `app.modules.risk_engine.service`'s module docstrings).

An `Order` is either an *entry* order (`trade_intent_id` set, `position_id`
null — created by `dispatch_trade_intent`) or an *exit* order (`position_id`
set, `trade_intent_id` null — created by `close_position` to flatten a
position on stop/target/trail/EOD/manual). Exactly one of the two is set,
mirroring the existing exactly-one-of-two-FKs convention already used by
`QuoteTick`/`IndicatorSnapshot` in `app.domain.market.models`.

`qty` on `Order`/`Position` is always the absolute quantity
(`qty_lots x instruments.lot_size`), resolved server-side by
`execution_engine.paper.service` at dispatch time — never accepted from a
caller, per the existing `qty_lots`-is-a-lot-count rule.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, UUIDPkMixin


class OrderMode(enum.StrEnum):
    PAPER = "paper"
    LIVE = "live"


class OrderSide(enum.StrEnum):
    """Duplicates `app.domain.strategy.models.SignalSide` /
    `app.modules.broker_adapter.base.contracts.OrderSide` deliberately —
    matches this codebase's existing convention of each domain owning its
    own copy of a shared value set rather than cross-importing another
    bounded context's enum (e.g. `app.domain.market.models.OptionType`
    already duplicates `broker_adapter.base.contracts.OptionType` the same
    way).
    """

    BUY = "buy"
    SELL = "sell"


class OrderType(enum.StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    SL_LIMIT = "sl_limit"
    SL_MARKET = "sl_market"


class OrderStatus(enum.StrEnum):
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    MODIFY_PENDING = "modify_pending"
    CANCEL_PENDING = "cancel_pending"


class PositionStatus(enum.StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class StopPlanStatus(enum.StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    TRIGGERED = "triggered"
    CANCELLED = "cancelled"


class TrailPlanStatus(enum.StrEnum):
    INACTIVE = "inactive"
    ACTIVE = "active"
    TRIGGERED = "triggered"


class ExitReason(enum.StrEnum):
    STOP = "stop"
    TARGET = "target"
    TRAIL = "trail"
    EOD_SQUARE_OFF = "eod_square_off"
    MANUAL = "manual"
    # Underlying-index structural invalidation — opening-range boundary
    # broken back through, pullback swing broken, or price closed back
    # through EMA9 — independent of whether the option premium's own
    # stop/target has been hit yet.
    STRUCTURE_BREAK = "structure_break"
    # The option's own bid/ask spread blew out past a tradeable width —
    # distinct from STRUCTURE_BREAK (a liquidity problem, not a setup
    # invalidation) so reports/scorecards can tell the two apart.
    SPREAD_BLOWOUT = "spread_blowout"
    # Emergency square-off's one narrow automatic trigger (Addendum
    # hardening batch) — a detected negative available margin on a
    # live session, distinct from EOD_SQUARE_OFF so reports can tell a
    # scheduled flatten from a forced one apart.
    MARGIN_BREACH = "margin_breach"
    # 2026-08-28: hard risk overlays independent of the premium stop/target
    # (TradeProposal.max_loss_per_lot / time_stop_minutes). MAX_LOSS = the
    # absolute per-lot INR loss cap was reached before stop_price; TIME_STOP
    # = held past time_stop_minutes without being in profit. Backtest exit
    # reconstruction only for now; production wiring is a gated follow-up.
    MAX_LOSS = "max_loss"
    TIME_STOP = "time_stop"
    # 2026-08-20: an exit order that didn't fill synchronously in
    # close_position (left the position OPEN, per that function's own
    # comment) and was only discovered filled later by
    # reconcile_pending_live_exit_orders. The original *trigger price* is
    # still gone by then (never persisted on the Order itself), so slippage
    # can't be measured against it the normal way and is reported as 0, not
    # fabricated -- that part is unchanged, deliberate design.
    #
    # 2026-08-25 correction: the *reason* itself is a different question
    # from the price, and unlike the price, it was always knowable --
    # close_position's caller (evaluate_open_position/eod_square_off/manual
    # square-off) already knows exactly why it's closing the position, it
    # just wasn't being written down before the async-fill uncertainty
    # window began. `Order.intended_exit_reason` now captures it at
    # placement time, so `_apply_resolved_pending_exit_order` reports the
    # *real* STOP/TARGET/TRAIL/etc. reason for a late-discovered exit
    # whenever it's known. RECONCILED is now the honest fallback only for
    # the case where no intended reason was ever recorded at all (e.g. an
    # `Order` row from before this field existed) -- not, as before, the
    # default for every late-discovered exit regardless of whether the real
    # reason was knowable.
    RECONCILED = "reconciled"


class Order(Base, UUIDPkMixin):
    __tablename__ = "orders"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    trading_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trading_sessions.id"))
    option_contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("option_contracts.id"))

    trade_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("trade_intents.id"), nullable=True
    )
    # use_alter: orders <-> positions is a circular FK pair (an entry Order
    # creates a Position, a Position's closing_order_id points back at the
    # exit Order) — deferring this one via ALTER is what lets both
    # Base.metadata.create_all (tests) and Alembic create the two tables at
    # all.
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("positions.id", use_alter=True, name="fk_orders_position_id"),
        nullable=True,
    )

    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    mode: Mapped[OrderMode] = mapped_column(String(10), default=OrderMode.PAPER)
    side: Mapped[OrderSide] = mapped_column(String(10))
    order_type: Mapped[OrderType] = mapped_column(String(20), default=OrderType.MARKET)
    qty: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    trigger_price: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)

    status: Mapped[OrderStatus] = mapped_column(String(20), default=OrderStatus.PENDING)
    filled_qty: Mapped[int] = mapped_column(Integer, default=0)
    avg_fill_price: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    broker_order_id: Mapped[str] = mapped_column(String(60), default="")

    # 2026-08-25: the *caller's* real reason for placing this exit order --
    # set only by close_position, only for the `exit:{position_id}` order it
    # creates, at the moment of placement (before it's known whether the
    # order will fill synchronously or not). Exists specifically so
    # `reconcile_pending_live_exit_orders` -> `_apply_resolved_pending_exit_
    # order` can recover the true STOP/TARGET/TRAIL/etc. reason for a LIVE
    # exit order that didn't fill synchronously, instead of defaulting every
    # late-discovered exit to the generic `ExitReason.RECONCILED` -- see
    # that enum member's own docstring for the full incident this closes.
    # `None` for entry orders and for pre-migration exit orders (honestly
    # unknown, correctly falls back to RECONCILED).
    intended_exit_reason: Mapped[ExitReason | None] = mapped_column(String(20), nullable=True)

    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "(trade_intent_id IS NOT NULL) <> (position_id IS NOT NULL)",
            name="ck_order_exactly_one_of_intent_or_position",
        ),
        Index("ix_orders_session", "trading_session_id"),
        Index("ix_orders_trade_intent", "trade_intent_id"),
        Index("ix_orders_position", "position_id"),
    )


class OrderEvent(Base, UUIDPkMixin):
    __tablename__ = "order_events"

    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"))
    event_type: Mapped[str] = mapped_column(String(40))
    raw_payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_order_events_order", "order_id"),)


class Position(Base, UUIDPkMixin):
    __tablename__ = "positions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    trading_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trading_sessions.id"))
    option_contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("option_contracts.id"))
    trade_intent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trade_intents.id"), unique=True)
    opening_order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"))
    closing_order_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("orders.id"), nullable=True
    )

    side: Mapped[OrderSide] = mapped_column(String(10))
    qty: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float] = mapped_column(Numeric(12, 4))
    status: Mapped[PositionStatus] = mapped_column(String(10), default=PositionStatus.OPEN)

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_positions_session_status", "trading_session_id", "status"),
        Index("ix_positions_option_contract", "option_contract_id"),
    )


class StopPlan(Base, UUIDPkMixin):
    __tablename__ = "stop_plans"

    position_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("positions.id"), unique=True)
    stop_price: Mapped[float] = mapped_column(Numeric(12, 4))
    qty: Mapped[int] = mapped_column(Integer)
    # A second, independent invalidation level on the *underlying's* own
    # price (opening-range boundary / pullback extreme / EMA9) — distinct
    # from stop_price, which is always on the option premium. Null for any
    # strategy that doesn't supply one (e.g. SyntheticStrategy); see
    # execution_engine.paper.service.evaluate_open_position.
    structure_level: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    # ATR-scaled minimum-breach margin (underlying index points) and minimum
    # persistence window (seconds) a structure_level breach must hold before
    # counting as a confirmed break -- frozen at signal time (copied from
    # TradeIntent), never recomputed live. Null on either means "no buffer /
    # confirm immediately", i.e. today's exact prior instant-exit behavior --
    # see evaluate_open_position's own docstring for the full state machine.
    structure_break_buffer: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    structure_break_persistence_seconds: Mapped[float | None] = mapped_column(
        Numeric(6, 2), nullable=True
    )
    # Mutable, unlike everything else on this row: set to the tick timestamp
    # the moment price first breaches structure_level by more than the
    # buffer, cleared back to None the instant price reclaims the level.
    # `evaluate_open_position` confirms the break only once
    # `now - structure_break_candidate_since >= structure_break_persistence_
    # seconds`. `structure_break_candidate_extreme` tracks the worst
    # excursion seen during the current candidate window, for diagnostics/
    # reporting only -- never read by the confirm decision itself.
    structure_break_candidate_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    structure_break_candidate_extreme: Mapped[float | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    status: Mapped[StopPlanStatus] = mapped_column(String(20), default=StopPlanStatus.CONFIRMED)
    # The broker's own order id for this position's currently-resting,
    # LIVE-only protective SL-LMT order -- `None` means there isn't one
    # right now (never placed, placement failed and fell back to local-only
    # monitoring, already cancelled, or already filled/finalized). Never a
    # second, parallel state enum alongside `status` above -- presence/
    # absence of this field alone is the single source of truth for
    # whether a resting order currently exists; `status` still owns the
    # plan's own lifecycle. See execution_engine.paper.service's
    # `place_protective_stop`/`cancel_resting_protective_stop`.
    resting_order_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # The trigger price last *successfully confirmed* (Shoonya `ModifyOrder`
    # didn't raise) armed at the broker for `resting_order_id` -- not merely
    # "the price we last computed locally," which could differ if a
    # `ModifyOrder` call failed. This is what makes TSL-via-ModifyOrder
    # self-healing: `sync_resting_protective_stop`'s own retry-until-
    # confirmed logic compares the current best local level against *this*
    # field, not against `trail_plan.current_stop_price` directly, so a
    # failed sync attempt is retried on every later cycle (the two values
    # keep disagreeing) rather than only on the next real tightening event.
    # `None` whenever `resting_order_id` is `None` (cleared together).
    resting_order_price: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TrailPlan(Base, UUIDPkMixin):
    __tablename__ = "trail_plans"

    position_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("positions.id"), unique=True)
    trail_type: Mapped[str] = mapped_column(String(40))
    activation_price: Mapped[float] = mapped_column(Numeric(12, 4))
    trail_value: Mapped[float] = mapped_column(Numeric(12, 4))
    current_stop_price: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    status: Mapped[TrailPlanStatus] = mapped_column(String(20), default=TrailPlanStatus.INACTIVE)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TradeOutcome(Base, UUIDPkMixin):
    __tablename__ = "trade_outcomes"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    trading_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trading_sessions.id"))
    position_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("positions.id"), unique=True)
    trade_intent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trade_intents.id"))

    entry_price: Mapped[float] = mapped_column(Numeric(12, 4))
    exit_price: Mapped[float] = mapped_column(Numeric(12, 4))
    qty: Mapped[int] = mapped_column(Integer)
    realized_pnl: Mapped[float] = mapped_column(Numeric(14, 2))
    slippage: Mapped[float] = mapped_column(Numeric(14, 2))
    exit_reason: Mapped[ExitReason] = mapped_column(String(20))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_trade_outcomes_session", "trading_session_id"),
        UniqueConstraint("position_id", name="uq_trade_outcome_position"),
    )
