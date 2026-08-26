"""Shared helpers for `app/api/v1/*` route modules — this package had no
common module before this file; each router built its own copy of anything
it needed.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.domain.identity.models import User
from app.domain.session.models import TradingSession


def get_session_or_404(db: Session, user: User, session_id: uuid.UUID) -> TradingSession:
    trading_session = (
        db.query(TradingSession)
        .filter(
            TradingSession.id == session_id,
            TradingSession.workspace_id == user.workspace_id,
        )
        .one_or_none()
    )
    if trading_session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trading session not found")
    return trading_session
