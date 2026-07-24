from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.security.passwords import verify_password
from app.core.security.rbac import get_user_permissions
from app.core.security.sessions import issue_session, resolve_session, revoke_session
from app.domain.identity.models import Permission, Role, RolePermission, User, UserRole


def test_issue_and_resolve_session_roundtrip(db: Session, user: User):
    raw_token = issue_session(db, user)
    resolved = resolve_session(db, raw_token)
    assert resolved is not None
    assert resolved.id == user.id


def test_resolve_session_rejects_garbage_token(db: Session):
    assert resolve_session(db, "not-a-real-token") is None


def test_revoked_session_no_longer_resolves(db: Session, user: User):
    raw_token = issue_session(db, user)
    revoke_session(db, raw_token)
    assert resolve_session(db, raw_token) is None


def test_user_with_no_roles_has_no_permissions(db: Session, user: User):
    assert get_user_permissions(db, user) == set()


def test_role_permission_grants_flow_through(db: Session, user: User, workspace):
    role = Role(id=uuid.uuid4(), name="TestRole")
    permission = Permission(id=uuid.uuid4(), code="test.permission", description="")
    db.add_all([role, permission])
    db.flush()

    db.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db.add(UserRole(user_id=user.id, role_id=role.id, workspace_id=workspace.id))
    db.flush()

    assert get_user_permissions(db, user) == {"test.permission"}


def test_seeded_admin_role_has_all_permissions(db: Session):
    """Sanity-checks the seed_data mapping itself, not just the DB query —
    if PERMISSIONS/ROLES ever drift apart this catches it in tests, not
    after a real deploy discovers Admin is missing a permission."""
    from app.domain.identity.seed_data import PERMISSIONS, ROLES

    assert set(ROLES["Admin"]) == set(PERMISSIONS.keys())


def test_password_hash_never_equals_plaintext_in_db(db: Session, user: User):
    assert user.password_hash != "correct horse battery staple"
    assert verify_password("correct horse battery staple", user.password_hash)
