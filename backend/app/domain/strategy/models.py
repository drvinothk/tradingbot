"""Strategy runtime domain. `StrategyConfig` is a strategy's persistent
definition (`is_enabled` on/off, `runtime_mode` force-paper override,
`params`); `Signal`/`TradeIntent` are what every strategy — synthetic now,
real from Phase 4 — is allowed to emit, per the shared `Strategy` interface
in `app.modules.strategy_engine.interface` (no Order/Position access at any
layer beneath it). A TradeIntent's own status lifecycle ends at `DISPATCHED`
("handed to Execution Service") — what happens after that lives in
`app.domain.execution`'s Order/Position/TradeOutcome chain, not here.

2026-08-28: the `StrategyStatus` graduation ladder
(research/paper/paper_plus_guarded_live/live) was retired (migration 0028) —
it never had an API setter and its only remaining reader
(`broker_adapter.composition.is_strategy_routed_live`) now decides live
routing purely from the session's `SafeMode` plus `runtime_mode`.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
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

from app.core.db.base import Base, TimestampMixin, UUIDPkMixin


class StrategyRuntimeMode(enum.StrEnum):
    """Ops-Hardening Phase 1 (2026-08-14): a per-strategy "hold this one on
    paper even though the session is live" override. Deliberately
    downgrade-only, mirroring the `SafeMode` matrix's own "overrides only
    ever restrict, never expand" philosophy
    (`app.core.modes.state_machine`) — there is no `FORCE_LIVE` value; the
    only way to raise a strategy to real money is the session master switch
    (`SafeMode.LIVE_ENABLED`). `StrategyConfig.runtime_mode` is nullable;
    `None` means "no override — route per the session mode."
    """

    FORCE_PAPER = "force_paper"


class SignalSide(enum.StrEnum):
    BUY = "buy"
    SELL = "sell"


class TradeIntentStatus(enum.StrEnum):
    PENDING_RISK = "pending_risk"
    RISK_REJECTED = "risk_rejected"
    PENDING_APPROVAL = "pending_approval"
    HUMAN_REJECTED = "human_rejected"
    EXPIRED = "expired"
    DISPATCHED = "dispatched"


class ExecutionMode(enum.StrEnum):
    AUTO = "auto"
    APPROVAL_REQUIRED = "approval_required"


class StrategyRunStatus(enum.StrEnum):
    SCANNING = "scanning"
    IN_POSITION = "in_position"
    PAUSED = "paused"
    STOPPED = "stopped"


class ApprovalStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class StrategyConfig(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "strategy_configs"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    name: Mapped[str] = mapped_column(String(120))
    # "synthetic" | "orb" | "vwap_pullback" | "ema_micro_pullback" — which
    # Strategy subclass api.v1.strategies.start_strategy instantiates. Not an
    # enum.StrEnum column like this file's other status fields because new
    # strategy types are added by later phases (Phase 7/8) without a
    # migration touching every existing row's constraint.
    strategy_type: Mapped[str] = mapped_column(String(40), default="synthetic")
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Ops-Hardening Phase 1: master on/off switch for the daily bootstrapper.
    # Load-bearing for the Phase 4 daily bootstrapper — a freshly
    # auto-created `TradingSession` has no existing `StrategyRun` rows to
    # resume, so `is_enabled` is what tells that bootstrap which configs
    # should be auto-started each morning at all.
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    runtime_mode: Mapped[StrategyRuntimeMode | None] = mapped_column(
        String(30), nullable=True, default=None
    )
    # Ops-Hardening Phase 6 (Auto-Spawner): which underlying (Instrument.symbol,
    # e.g. "NIFTY"/"BANKNIFTY") the daily auto-spawner should resolve an
    # instrument_id/expiry_date for. Nullable -- an is_enabled config with no
    # underlying_symbol set is skipped (alerted, not guessed) by the spawner.
    # Previously this was request-only (POST /strategies/{id}/start's own
    # instrument_id), never persisted at the config level, since nothing
    # needed to auto-start a run without a human picking an instrument first.
    underlying_symbol: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # 2026-09-04: "I'm done with this config" -- distinct from is_enabled,
    # which stays the quick/temporary pause (no precondition, always was and
    # still is instant). Archiving is the more permanent "put it away"
    # action: `api.v1.strategies.archive_strategy` blocks it (409) while
    # anything is running against it -- an active StrategyRun, or an open
    # Position from one that's since been stopped (see that function's own
    # docstring) -- but does NOT require is_enabled to already be false;
    # archiving itself forces is_enabled=false unconditionally as part of
    # the same call, no separate "disable it first" step. Un-archiving just
    # clears this column -- it deliberately does NOT re-enable is_enabled,
    # so the operator flips that back on deliberately. Name stays unique
    # across archived+active rows (uq_strategy_config_name below is
    # untouched) so a still-archived name can't be silently reused by a new
    # config.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_strategy_config_name"),)


class StrategyRun(Base, UUIDPkMixin):
    __tablename__ = "strategy_runs"

    strategy_config_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategy_configs.id"))
    trading_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trading_sessions.id"))
    execution_mode: Mapped[ExecutionMode] = mapped_column(String(20))
    status: Mapped[StrategyRunStatus] = mapped_column(
        String(20), default=StrategyRunStatus.SCANNING
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    # `instrument_id`/`expiry_date`/`interval_seconds` were request params to
    # POST /strategies/{id}/start, never persisted — the *only* place that
    # combination lived was the in-memory Strategy object inside the runner
    # thread (see api.v1.strategies.list_running_strategies' own
    # data_freshness fallback: "None ... there's nothing to classify
    # freshness *of* in that case" when no live runner is registered). That
    # meant a StrategyRun could never be resumed after a restart even in
    # principle — nothing durable recorded *what* it was scanning. Nullable
    # since existing rows predate this column and can't be backfilled (the
    # original request body is gone); a resume pass must skip any row where
    # this is still NULL.
    instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("instruments.id"), nullable=True
    )
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    interval_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        Index("ix_strategy_runs_session", "trading_session_id"),
        Index("ix_strategy_runs_config", "strategy_config_id"),
    )


class Signal(Base, UUIDPkMixin):
    __tablename__ = "signals"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    strategy_config_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategy_configs.id"))
    strategy_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategy_runs.id"))
    trading_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trading_sessions.id"))
    option_contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("option_contracts.id"))

    side: Mapped[SignalSide] = mapped_column(String(10))
    entry_price: Mapped[float] = mapped_column(Numeric(12, 4))
    stop_price: Mapped[float] = mapped_column(Numeric(12, 4))
    target_price: Mapped[float] = mapped_column(Numeric(12, 4))
    qty_lots: Mapped[int] = mapped_column(Integer)
    # Per-method trailing (see TradeProposal) — nullable so a strategy that
    # doesn't set them (SyntheticStrategy) falls back to the generic 0.5/0.5
    # Phase-3 rule at dispatch time, not a schema-level default here.
    trail_activation_fraction: Mapped[float | None] = mapped_column(
        Numeric(6, 4), nullable=True
    )
    trail_lock_fraction: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    # Underlying-index structural invalidation level (opening-range boundary /
    # pullback-bar extreme / EMA9 value) — independent of stop_price/
    # target_price, which are on the option premium. See StopPlan.structure_level.
    structure_level: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    # ATR-scaled minimum-breach margin (underlying index points) and minimum
    # persistence window (seconds) a structure_level breach must hold before
    # counting as a confirmed break, rather than a single noisy tick — frozen
    # at signal time, same as structure_level itself. Null on either means
    # "no buffer / confirm immediately" (today's exact prior behavior). See
    # StopPlan's identical pair and evaluate_open_position's own docstring.
    structure_break_buffer: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    structure_break_persistence_seconds: Mapped[float | None] = mapped_column(
        Numeric(6, 2), nullable=True
    )
    # Multi-leg exit engine: the frozen per-leg exit spec this strategy
    # proposed (list of dicts — qty_fraction + per-leg stop/target/trail/
    # structure/max_loss/time_stop). `None` means "single full-qty exit"
    # (today's behaviour). Serialized from `TradeProposal.exit_legs`;
    # `TradeIntent.exit_legs` is the copy execution actually reads at
    # dispatch, same mirror-onto-both convention as the structure_break trio.
    exit_legs: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_signals_run", "strategy_run_id"),
        Index("ix_signals_session", "trading_session_id"),
    )


class TradeIntent(Base, UUIDPkMixin):
    __tablename__ = "trade_intents"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    signal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("signals.id"))
    strategy_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategy_runs.id"))
    trading_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trading_sessions.id"))
    option_contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("option_contracts.id"))

    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    side: Mapped[SignalSide] = mapped_column(String(10))
    qty_lots: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float] = mapped_column(Numeric(12, 4))
    stop_price: Mapped[float] = mapped_column(Numeric(12, 4))
    target_price: Mapped[float] = mapped_column(Numeric(12, 4))
    # See Signal's identical trio above — this is the copy execution_engine
    # actually reads at dispatch time (_open_position_from_fill).
    trail_activation_fraction: Mapped[float | None] = mapped_column(
        Numeric(6, 4), nullable=True
    )
    trail_lock_fraction: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    structure_level: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    # See Signal's identical pair above.
    structure_break_buffer: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    structure_break_persistence_seconds: Mapped[float | None] = mapped_column(
        Numeric(6, 2), nullable=True
    )
    # Multi-leg exit engine — the copy `execution_engine.paper.service
    # ._open_position_from_fill` reads to build `position_exit_legs`. `None`
    # = single full-qty exit (today's behaviour). See Signal.exit_legs.
    exit_legs: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[TradeIntentStatus] = mapped_column(
        String(20), default=TradeIntentStatus.PENDING_RISK
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_trade_intents_session", "trading_session_id"),
        Index("ix_trade_intents_run", "strategy_run_id"),
        Index("ix_trade_intents_contract", "option_contract_id"),
    )


class PendingTradeApproval(Base, UUIDPkMixin):
    __tablename__ = "pending_trade_approvals"

    trade_intent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trade_intents.id"), unique=True
    )
    strategy_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategy_runs.id"))
    status: Mapped[ApprovalStatus] = mapped_column(String(20), default=ApprovalStatus.PENDING)

    capital_required: Mapped[float] = mapped_column(Numeric(14, 2))
    breakeven_price: Mapped[float] = mapped_column(Numeric(12, 4))
    pnl_scenarios: Mapped[dict] = mapped_column(JSONB)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_pending_trade_approvals_run", "strategy_run_id"),)
