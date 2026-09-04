"""System alerts read API — `system_alerts` (written from Risk Service,
`PositionManager`, `HealthCheckScheduler`, and now `execution_engine.paper.
service`'s exit-order-unfilled guard) had no read path at all before this,
same gap `audit.py`/`metrics.py` closed for their own tables. Feeds the
frontend Recovery panel.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db.base import utcnow as _utcnow
from app.core.db.session import get_db
from app.core.security.rbac import require_permission
from app.domain.audit.models import ActorType, EventCategory
from app.domain.identity.models import User
from app.domain.ops.models import SystemAlert
from app.modules.audit_service.service import record_event

router = APIRouter(prefix="/system-alerts", tags=["system-alerts"])


class SystemAlertOut(BaseModel):
    id: uuid.UUID
    trading_session_id: uuid.UUID | None
    severity: str
    category: str
    message: str
    payload: dict
    created_at: datetime
    resolved_at: datetime | None
    is_resolved: bool
    occurrence_count: int
    last_seen_at: datetime
    # 2026-09-04: 'paper' | 'live' | None. See SystemAlert.mode's own
    # docstring -- Control Room's "Attention Required" card filters on this.
    mode: str | None

    model_config = {"from_attributes": True}


@router.get("", response_model=list[SystemAlertOut])
def list_system_alerts(
    trading_session_id: uuid.UUID | None = None,
    is_resolved: bool | None = None,
    limit: int = Query(default=100, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("audit.view")),
) -> list[SystemAlert]:
    query = db.query(SystemAlert).filter(SystemAlert.workspace_id == user.workspace_id)
    if trading_session_id is not None:
        query = query.filter(SystemAlert.trading_session_id == trading_session_id)
    if is_resolved is not None:
        query = query.filter(SystemAlert.is_resolved == is_resolved)
    return query.order_by(SystemAlert.created_at.desc()).limit(limit).all()


@router.post("/{alert_id}/resolve", response_model=SystemAlertOut)
def resolve_system_alert(
    alert_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("risk.override")),
) -> SystemAlert:
    """2026-09-04: the general manual-resolve safety valve — before this,
    `is_resolved` was only ever written by two places: `alerting.manager.
    send_alert`'s own row-collapse (never to `True`) and `scheduler.
    alert_housekeeping.AlertHousekeepingScheduler`'s hourly sweep, which
    only auto-closes a row once it hasn't recurred for `system_alert_
    collapse_window_hours` (24h default). A real issue fixed in minutes
    (e.g. by hand via `manual-reconcile`) kept re-pushing to Telegram every
    15 minutes for the rest of that window with no way to acknowledge it —
    the incident this endpoint exists to close. `reconciliation.service.
    run_reconciliation`'s own auto-resolve-on-next-clean-pass covers the
    `reconciliation_mismatch` category specifically and fires automatically;
    this endpoint is the general fallback for every other category (`exit_
    order_unfilled`, `protective_stop_cancel_unresolved`, `strategy_run_
    stalled`, etc.) and for a human override of any alert regardless of
    category.

    Gated on `risk.override` — same bar `manual_reconcile_position`/
    `recover_from_reconciliation_lock` already use for "a human is directly
    correcting system state." Idempotent: resolving an already-resolved
    alert is a harmless no-op (200, unchanged `resolved_at`), not a 409 —
    this is an acknowledgement action, not a state transition with a race
    to guard against.
    """
    alert = (
        db.query(SystemAlert)
        .filter(SystemAlert.id == alert_id, SystemAlert.workspace_id == user.workspace_id)
        .one_or_none()
    )
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "System alert not found")

    if not alert.is_resolved:
        alert.is_resolved = True
        alert.resolved_at = _utcnow()
        db.add(alert)
        record_event(
            db,
            workspace_id=user.workspace_id,
            actor_type=ActorType.USER,
            actor_id=user.id,
            event_category=EventCategory.MANUAL_OVERRIDE,
            event_type="system_alert.manual_resolve",
            entity_type="system_alert",
            entity_id=alert.id,
            trading_session_id=alert.trading_session_id,
            payload={"category": alert.category, "severity": alert.severity},
        )
        db.commit()
        db.refresh(alert)

    return alert
