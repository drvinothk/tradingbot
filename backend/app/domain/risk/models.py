"""Risk domain. `RiskLimitConfig` is versioned rather than updated in place —
editing limits creates a new row and deactivates the previous one — so a
`RiskDecision` made under yesterday's limits stays explainable even after an
Admin tightens them today. `RiskDecision` carries the full pre-trade
analytics snapshot (capital_required/breakeven_price/pnl_scenarios) computed
once at evaluation time, per the build plan: this is what the UI eventually
shows the user must exactly match what Risk actually evaluated, not a
separately-recomputed estimate.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, TimestampMixin, UUIDPkMixin


class RiskDecisionOutcome(enum.StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class RiskLimitConfig(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "risk_limit_configs"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    version: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    max_concurrent_positions: Mapped[int] = mapped_column(Integer)
    max_trades_per_day: Mapped[int] = mapped_column(Integer)
    consecutive_loss_pause_threshold: Mapped[int] = mapped_column(Integer)
    daily_loss_cap: Mapped[float] = mapped_column(Numeric(14, 2))
    daily_target_profit: Mapped[float] = mapped_column(Numeric(14, 2))
    per_trade_lot_cap: Mapped[int] = mapped_column(Integer)
    cross_strategy_guard_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        Index("ix_risk_limit_configs_workspace_active", "workspace_id", "is_active"),
    )


class RiskDecision(Base, UUIDPkMixin):
    __tablename__ = "risk_decisions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    trade_intent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trade_intents.id"))
    risk_limit_config_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("risk_limit_configs.id"))

    decision: Mapped[RiskDecisionOutcome] = mapped_column(String(10))
    reasons: Mapped[list] = mapped_column(JSONB, default=list)
    checked_margin: Mapped[bool] = mapped_column(Boolean)
    funding_mode: Mapped[str] = mapped_column(String(10))

    capital_required: Mapped[float] = mapped_column(Numeric(14, 2))
    breakeven_price: Mapped[float] = mapped_column(Numeric(12, 4))
    pnl_scenarios: Mapped[dict] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_risk_decisions_trade_intent", "trade_intent_id"),
        Index("ix_risk_decisions_workspace_created", "workspace_id", "created_at"),
    )
