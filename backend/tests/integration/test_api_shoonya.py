"""API-level tests for the Shoonya OAuth login routes. `exchange_code_for_
token` is monkeypatched everywhere here — hitting the real GenAcsTok
endpoint needs a real Shoonya account and a human completing a browser
login, neither of which exist in a test environment (see `broker_adapter/
shoonya/auth.py`'s own "researched, not live-verified" caveat).
"""

from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import app.api.v1.shoonya as shoonya_module
from app.core.db.session import get_db
from app.core.security.passwords import hash_password
from app.domain.identity.models import Permission, Role, RolePermission, User, UserRole, Workspace
from app.main import app
from app.modules.broker_adapter import composition
from app.modules.broker_adapter.base.contracts import AuthResult
from app.modules.broker_adapter.shoonya.auth import OAuthSession, ShoonyaAuthError

ADMIN_PASSWORD = "correct horse battery staple 123!"


@pytest.fixture(autouse=True)
def reset_broker_singleton():
    """`composition.set_broker` mutates process-global state — every other
    test in this suite assumes `get_broker()` resolves to a mock adapter,
    so a fake `ShoonyaBrokerAdapter` installed here must not leak past this
    file's own tests.
    """
    yield
    composition.set_broker(None)


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
        workspace = Workspace(id=uuid.uuid4(), name=f"shoonya-test-{uuid.uuid4().hex[:8]}")
        db.add(workspace)
        db.flush()

        role = Role(id=uuid.uuid4(), name=f"shoonya-admin-{uuid.uuid4().hex[:8]}")
        db.add(role)
        db.flush()
        permission_ids: list[uuid.UUID] = []
        for code in ("session.start",):
            permission = Permission(id=uuid.uuid4(), code=code, description="")
            db.add(permission)
            db.flush()
            permission_ids.append(permission.id)
            db.add(RolePermission(role_id=role.id, permission_id=permission.id))

        user = User(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            email=f"shoonya-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password(ADMIN_PASSWORD),
            display_name="Shoonya Test Admin",
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

        cleanup_db.query(AuditEvent).filter(AuditEvent.workspace_id == ids["workspace_id"]).delete()
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


def test_login_url_requires_login(api_client: TestClient):
    response = api_client.get("/shoonya/login-url")
    assert response.status_code == 401


def test_status_requires_login(api_client: TestClient):
    response = api_client.get("/shoonya/status")
    assert response.status_code == 401


def test_status_reports_not_connected_by_default(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    response = api_client.get("/shoonya/status")
    assert response.status_code == 200
    assert response.json() == {"connected": False}


def test_login_url_returns_409_when_credentials_not_configured(
    api_client: TestClient, seeded_admin, monkeypatch
):
    _login(api_client, seeded_admin)
    from app.config.settings import ShoonyaSettings

    monkeypatch.setattr(
        shoonya_module, "get_settings", lambda: _fake_settings(ShoonyaSettings(client_id=""))
    )
    response = api_client.get("/shoonya/login-url")
    assert response.status_code == 409


def test_login_url_returns_authorize_url_when_configured(
    api_client: TestClient, seeded_admin, monkeypatch
):
    _login(api_client, seeded_admin)
    from pydantic import SecretStr

    from app.config.settings import ShoonyaSettings

    settings = ShoonyaSettings(
        client_id="TESTCID",
        secret_code=SecretStr("TESTSECRET"),
        user_id="FA12345",
        redirect_url="http://127.0.0.1:5000/shoonya/callback",
        oauth_authorize_url="https://api.shoonya.test/OAuthlogin/authorize/oauth",
    )
    monkeypatch.setattr(shoonya_module, "get_settings", lambda: _fake_settings(settings))

    response = api_client.get("/shoonya/login-url")
    assert response.status_code == 200
    assert "client_id=TESTCID" in response.json()["authorize_url"]


def test_callback_success_installs_broker_and_audits(
    api_client: TestClient, seeded_admin, engine, monkeypatch
):
    _login(api_client, seeded_admin)

    fake_session = OAuthSession(
        auth_result=AuthResult(session_token="tok-123", account_id="FA1"), refresh_token=None
    )
    monkeypatch.setattr(
        shoonya_module, "exchange_code_for_token", lambda settings, code: fake_session
    )

    response = api_client.get("/shoonya/callback", params={"code": "auth-code"})
    assert response.status_code == 200
    assert "connected" in response.text.lower()
    assert composition.is_shoonya_configured() is True

    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as verify_db:
        from app.domain.audit.models import AuditEvent

        events = (
            verify_db.query(AuditEvent)
            .filter(
                AuditEvent.workspace_id == seeded_admin["workspace_id"],
                AuditEvent.event_type == "shoonya.oauth_login_succeeded",
            )
            .all()
        )
        assert len(events) == 1


def test_callback_failure_returns_html_error_without_installing_broker(
    api_client: TestClient, seeded_admin, monkeypatch
):
    _login(api_client, seeded_admin)

    def _raise(settings, code):
        raise ShoonyaAuthError("bad checksum")

    monkeypatch.setattr(shoonya_module, "exchange_code_for_token", _raise)

    response = api_client.get("/shoonya/callback", params={"code": "auth-code"})
    assert response.status_code == 200
    assert "failed" in response.text.lower()
    assert composition.is_shoonya_configured() is False


def _fake_settings(shoonya_settings):
    class _Settings:
        shoonya = shoonya_settings

    return _Settings()
