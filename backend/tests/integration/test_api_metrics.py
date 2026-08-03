"""Tests for the metrics read API (`GET /metrics`) — same "no read path
existed" gap `test_api_audit.py` covers for `audit_events`, now for
`metric_series`. Calls the route function directly, same reasoning as
`test_api_audit.py` (RBAC itself is covered by `test_auth_and_rbac.py`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.api.v1.metrics import list_metrics
from app.domain.identity.models import User, Workspace
from app.modules.ops.metrics_service import record_metric


def test_list_metrics_is_workspace_scoped(db: Session, workspace: Workspace, user: User):
    other_workspace = Workspace(id=uuid.uuid4(), name=f"other-{uuid.uuid4().hex[:8]}")
    db.add(other_workspace)
    db.flush()

    record_metric(db, workspace_id=workspace.id, metric_name="disk_free_gb", value=42.0)
    record_metric(db, workspace_id=other_workspace.id, metric_name="disk_free_gb", value=1.0)
    db.flush()

    results = list_metrics(
        metric_name=None,
        trading_session_id=None,
        since=None,
        until=None,
        limit=200,
        db=db,
        user=user,
    )

    assert len(results) == 1
    assert results[0].value == 42.0


def test_list_metrics_filters_by_name_and_time_range(
    db: Session, workspace: Workspace, user: User
):
    now = datetime.now(UTC)
    record_metric(
        db, workspace_id=workspace.id, metric_name="ntp_drift_seconds", value=0.2, recorded_at=now
    )
    record_metric(
        db,
        workspace_id=workspace.id,
        metric_name="disk_free_gb",
        value=10.0,
        recorded_at=now - timedelta(hours=2),
    )
    db.flush()

    by_name = list_metrics(
        metric_name="ntp_drift_seconds",
        trading_session_id=None,
        since=None,
        until=None,
        limit=200,
        db=db,
        user=user,
    )
    assert {m.metric_name for m in by_name} == {"ntp_drift_seconds"}

    recent_only = list_metrics(
        metric_name=None,
        trading_session_id=None,
        since=now - timedelta(hours=1),
        until=None,
        limit=200,
        db=db,
        user=user,
    )
    assert {m.metric_name for m in recent_only} == {"ntp_drift_seconds"}
