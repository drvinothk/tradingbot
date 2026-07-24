"""FastAPI auth dependency. Session token travels as an HttpOnly, Secure,
SameSite=Lax cookie — never in localStorage/JS-readable storage, per the
source blueprint's "avoid unsafe browser token storage patterns" requirement.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.core.security.sessions import resolve_session
from app.domain.identity.models import User

SESSION_COOKIE_NAME = "session_token"


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    user = resolve_session(db, raw_token)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    return user
