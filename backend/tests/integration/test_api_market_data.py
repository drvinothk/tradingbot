"""Ops-Hardening Phase 4: GET/PATCH /api/v1/market-data/provider-preference
-- API-level behavior (auth, validation, persistence, and live-apply to an
existing FailoverMarketDataProvider singleton when one exists).
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
from app.domain.ops.models import MarketDataProviderPreference
from app.main import app
from app.modules.broker_adapter.base.contracts import Tick
from app.modules.market_data.providers.base import BaseMarketDataProvider
from app.modules.market_data.providers.failover import FailoverMarketDataProvider

ADMIN_PASSWORD = "correct horse battery staple 123!"


class _FakeProvider(BaseMarketDataProvider):
    def __init__(self) -> None:
        self.subscribe_calls: list[list[str]] = []

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def subscribe_ticks(self, symbols, on_tick, on_depth=None) -> None:
        self.subscribe_calls.append(list(symbols))

    def unsubscribe_ticks(self, symbols) -> None:
        pass

    def get_latest_tick(self, symbol) -> Tick | None:
        return None

    def get_price_history(self, underlying, start, end, timeframe_seconds=60):
        return []


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
        workspace = Workspace(id=uuid.uuid4(), name=f"md-pref-test-{uuid.uuid4().hex[:8]}")
        db.add(workspace)
        db.flush()

        role = Role(id=uuid.uuid4(), name=f"md-pref-admin-{uuid.uuid4().hex[:8]}")
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
            email=f"md-pref-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password(ADMIN_PASSWORD),
            display_name="Market Data Pref Test Admin",
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
        cleanup_db.query(MarketDataProviderPreference).filter(
            MarketDataProviderPreference.workspace_id == ids["workspace_id"]
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
    response = api_client.get("/api/v1/market-data/provider-preference")
    assert response.status_code == 401


def test_get_with_no_preference_returns_null(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)

    response = api_client.get("/api/v1/market-data/provider-preference")

    assert response.status_code == 200
    assert response.json() == {"active_provider": None, "live_active_leg": None}


def test_patch_sets_a_preference(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)

    response = api_client.patch(
        "/api/v1/market-data/provider-preference", json={"active_provider": "angel_one"}
    )

    assert response.status_code == 200
    assert response.json()["active_provider"] == "angel_one"

    get_response = api_client.get("/api/v1/market-data/provider-preference")
    assert get_response.json()["active_provider"] == "angel_one"


def test_patch_rejects_an_unrecognized_provider(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)

    response = api_client.patch(
        "/api/v1/market-data/provider-preference", json={"active_provider": "truedata"}
    )

    assert response.status_code == 400


def test_patch_null_clears_an_existing_preference(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    api_client.patch(
        "/api/v1/market-data/provider-preference", json={"active_provider": "shoonya"}
    )

    response = api_client.patch(
        "/api/v1/market-data/provider-preference", json={"active_provider": None}
    )

    assert response.status_code == 200
    assert response.json()["active_provider"] is None


def test_patch_without_a_live_failover_provider_still_persists(
    api_client: TestClient, seeded_admin, monkeypatch
):
    import app.api.v1.market_data as market_data_api

    # market_data.py does `from ... import get_market_data_provider`, so its
    # own imported name (not provider_composition's) must be patched --
    # monkeypatching the origin module doesn't affect an already-bound
    # `from X import Y` reference in a different module's namespace.
    monkeypatch.setattr(market_data_api, "get_market_data_provider", lambda: _FakeProvider())
    _login(api_client, seeded_admin)

    response = api_client.patch(
        "/api/v1/market-data/provider-preference", json={"active_provider": "shoonya"}
    )

    assert response.status_code == 200
    assert response.json()["active_provider"] == "shoonya"
    assert response.json()["live_active_leg"] is None


def test_patch_applies_live_to_an_existing_failover_provider(
    api_client: TestClient, seeded_admin, monkeypatch
):
    primary, backup = _FakeProvider(), _FakeProvider()
    failover = FailoverMarketDataProvider(
        primary=primary,
        backup=backup,
        primary_name="shoonya",
        backup_name="angel_one",
        failover_threshold_seconds=5.0,
        recovery_stabilization_seconds=20.0,
        backup_retry_seconds=30.0,
        poll_interval_seconds=1_000_000.0,
    )
    failover.subscribe_ticks(["NIFTY"], on_tick=lambda t: None)

    import app.api.v1.market_data as market_data_api

    monkeypatch.setattr(market_data_api, "get_market_data_provider", lambda: failover)
    _login(api_client, seeded_admin)

    response = api_client.patch(
        "/api/v1/market-data/provider-preference", json={"active_provider": "angel_one"}
    )

    assert response.status_code == 200
    assert response.json()["live_active_leg"] == "angel_one"
    assert failover.active_provider_name == "angel_one"
    assert backup.subscribe_calls == [["NIFTY"]]

    failover.disconnect()
