"""System alerts read API — `system_alerts` (written from Risk Service,
`PositionManager`, `HealthCheckScheduler`, and now `execution_engine.paper.
service`'s exit-order-unfilled guard) had no read path at all before this,
same gap `audit.py`/`metrics.py` closed for their own tables. Feeds the
frontend Recovery panel.
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
from app.domain.ops.models import SystemAlert

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
