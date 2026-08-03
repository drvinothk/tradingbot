"""Ops metrics read API — `metric_series` (`modules/ops/metrics_service.
record_metric`) had no read path at all before this, same gap `audit.py`
closed for `audit_events`. Reuses `audit.view` rather than a new permission
code: "operational metrics" is adjacent enough to "audit view" that a
dedicated RBAC row isn't warranted for this batch.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.core.security.rbac import require_permission
from app.domain.identity.models import User
from app.domain.ops.models import MetricSeries

router = APIRouter(prefix="/metrics", tags=["metrics"])


class MetricSeriesOut(BaseModel):
    id: uuid.UUID
    trading_session_id: uuid.UUID | None
    metric_name: str
    value: float
    recorded_at: datetime
    tags: dict

    model_config = {"from_attributes": True}


@router.get("", response_model=list[MetricSeriesOut])
def list_metrics(
    metric_name: str | None = None,
    trading_session_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=200, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("audit.view")),
) -> list[MetricSeries]:
    query = db.query(MetricSeries).filter(MetricSeries.workspace_id == user.workspace_id)
    if metric_name is not None:
        query = query.filter(MetricSeries.metric_name == metric_name)
    if trading_session_id is not None:
        query = query.filter(MetricSeries.trading_session_id == trading_session_id)
    if since is not None:
        query = query.filter(MetricSeries.recorded_at >= since)
    if until is not None:
        query = query.filter(MetricSeries.recorded_at <= until)
    return query.order_by(MetricSeries.recorded_at.desc()).limit(limit).all()
