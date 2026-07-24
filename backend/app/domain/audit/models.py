"""Audit domain: a hash-chained, append-only event log. `trading_session_id`
and `strategy_config_id` are plain UUID columns without FK constraints for
now — those tables don't exist until Phase 1/2; the FK constraints get added
in the migration that introduces them, the column shape doesn't change.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, UUIDPkMixin


class ActorType(enum.StrEnum):
    USER = "user"
    SYSTEM = "system"


class EventCategory(enum.StrEnum):
    AUTH = "auth"
    CREDENTIAL_CONFIG_CHANGE = "credential_config_change"
    STRATEGY_STATE_CHANGE = "strategy_state_change"
    MARKET_DATA_CONNECTIVITY = "market_data_connectivity"
    SIGNAL_GENERATION = "signal_generation"
    RISK_DECISION = "risk_decision"
    ORDER_LIFECYCLE = "order_lifecycle"
    BROKER_RECONCILIATION = "broker_reconciliation"
    MANUAL_OVERRIDE = "manual_override"
    SYSTEM_HEALTH = "system_health"
    MODE_TRANSITION = "mode_transition"


class AuditEvent(Base, UUIDPkMixin):
    __tablename__ = "audit_events"

    # Strict insertion order for hash-chain purposes — UUID PKs and even `ts`
    # (millisecond collisions) aren't reliable for "what's the previous row".
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    actor_type: Mapped[ActorType] = mapped_column(String(20))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )

    event_category: Mapped[EventCategory] = mapped_column(String(40))
    event_type: Mapped[str] = mapped_column(String(80))

    entity_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)

    trading_session_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    broker_account_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("broker_accounts.id"), nullable=True
    )
    strategy_config_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )

    payload: Mapped[dict] = mapped_column(JSONB, default=dict)

    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hash: Mapped[str] = mapped_column(String(64), unique=True)

    __table_args__ = (
        Index("ix_audit_events_workspace_ts", "workspace_id", "ts"),
        Index("ix_audit_events_actor", "actor_type", "actor_id"),
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
        Index("ix_audit_events_trading_session", "trading_session_id"),
        Index("ix_audit_events_broker_account", "broker_account_id"),
        Index("ix_audit_events_strategy_config", "strategy_config_id"),
    )
