"""Login session issuance/verification. The raw token is returned to the
client exactly once (at login) and never stored — only its SHA-256 hash is
persisted, so a DB read alone can never leak a usable session token.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.domain.identity.models import LoginSession, User


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue_session(
    db: Session, user: User, ip_address: str | None = None, user_agent: str | None = None
) -> str:
    """Creates a LoginSession row and returns the raw token — the only time
    it's ever available in plaintext."""
    raw_token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    ttl = get_settings().app.session_ttl_minutes

    login_session = LoginSession(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=_hash_token(raw_token),
        issued_at=now,
        expires_at=now + timedelta(minutes=ttl),
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.add(login_session)
    db.flush()
    return raw_token


def resolve_session(db: Session, raw_token: str) -> User | None:
    """Returns the User for a valid, non-expired, non-revoked session token,
    or None. Callers should treat None as "not authenticated" uniformly,
    regardless of whether the token was malformed, expired, or revoked —
    that distinction is not information the caller needs."""
    token_hash = _hash_token(raw_token)
    login_session = (
        db.query(LoginSession).filter(LoginSession.token_hash == token_hash).one_or_none()
    )
    if login_session is None:
        return None
    now = datetime.now(UTC)
    if login_session.revoked_at is not None or login_session.expires_at < now:
        return None
    return db.query(User).filter(User.id == login_session.user_id).one_or_none()


def revoke_session(db: Session, raw_token: str) -> None:
    token_hash = _hash_token(raw_token)
    login_session = (
        db.query(LoginSession).filter(LoginSession.token_hash == token_hash).one_or_none()
    )
    if login_session is not None and login_session.revoked_at is None:
        login_session.revoked_at = datetime.now(UTC)
        db.flush()
