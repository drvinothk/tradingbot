"""Ops Metrics Service: the only writer for `metric_series`. Deliberately a
single-row insert with no aggregation/rollup logic — same minimalism as
`audit_service.record_event`, which this mirrors but without the hash-chain
(metrics are an operational view, not a safety-audit trail; `audit_events`
stays the source of truth for "did it happen").
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.domain.ops.models import MetricSeries


def record_metric(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    metric_name: str,
    value: float,
    trading_session_id: uuid.UUID | None = None,
    tags: dict | None = None,
    recorded_at: datetime | None = None,
) -> MetricSeries:
    metric = MetricSeries(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        trading_session_id=trading_session_id,
        metric_name=metric_name,
        value=value,
        recorded_at=recorded_at or datetime.now(UTC),
        tags=tags or {},
    )
    db.add(metric)
    db.flush()
    return metric
