"""Strategy runtime domain. `StrategyConfig` is the graduation record (its
`status` field is what Phase 6 checks before a strategy can receive a live
TradeIntent); `Signal`/`TradeIntent` are what every strategy — synthetic now,
real from Phase 4 — is allowed to emit, per the shared `Strategy` interface
in `app.modules.strategy_engine.interface` (no Order/Position access at any
layer beneath it). A TradeIntent's own status lifecycle ends at `DISPATCHED`
("handed to Execution Service") — what happens after that lives in
`app.domain.execution`'s Order/Position/TradeOutcome chain, not here.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
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


class StrategyStatus(enum.StrEnum):
    RESEARCH = "research"
    PAPER = "paper"
    PAPER_PLUS_GUARDED_LIVE = "paper_plus_guarded_live"
    LIVE = "live"


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
    # enum.StrEnum column like the rest of this file's status fields because
    # new strategy types are added by later phases (Phase 7/8) without a
    # migration touching every existing row's constraint.
    strategy_type: Mapped[str] = mapped_column(String(40), default="synthetic")
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[StrategyStatus] = mapped_column(String(30), default=StrategyStatus.RESEARCH)

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
