"""Shared fixtures. DB-backed fixtures require a live Postgres (see
DB_* env vars in .env / CI) — they create the schema fresh via
Base.metadata.create_all rather than running Alembic, so tests aren't
coupled to migration history, and wrap each test in a rolled-back
transaction for isolation.

Deliberately never runs against DB_NAME directly: this fixture set does a
session-scoped Base.metadata.drop_all() at teardown, which would silently
wipe a real dev/prod database if pointed at it. Always targets a dedicated
"<DB_NAME>_test" database instead, created on demand if it doesn't exist —
this is the actual fix for a real incident where the first run of this suite
against DB_NAME dropped the freshly-migrated dev schema.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import get_settings
from app.core.db.base import Base
from app.domain import audit, identity, session  # noqa: F401 - registers models on Base
from app.domain.identity.models import User, Workspace


def _test_database_url() -> str:
    db_settings = get_settings().db
    test_db_name = f"{db_settings.name}_test"
    base_url = db_settings.sqlalchemy_url.rsplit("/", 1)[0]
    return f"{base_url}/{test_db_name}"


def _ensure_test_database_exists() -> None:
    db_settings = get_settings().db
    test_db_name = f"{db_settings.name}_test"
    maintenance_url = db_settings.sqlalchemy_url.rsplit("/", 1)[0] + "/postgres"

    maintenance_engine = create_engine(maintenance_url, future=True, isolation_level="AUTOCOMMIT")
    try:
        with maintenance_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": test_db_name},
            ).first()
            if exists is None:
                conn.execute(text(f'CREATE DATABASE "{test_db_name}"'))
    finally:
        maintenance_engine.dispose()


@pytest.fixture(scope="session")
def engine():
    _ensure_test_database_exists()
    eng = create_engine(_test_database_url(), future=True)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def db(engine) -> Generator[Session, None, None]:
    connection = engine.connect()
    transaction = connection.begin()
    session_factory = sessionmaker(bind=connection, future=True)
    db_session = session_factory()

    yield db_session

    db_session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def workspace(db: Session) -> Workspace:
    ws = Workspace(id=uuid.uuid4(), name=f"test-{uuid.uuid4().hex[:8]}")
    db.add(ws)
    db.flush()
    return ws


@pytest.fixture
def user(db: Session, workspace: Workspace) -> User:
    from app.core.security.passwords import hash_password

    u = User(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("correct horse battery staple"),
        display_name="Test User",
        is_active=True,
    )
    db.add(u)
    db.flush()
    return u


def utcnow() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def authorized_user(db: Session, workspace: Workspace) -> User:
    """A user holding every permission the state-machine tests exercise
    (session.stop, livetrade.execute, risk.override). Schema here comes from
    Base.metadata.create_all, not the seed migration (0002), so there is no
    pre-seeded Admin role to reference — build the Role/Permission chain
    directly, same pattern as test_auth_and_rbac.py's grant-flow-through test.
    """
    from app.core.security.passwords import hash_password
    from app.domain.identity.models import Permission, Role, RolePermission, UserRole

    u = User(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("correct horse battery staple"),
        display_name="Authorized Test User",
        is_active=True,
    )
    db.add(u)
    db.flush()

    role = Role(id=uuid.uuid4(), name=f"test-role-{uuid.uuid4().hex[:8]}")
    db.add(role)
    db.flush()

    for code in ("session.stop", "livetrade.execute", "risk.override"):
        permission = Permission(id=uuid.uuid4(), code=code, description="")
        db.add(permission)
        db.flush()
        db.add(RolePermission(role_id=role.id, permission_id=permission.id))

    db.add(UserRole(user_id=u.id, role_id=role.id, workspace_id=workspace.id))
    db.flush()
    return u
