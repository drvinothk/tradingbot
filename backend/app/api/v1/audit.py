"""Audit read API — the hash-chained `audit_events` log (`audit_service.service.
record_event`) had no read path at all before this: every safety-relevant
action writes here, but the only way to inspect it was direct DB access.
Two endpoints: a filtered listing, and an on-demand tamper-evidence check
via `verify_chain` (which existed and was correct, but was previously only
ever called from tests).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.core.security.rbac import require_permission
from app.domain.audit.models import AuditEvent
from app.domain.identity.models import User
from app.modules.audit_service.service import verify_chain

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditEventOut(BaseModel):
    id: uuid.UUID
    seq: int
    ts: datetime
    actor_type: str
    actor_id: uuid.UUID | None
    event_category: str
    event_type: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    trading_session_id: uuid.UUID | None
    broker_account_id: uuid.UUID | None
    strategy_config_id: uuid.UUID | None
    payload: dict

    model_config = {"from_attributes": True}


class VerifyChainOut(BaseModel):
    intact: bool
    first_broken_event_id: uuid.UUID | None
    since_seq: int | None = None


@router.get("", response_model=list[AuditEventOut])
def list_audit_events(
    trading_session_id: uuid.UUID | None = None,
    strategy_config_id: uuid.UUID | None = None,
    limit: int = Query(default=200, le=1000),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("audit.view")),
) -> list[AuditEvent]:
    query = db.query(AuditEvent).filter(AuditEvent.workspace_id == user.workspace_id)
    if trading_session_id is not None:
        query = query.filter(AuditEvent.trading_session_id == trading_session_id)
    if strategy_config_id is not None:
        query = query.filter(AuditEvent.strategy_config_id == strategy_config_id)
    return query.order_by(AuditEvent.seq.desc()).limit(limit).all()


@router.get("/verify", response_model=VerifyChainOut)
def verify_audit_chain(
    since_seq: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("audit.view")),
) -> VerifyChainOut:
    """On-demand tamper-evidence check — `verify_chain` walks the *whole*
    chain (not workspace-scoped, since the chain itself is one sequence
    across all workspaces), recomputing every row's hash. Callable any time
    by anyone with `audit.view`, not gated behind a specific mode-recovery
    flow — that flow (kill-switch/reconciliation-lock resume) doesn't exist
    yet (Phase 6); this is the smallest safe surface buildable today.

    `since_seq` lets a caller verify from a documented checkpoint forward
    instead of from the very first event — see
    `audit_service.service.verify_chain`'s own docstring for why no default
    is baked in here (it's a fact about one database's own history, not
    about this endpoint).
    """
    del user
    intact, first_broken_event_id = verify_chain(db, since_seq=since_seq)
    return VerifyChainOut(
        intact=intact, first_broken_event_id=first_broken_event_id, since_seq=since_seq
    )
