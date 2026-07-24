"""RBAC dependency. Permission codes match the source blueprint's example
list (auth.login, strategy.view, ..., audit.view) and are checked against the
union of permissions across every role the user holds — a user with no roles
has no permissions, fully deny-by-default.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.core.security.deps import get_current_user
from app.domain.identity.models import Permission, RolePermission, User, UserRole


def get_user_permissions(db: Session, user: User) -> set[str]:
    rows = (
        db.query(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(UserRole, UserRole.role_id == RolePermission.role_id)
        .filter(UserRole.user_id == user.id)
        .all()
    )
    return {code for (code,) in rows}


def require_permission(permission_code: str):
    def _check(
        db: Session = Depends(get_db),
        user: User = Depends(get_current_user),
    ) -> User:
        if permission_code not in get_user_permissions(db, user):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Missing required permission: {permission_code}",
            )
        return user

    return _check
