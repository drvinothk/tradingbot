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
from app.domain import (  # noqa: F401 - registers models on Base
    audit,
    broker,
    execution,
    identity,
    market,
    ops,
    risk,
    session,
    strategy,
)
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


@pytest.fixture(autouse=True)
def _reset_broker_singleton() -> Generator[None, None, None]:
    """`app.modules.broker_adapter.composition.get_broker`/
    `get_execution_broker` lazily construct one process-wide
    `MockBrokerAdapter` each (the same instance, when nothing real is
    connected) — reset both to unset before and after every test so each
    test gets a fresh adapter (no leftover orders/positions from a previous
    test) rather than sharing state across the whole suite. A test that
    wants a specific broker instance (a seeded one, or a fake) calls
    `composition.set_broker(...)` itself; this fixture only guarantees a
    clean slate either way.

    `market_data.provider_composition` gets the identical treatment for the
    identical reason — it's a second, independent module-level singleton
    (`get_market_data_provider()`), not reset by `composition.reset_for_tests()`
    above, that would otherwise leak a provider/`ScripMasterService` instance
    across tests the same way an unreset broker singleton would.
    """
    from app.modules.broker_adapter import composition
    from app.modules.market_data import provider_composition

    composition.reset_for_tests()
    provider_composition.reset_for_tests()
    yield
    composition.reset_for_tests()
    provider_composition.reset_for_tests()


@pytest.fixture(autouse=True)
def _fake_market_data_provider_preference_lookup(monkeypatch) -> None:
    """Ops-Hardening Phase 4: `provider_composition._seed_manual_override`
    (called from `get_market_data_provider()` whenever failover is enabled)
    reads `MarketDataProviderPreference` via the real `session_scope` --
    bound to the actual dev-configured engine, not this suite's own
    isolated test database (see this file's own module docstring for why
    those are deliberately never the same connection). Left unpatched,
    every test that constructs a failover-wrapped provider would make a
    real read against the live dev DB. Patched to a fake that returns "no
    preference row" without touching any real connection at all -- same
    "never let a composition-root helper default to the production DB
    inside a test" discipline as every other background-write path fixed
    this same way elsewhere in this codebase.
    """
    from contextlib import contextmanager

    from app.modules.market_data import provider_composition

    class _NoRowsQuery:
        def first(self):
            return None

    class _FakeSession:
        def query(self, *args, **kwargs):
            return _NoRowsQuery()

    @contextmanager
    def _fake_session_scope():
        yield _FakeSession()

    monkeypatch.setattr(provider_composition, "session_scope", _fake_session_scope)


@pytest.fixture(autouse=True)
def _force_no_real_money_dispatch(monkeypatch) -> None:
    """Ops-Hardening Phase 5. Forces `Settings.app.allow_real_money_dispatch`
    to `False` for every test, unconditionally -- regardless of whatever a
    local, misconfigured `.env` might set. `get_settings()` is `@lru_cache`'d,
    so this patches the attribute directly on the already-cached instance
    (same pattern already used for `settings.telegram.bot_token`/`chat_id`
    in the Phase 2 alerting tests), not the environment variable, which
    would be too late for an instance that's already constructed. No test
    anywhere in this suite should ever be able to accidentally clear the
    real-money gate.
    """
    from app.config.settings import get_settings

    monkeypatch.setattr(get_settings().app, "allow_real_money_dispatch", False)


@pytest.fixture(autouse=True)
def _force_no_telegram_dispatch(monkeypatch) -> None:
    """Forces `Settings.telegram.bot_token`/`chat_id` empty for every test,
    unconditionally -- same rationale and pattern as
    `_force_no_real_money_dispatch` above. Without this, any test that
    exercises `alerting.manager.send_alert` (e.g. `test_runner_watchdog.py`,
    `test_health_check_scheduler.py`) sends a real Telegram message to
    whatever `config/credentials/telegram.env` is configured locally --
    found 2026-08-26 after a batch of local test runs leaked real CRITICAL
    alerts (stalled-feed, disk-failure) to the production chat. `_send_
    telegram` already no-ops cleanly on empty credentials, so this is pure
    test isolation with no production behavior change.
    """
    from pydantic import SecretStr

    from app.config.settings import get_settings

    monkeypatch.setattr(get_settings().telegram, "bot_token", SecretStr(""))
    monkeypatch.setattr(get_settings().telegram, "chat_id", "")


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
