"""Broker-sync domain: the local-vs-broker reconciliation state, kept as its
own bounded context per the build plan's schema outline (a separate
"Broker sync" section from "Execution"). `BrokerSyncState` is the latest
known snapshot per (trading_session, option_contract) — overwritten on each
reconciliation pass, not history; `ReconciliationRun` is the append-only log
of each pass itself (event-triggered or polling), which is what the "an
injected inconsistency is correctly flagged by reconciliation" Phase 3 done-
when criterion is checked against.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, UUIDPkMixin


class ReconciliationTrigger(enum.StrEnum):
    EVENT = "event"
    POLL = "poll"


class BrokerSyncState(Base, UUIDPkMixin):
    __tablename__ = "broker_sync_states"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    trading_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trading_sessions.id"))
    option_contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("option_contracts.id"))

    local_qty: Mapped[int] = mapped_column(Integer)
    broker_qty: Mapped[int] = mapped_column(Integer)
    is_mismatched: Mapped[bool] = mapped_column(Boolean, default=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "trading_session_id", "option_contract_id", name="uq_broker_sync_state_session_contract"
        ),
    )


class ReconciliationRun(Base, UUIDPkMixin):
    __tablename__ = "reconciliation_runs"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    trading_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trading_sessions.id"))

    trigger_type: Mapped[ReconciliationTrigger] = mapped_column(String(10))
    mismatches_found: Mapped[int] = mapped_column(Integer, default=0)
    action_taken: Mapped[str] = mapped_column(String(60), default="none")
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_reconciliation_runs_session", "trading_session_id"),)
