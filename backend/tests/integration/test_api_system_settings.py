"""Ops-Hardening Phase 7: GET/PATCH /api/v1/system-settings/instrument-firewall
-- API-level behavior (auth, validation, persistence, workspace scoping).
"""

from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.db.session import get_db
from app.core.security.passwords import hash_password
from app.domain.identity.models import Permission, Role, RolePermission, User, UserRole, Workspace
from app.domain.ops.models import InstrumentFirewallConfig
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
    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as db:
        workspace = Workspace(id=uuid.uuid4(), name=f"firewall-test-{uuid.uuid4().hex[:8]}")
        db.add(workspace)
        db.flush()

        role = Role(id=uuid.uuid4(), name=f"firewall-admin-{uuid.uuid4().hex[:8]}")
        db.add(role)
        db.flush()
        permission_ids: list[uuid.UUID] = []
        for code in ("risk.override",):
            permission = Permission(id=uuid.uuid4(), code=code, description="")
            db.add(permission)
            db.flush()
            permission_ids.append(permission.id)
            db.add(RolePermission(role_id=role.id, permission_id=permission.id))

        user = User(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            email=f"firewall-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password(ADMIN_PASSWORD),
            display_name="Firewall Test Admin",
            is_active=True,
        )
        db.add(user)
        db.flush()
        db.add(UserRole(user_id=user.id, role_id=role.id, workspace_id=workspace.id))
        db.commit()

        ids = {
            "workspace_id": workspace.id,
            "user_id": user.id,
            "email": user.email,
            "role_id": role.id,
            "permission_ids": permission_ids,
        }

    yield ids

    with session_factory() as cleanup_db:
        from app.domain.audit.models import AuditEvent
        from app.domain.identity.models import LoginSession

        cleanup_db.query(AuditEvent).filter(
            AuditEvent.workspace_id == ids["workspace_id"]
        ).delete()
        cleanup_db.query(InstrumentFirewallConfig).filter(
            InstrumentFirewallConfig.workspace_id == ids["workspace_id"]
        ).delete()
        cleanup_db.query(LoginSession).filter(LoginSession.user_id == ids["user_id"]).delete()
        cleanup_db.query(UserRole).filter(UserRole.user_id == ids["user_id"]).delete()
        cleanup_db.query(User).filter(User.id == ids["user_id"]).delete()
        cleanup_db.query(RolePermission).filter(RolePermission.role_id == ids["role_id"]).delete()
        cleanup_db.query(Permission).filter(Permission.id.in_(ids["permission_ids"])).delete(
            synchronize_session=False
        )
        cleanup_db.query(Role).filter(Role.id == ids["role_id"]).delete()
        cleanup_db.query(Workspace).filter(Workspace.id == ids["workspace_id"]).delete()
        cleanup_db.commit()


def _login(api_client: TestClient, seeded_admin) -> None:
    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )


def test_get_requires_login(api_client: TestClient):
    response = api_client.get("/api/v1/system-settings/instrument-firewall")
    assert response.status_code == 401


def test_get_with_no_row_defaults_to_nifty_only(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)

    response = api_client.get("/api/v1/system-settings/instrument-firewall")

    assert response.status_code == 200
    body = response.json()
    assert body["active_live_instruments"] == ["NIFTY"]
    assert set(body["recognized_instruments"]) == {"NIFTY", "BANKNIFTY"}


def test_patch_sets_the_firewall(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)

    response = api_client.patch(
        "/api/v1/system-settings/instrument-firewall",
        json={"active_live_instruments": ["NIFTY", "BANKNIFTY"]},
    )

    assert response.status_code == 200
    assert response.json()["active_live_instruments"] == ["NIFTY", "BANKNIFTY"]

    get_response = api_client.get("/api/v1/system-settings/instrument-firewall")
    assert get_response.json()["active_live_instruments"] == ["NIFTY", "BANKNIFTY"]


def test_patch_rejects_an_unrecognized_instrument(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)

    response = api_client.patch(
        "/api/v1/system-settings/instrument-firewall",
        json={"active_live_instruments": ["FINNIFTY"]},
    )

    assert response.status_code == 400


def test_patch_can_set_an_empty_list(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)

    response = api_client.patch(
        "/api/v1/system-settings/instrument-firewall", json={"active_live_instruments": []}
    )

    assert response.status_code == 200
    assert response.json()["active_live_instruments"] == []
