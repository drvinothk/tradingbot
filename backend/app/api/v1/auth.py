"""Login/logout. Session token travels as an HttpOnly cookie (see
core.security.deps) — the frontend never reads or stores it directly.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.core.security.deps import SESSION_COOKIE_NAME, get_current_user
from app.core.security.passwords import verify_password
from app.core.security.sessions import issue_session, revoke_session
from app.domain.audit.models import ActorType, EventCategory
from app.domain.identity.models import User
from app.modules.audit_service.service import record_event

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str

    model_config = {"from_attributes": True}


@router.post("/login", response_model=UserOut)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> User:
    user = db.query(User).filter(User.email == body.email.lower()).one_or_none()

    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        # Deliberately identical error for "no such user" and "wrong password" —
        # distinguishing them leaks which emails are registered.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    raw_token = issue_session(
        db, user, ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        event_category=EventCategory.AUTH,
        event_type="auth.login_success",
        entity_type="user",
        entity_id=user.id,
        payload={"email": user.email},
    )
    db.commit()

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return user


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token:
        revoke_session(db, raw_token)

    record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        event_category=EventCategory.AUTH,
        event_type="auth.logout",
        entity_type="user",
        entity_id=user.id,
    )
    db.commit()

    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> User:
    return user
