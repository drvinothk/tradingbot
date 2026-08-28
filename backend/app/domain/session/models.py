"""Trading session & safe-operating-mode domain. A `TradingSession` is a
single trading day for one broker account; `mode` is governed exclusively by
the state machine in app.core.modes — nothing should ever write `mode`
directly except that module's guarded transition function.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, time

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Time
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, TimestampMixin, UUIDPkMixin


class SafeMode(enum.StrEnum):
    # 2026-08-28: the per-strategy graduation tier `PAPER_PLUS_GUARDED_LIVE`
    # was retired (migration 0028) — it only ever paired with
    # `StrategyConfig.status == LIVE`, a field that never had a setter. The
    # master switch now walks `paper_only <-> live_enabled` directly; a
    # single strategy is held on paper inside a live session via
    # `StrategyRuntimeMode.FORCE_PAPER`, not a session sub-mode.
    PAPER_ONLY = "paper_only"
    LIVE_ENABLED = "live_enabled"
    DEGRADED_MODE = "degraded_mode"
    RECONCILIATION_LOCK = "reconciliation_lock"
    KILL_SWITCH = "kill_switch"


class TradingSessionStatus(enum.StrEnum):
    ACTIVE = "active"
    ENDED = "ended"


class FundingMode(enum.StrEnum):
    CASH = "cash"
    MTF = "mtf"


class EntriesPausedReason(enum.StrEnum):
    DAILY_TARGET_REACHED = "daily_target_reached"
    ADMIN_PAUSE = "admin_pause"


class TransitionTriggerType(enum.StrEnum):
    MANUAL = "manual"
    SYSTEM = "system"
    RISK = "risk"
    RECONCILIATION = "reconciliation"


class TradingSession(Base, UUIDPkMixin, TimestampMixin):
    __tablename__ = "trading_sessions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    broker_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("broker_accounts.id"))
    started_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))

    mode: Mapped[SafeMode] = mapped_column(String(30), default=SafeMode.PAPER_ONLY)
    status: Mapped[TradingSessionStatus] = mapped_column(
        String(20), default=TradingSessionStatus.ACTIVE
    )
    prior_mode: Mapped[SafeMode | None] = mapped_column(
        String(30),
        nullable=True,
        doc="Remembered for degraded_mode/reconciliation_lock recovery",
    )
    # 2026-08-25: consecutive clean `run_full_reconciliation` checks while
    # in RECONCILIATION_LOCK, driven only by ReconciliationLockRecoveryScheduler
    # (never by any other reconciliation call site) -- reaching the
    # scheduler's own threshold triggers unattended auto-recovery, including
    # back to a live prior_mode (a deliberate, scoped exception to Rule 4 --
    # see app.core.modes.state_machine.recover_from_reconciliation_lock's own
    # docstring). Reset to 0 on every fresh entry into RECONCILIATION_LOCK.
    reconciliation_lock_clean_streak: Mapped[int] = mapped_column(Integer, default=0)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cutoff_time: Mapped[time] = mapped_column(Time, default=time(15, 9))

    # Daily plan — pre-filled from risk_limit_configs, editable at session
    # start. Field names deliberately match risk_limit_configs 1:1.
    budget_amount: Mapped[float] = mapped_column(Numeric(14, 2))
    daily_target_profit: Mapped[float] = mapped_column(Numeric(14, 2))
    daily_loss_cap: Mapped[float] = mapped_column(Numeric(14, 2))
    funding_mode: Mapped[FundingMode] = mapped_column(String(10), default=FundingMode.CASH)

    # Not degraded_mode — reaching a profit target is a goal, not a fault.
    entries_paused_reason: Mapped[EntriesPausedReason | None] = mapped_column(
        String(30), nullable=True
    )

    # Running totals Risk Service checks daily_loss_cap / daily_target_profit
    # / consecutive_loss_pause_threshold against. Updated by
    # risk_engine.service.record_trade_outcome_effects whenever
    # execution_engine.paper.service.close_position closes a real Position.
    cumulative_realized_pnl: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)


class SessionModeTransition(Base, UUIDPkMixin):
    __tablename__ = "session_mode_transitions"

    trading_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trading_sessions.id"))
    from_mode: Mapped[SafeMode | None] = mapped_column(String(30), nullable=True)
    to_mode: Mapped[SafeMode] = mapped_column(String(30))
    trigger_type: Mapped[TransitionTriggerType] = mapped_column(String(20))
    triggered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    reason: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
