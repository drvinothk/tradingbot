"""Ops domain. `SystemAlert` is pulled forward from the full Ops schema
(system_alerts / metric_series / scheduler_job_runs) because Phase 2's Risk
Service needs somewhere to make a limit breach visible beyond the audit log.
`MetricSeries` follows in the Addendum hardening batch, the first real writer
being the periodic health-check loop (`scheduler/health_check.py`) — see
`modules/ops/metrics_service.py`'s `record_metric`. `scheduler_job_runs`
stays out of scope until whatever phase actually needs it.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, UUIDPkMixin


class AlertSeverity(enum.StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class SystemAlert(Base, UUIDPkMixin):
    __tablename__ = "system_alerts"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    trading_session_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    severity: Mapped[AlertSeverity] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(String(60))
    message: Mapped[str] = mapped_column(String(1000))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("ix_system_alerts_workspace_created", "workspace_id", "created_at"),
        Index("ix_system_alerts_trading_session", "trading_session_id"),
    )


class MetricSeries(Base, UUIDPkMixin):
    __tablename__ = "metric_series"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    trading_session_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    metric_name: Mapped[str] = mapped_column(String(100))
    value: Mapped[float] = mapped_column(Float)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tags: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        Index(
            "ix_metric_series_workspace_name_recorded",
            "workspace_id",
            "metric_name",
            "recorded_at",
        ),
        Index("ix_metric_series_trading_session", "trading_session_id"),
    )
