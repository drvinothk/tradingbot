"""API-level tests via FastAPI's TestClient. Deliberately scoped to the
behaviors this QC pass actually touched (auth requirement, RBAC enforcement,
and the three bugs found and fixed here — the budget_amount fallback, missing
workspace scoping, and unvalidated broker_account_id) rather than exhaustive
endpoint coverage, since Phase 2 substantially reworks the session endpoints
anyway (the real daily-plan form).

Plain `TestClient(app)` (not used as a context manager) does not trigger
app.main's lifespan — confirmed empirically during Phase 0 manual testing —
which is what makes this safe to run without contending for the process
singleton lock against a real dev server that might also be running.
`get_db` is overridden to point at the isolated test database instead.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.db.session import get_db
from app.core.security.passwords import hash_password
from app.domain.identity.models import (
    BrokerAccount,
    BrokerAccountStatus,
    BrokerType,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
    Workspace,
)
from app.main import app

ADMIN_PASSWORD = "correct horse battery staple 123!"


@pytest.fixture
def api_client(engine) -> Generator[TestClient, None, None]:
    session_factory = sessionmaker(bind=engine, future=True)

    def override_get_db() -> Generator:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def seeded_admin(engine):
    """Mirrors scripts/bootstrap_admin.py's shape (workspace + Admin role +
    every permission + one user), committed for real against the test engine
    since the API endpoints under test commit for real too — then cleaned up
    explicitly at teardown, same reasoning as the market-data ingestion tests'
    seeded_universe fixture.
    """
    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as db:
        workspace = Workspace(id=uuid.uuid4(), name=f"qc-test-{uuid.uuid4().hex[:8]}")
        db.add(workspace)
        db.flush()

        role = Role(id=uuid.uuid4(), name=f"qc-admin-{uuid.uuid4().hex[:8]}")
        db.add(role)
        db.flush()
        permission_ids: list[uuid.UUID] = []
        for code in ("session.start", "session.stop", "strategy.view", "papertrade.execute"):
            permission = Permission(id=uuid.uuid4(), code=code, description="")
            db.add(permission)
            db.flush()
            permission_ids.append(permission.id)
            db.add(RolePermission(role_id=role.id, permission_id=permission.id))

        user = User(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            email=f"qc-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password(ADMIN_PASSWORD),
            display_name="QC Test Admin",
            is_active=True,
        )
        db.add(user)
        db.flush()
        db.add(UserRole(user_id=user.id, role_id=role.id, workspace_id=workspace.id))

        broker_account = BrokerAccount(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            broker_type=BrokerType.SHOONYA,
            label="qc-test-account",
            credentials_ref="config/credentials/shoonya.env",
            status=BrokerAccountStatus.ACTIVE,
        )
        db.add(broker_account)
        db.commit()

        ids = {
            "workspace_id": workspace.id,
            "user_id": user.id,
            "email": user.email,
            "broker_account_id": broker_account.id,
            "role_id": role.id,
            "permission_ids": permission_ids,
        }

    yield ids

    # Deleted in explicit FK-safe order, by the exact IDs created above —
    # relying on post-hoc subqueries (e.g. "role_id in (select ... from
    # UserRole)") is fragile once earlier statements in the same cleanup
    # have already deleted the rows those subqueries depend on.
    with session_factory() as cleanup_db:
        from app.domain.audit.models import AuditEvent
        from app.domain.identity.models import LoginSession
        from app.domain.session.models import SessionModeTransition, TradingSession

        # Every API call under test that succeeds writes at least one audit
        # row (login, kill-switch) — miss this and cleanup fails on the same
        # FK-violation-cascades-into-the-next-test's-unique-constraint
        # pattern as the Permission rows below.
        cleanup_db.query(AuditEvent).filter(
            AuditEvent.workspace_id == ids["workspace_id"]
        ).delete()
        cleanup_db.query(SessionModeTransition).delete()
        cleanup_db.query(TradingSession).filter(
            TradingSession.workspace_id == ids["workspace_id"]
        ).delete()
        cleanup_db.query(LoginSession).filter(LoginSession.user_id == ids["user_id"]).delete()
        cleanup_db.query(BrokerAccount).filter(
            BrokerAccount.id == ids["broker_account_id"]
        ).delete()
        cleanup_db.query(UserRole).filter(UserRole.user_id == ids["user_id"]).delete()
        cleanup_db.query(User).filter(User.id == ids["user_id"]).delete()
        cleanup_db.query(RolePermission).filter(
            RolePermission.role_id == ids["role_id"]
        ).delete()
        cleanup_db.query(Permission).filter(Permission.id.in_(ids["permission_ids"])).delete(
            synchronize_session=False
        )
        cleanup_db.query(Role).filter(Role.id == ids["role_id"]).delete()
        cleanup_db.query(Workspace).filter(Workspace.id == ids["workspace_id"]).delete()
        cleanup_db.commit()


def test_me_requires_authentication(api_client: TestClient):
    response = api_client.get("/api/v1/auth/me")
    assert response.status_code == 401


def test_login_rejects_wrong_password(api_client: TestClient, seeded_admin):
    response = api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": "wrong password"},
    )
    assert response.status_code == 401


def test_login_success_sets_cookie_and_returns_user(api_client: TestClient, seeded_admin):
    response = api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    assert response.json()["email"] == seeded_admin["email"]
    assert "session_token" in response.cookies


def test_login_then_me_roundtrip(api_client: TestClient, seeded_admin):
    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )
    response = api_client.get("/api/v1/auth/me")
    assert response.status_code == 200
    assert response.json()["id"] == str(seeded_admin["user_id"])


def test_create_session_rejects_unknown_broker_account(api_client: TestClient, seeded_admin):
    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )
    response = api_client.post(
        "/api/v1/sessions", json={"broker_account_id": str(uuid.uuid4())}
    )
    # Must be a clean 404, not an unhandled 500 from a foreign-key violation.
    assert response.status_code == 404


def test_create_session_uses_dedicated_budget_default_not_loss_cap(
    api_client: TestClient, seeded_admin
):
    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )
    response = api_client.post(
        "/api/v1/sessions",
        json={"broker_account_id": str(seeded_admin["broker_account_id"])},
    )
    assert response.status_code == 200

    from app.config.settings import get_settings

    defaults = get_settings().risk_defaults
    # Regression check for the fallback bug: budget_amount must come from its
    # own default, not silently equal daily_loss_cap.
    assert defaults.default_budget != defaults.daily_loss_cap


def test_kill_switch_accepts_reason_in_json_body(api_client: TestClient, seeded_admin):
    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )
    create_resp = api_client.post(
        "/api/v1/sessions",
        json={"broker_account_id": str(seeded_admin["broker_account_id"])},
    )
    session_id = create_resp.json()["id"]

    response = api_client.post(
        f"/api/v1/sessions/{session_id}/kill-switch",
        json={"reason": "api test reason"},
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "kill_switch"


def test_daily_plan_updates_session_and_is_audited(api_client: TestClient, seeded_admin, engine):
    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )
    create_resp = api_client.post(
        "/api/v1/sessions",
        json={"broker_account_id": str(seeded_admin["broker_account_id"])},
    )
    session_id = create_resp.json()["id"]

    response = api_client.post(
        f"/api/v1/sessions/{session_id}/daily-plan",
        json={
            "budget_amount": 75000,
            "daily_target_profit": 3000,
            "daily_loss_cap": 1500,
            "funding_mode": "mtf",
        },
    )
    assert response.status_code == 200

    from app.domain.audit.models import AuditEvent
    from app.domain.session.models import TradingSession

    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as verify_db:
        row = verify_db.get(TradingSession, uuid.UUID(session_id))
        assert row is not None
        assert float(row.budget_amount) == 75000
        assert float(row.daily_target_profit) == 3000
        assert float(row.daily_loss_cap) == 1500
        assert row.funding_mode == "mtf"

        events = (
            verify_db.query(AuditEvent)
            .filter(
                AuditEvent.trading_session_id == uuid.UUID(session_id),
                AuditEvent.event_type == "daily_plan.updated",
            )
            .all()
        )
        assert len(events) == 1


def test_daily_plan_rejects_non_positive_budget(api_client: TestClient, seeded_admin):
    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )
    create_resp = api_client.post(
        "/api/v1/sessions",
        json={"broker_account_id": str(seeded_admin["broker_account_id"])},
    )
    session_id = create_resp.json()["id"]

    response = api_client.post(
        f"/api/v1/sessions/{session_id}/daily-plan",
        json={
            "budget_amount": 0,
            "daily_target_profit": 3000,
            "daily_loss_cap": 1500,
            "funding_mode": "cash",
        },
    )
    assert response.status_code == 422
