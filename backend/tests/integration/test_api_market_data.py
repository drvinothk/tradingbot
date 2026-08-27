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


@pytest.fixture(autouse=True)
def _reset_alice_blue_session() -> Generator[None, None, None]:
    """`failover_backup_provider` defaults to `"alice_blue"` as of
    2026-08-25 -- without this, `get_alice_blue_session()`'s own disk-cache
    (`alice_blue_session.py`'s own docstring) would leak whatever real
    session happens to be cached on the machine running the suite into
    tests that assume "no Alice Blue session" (e.g. the failback-diagnostic
    409 tests below), making them pass or fail depending on unrelated local
    dev state instead of deterministically. `reset_for_tests()` is built
    exactly for this -- it clears only the in-memory singleton, never the
    real on-disk cache file.
    """
    from app.modules.market_data.providers import alice_blue_session

    alice_blue_session.reset_for_tests()
    yield
    alice_blue_session.reset_for_tests()


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
        from app.domain.ops.models import MarketDataDiagnosticRun, MarketDataDiagnosticSnapshot
        from app.modules.market_data import diagnostic_session

        # Defensively join any diagnostic background thread a test left
        # running, so it can't insert a snapshot row between the deletes below.
        diagnostic_session.stop_all()

        cleanup_db.query(AuditEvent).filter(
            AuditEvent.workspace_id == ids["workspace_id"]
        ).delete()
        cleanup_db.query(MarketDataProviderPreference).filter(
            MarketDataProviderPreference.workspace_id == ids["workspace_id"]
        ).delete()
        run_ids = [
            row[0]
            for row in cleanup_db.query(MarketDataDiagnosticRun.id).filter(
                MarketDataDiagnosticRun.workspace_id == ids["workspace_id"]
            )
        ]
        if run_ids:
            cleanup_db.query(MarketDataDiagnosticSnapshot).filter(
                MarketDataDiagnosticSnapshot.run_id.in_(run_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(MarketDataDiagnosticRun).filter(
                MarketDataDiagnosticRun.id.in_(run_ids)
            ).delete(synchronize_session=False)
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
        "/api/v1/market-data/provider-preference", json={"active_provider": "shoonya"}
    )

    assert response.status_code == 200
    assert response.json()["active_provider"] == "shoonya"

    get_response = api_client.get("/api/v1/market-data/provider-preference")
    assert get_response.json()["active_provider"] == "shoonya"


def test_patch_accepts_alice_blue(api_client: TestClient, seeded_admin):
    """alice_blue promoted 2026-08-25 to a UI-selectable failover override,
    same as shoonya -- see RECOGNIZED_OVERRIDE_PROVIDERS's own comment.
    """
    _login(api_client, seeded_admin)

    response = api_client.patch(
        "/api/v1/market-data/provider-preference", json={"active_provider": "alice_blue"}
    )

    assert response.status_code == 200
    assert response.json()["active_provider"] == "alice_blue"


def test_patch_rejects_an_unrecognized_provider(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)

    response = api_client.patch(
        "/api/v1/market-data/provider-preference", json={"active_provider": "truedata"}
    )

    assert response.status_code == 400


def test_patch_rejects_archived_angel_one(api_client: TestClient, seeded_admin):
    """angel_one was archived 2026-08-21 (see CLAUDE.md) -- still a valid
    provider_composition backend, but no longer a UI-selectable override.
    """
    _login(api_client, seeded_admin)

    response = api_client.patch(
        "/api/v1/market-data/provider-preference", json={"active_provider": "angel_one"}
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
        primary_name="truedata",
        backup_name="shoonya",
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
        "/api/v1/market-data/provider-preference", json={"active_provider": "shoonya"}
    )

    assert response.status_code == 200
    assert response.json()["live_active_leg"] == "shoonya"
    assert failover.active_provider_name == "shoonya"
    assert backup.subscribe_calls == [["NIFTY"]]

    failover.disconnect()


# ---------- WS quality diagnostic (Test Default/Test Failback/Both) ----------


@pytest.fixture(autouse=True)
def _reset_diagnostic_session(engine) -> Generator[None, None, None]:
    """`diagnostic_session` defaults to the production `session_scope` --
    without this, its background threads would write real rows into the dev
    DB from inside a test run, the exact incident class CLAUDE.md already
    documents for a different module. Bound to this file's own isolated
    `engine` fixture instead, same real-commit-not-rolled-back-transaction
    reasoning `seeded_admin`'s own `session_factory` already relies on
    (a background thread needs a real commit to be visible to the test's
    own separate connection, not a transaction the test fixture will roll
    back).
    """
    from contextlib import contextmanager

    from app.modules.market_data import diagnostic_session

    session_factory = sessionmaker(bind=engine, future=True)

    @contextmanager
    def _test_session_scope():
        db = session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    diagnostic_session.reset_for_tests(session_factory=_test_session_scope)
    yield
    diagnostic_session.reset_for_tests()


def test_diagnostic_endpoints_require_login(api_client: TestClient):
    start_resp = api_client.post(
        "/api/v1/market-data/diagnostic/start", json={"mode": "default"}
    )
    stop_resp = api_client.post("/api/v1/market-data/diagnostic/stop", json={"mode": "default"})

    assert start_resp.status_code == 401
    assert stop_resp.status_code == 401
    assert api_client.get("/api/v1/market-data/diagnostic/status").status_code == 401


def test_diagnostic_start_rejects_an_unknown_mode(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    response = api_client.post("/api/v1/market-data/diagnostic/start", json={"mode": "bogus"})
    assert response.status_code == 400


def test_diagnostic_default_role_starts_and_stops_cleanly(api_client: TestClient, seeded_admin):
    """"default" never needs a real broker connection -- it only reads
    `get_market_data_provider().get_latest_tick()`, which never triggers a
    real connect (no ticks have ever arrived, so it just returns `None`),
    matching `_validate_can_run`'s own "always safe" reasoning regardless
    of which provider `MARKET_DATA_PROVIDER` happens to resolve to in
    whatever environment the suite runs in -- deliberately not asserting a
    specific provider name here, since that's environment config, not this
    endpoint's own behavior.
    """
    from app.config.settings import get_settings

    expected_provider = get_settings().market_data.provider
    _login(api_client, seeded_admin)

    start_response = api_client.post(
        "/api/v1/market-data/diagnostic/start", json={"mode": "default"}
    )
    assert start_response.status_code == 200
    body = start_response.json()
    assert body["default"]["already_running"] is False
    assert body["default"]["provider"] == expected_provider

    status_response = api_client.get("/api/v1/market-data/diagnostic/status")
    assert status_response.json()["default"]["running"] is True

    stop_response = api_client.post(
        "/api/v1/market-data/diagnostic/stop", json={"mode": "default"}
    )
    assert stop_response.status_code == 200
    assert stop_response.json()["default"]["was_running"] is True

    status_after_stop = api_client.get("/api/v1/market-data/diagnostic/status")
    assert status_after_stop.json()["default"]["running"] is False


def test_diagnostic_default_role_start_is_idempotent(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    first = api_client.post("/api/v1/market-data/diagnostic/start", json={"mode": "default"})
    second = api_client.post("/api/v1/market-data/diagnostic/start", json={"mode": "default"})

    assert first.json()["default"]["already_running"] is False
    assert second.json()["default"]["already_running"] is True
    assert first.json()["default"]["run_id"] == second.json()["default"]["run_id"]

    # Stop the background diagnostic thread this test started -- otherwise it
    # keeps writing MarketDataDiagnosticSnapshot rows (real commits) and races
    # `seeded_admin`'s teardown, which deletes the parent run row: an
    # intermittent FK violation that aborts teardown and cascades leaked
    # Permission rows into ~50 later tests.
    api_client.post("/api/v1/market-data/diagnostic/stop", json={"mode": "default"})


def test_diagnostic_failback_role_rejects_unsupported_provider_synchronously(
    api_client: TestClient, seeded_admin, monkeypatch
):
    """`_validate_can_run` must fail synchronously (409), not silently start
    a thread that only errors out later, for a `failover_backup_provider`
    diagnostic_session has no isolated/shared test for -- see that
    function's own docstring for why this distinction was worth a dedicated
    fix. Test settings default `failover_backup_provider` is now
    `"alice_blue"` (promoted 2026-08-25, a genuinely supported failback
    role), so this test forces an unsupported name explicitly rather than
    relying on the default to exercise that branch.
    """
    from app.config.settings import get_settings

    monkeypatch.setattr(get_settings().market_data, "failover_backup_provider", "truedata")
    _login(api_client, seeded_admin)
    response = api_client.post("/api/v1/market-data/diagnostic/start", json={"mode": "failback"})
    assert response.status_code == 409

    status_response = api_client.get("/api/v1/market-data/diagnostic/status")
    assert status_response.json()["failback"]["running"] is False


def test_diagnostic_failback_role_rejects_when_shoonya_not_connected(
    api_client: TestClient, seeded_admin, monkeypatch
):
    from app.config.settings import get_settings

    monkeypatch.setattr(get_settings().market_data, "failover_backup_provider", "shoonya")
    _login(api_client, seeded_admin)

    response = api_client.post("/api/v1/market-data/diagnostic/start", json={"mode": "failback"})
    assert response.status_code == 409
    assert "Shoonya is not connected" in response.json()["detail"]


def test_diagnostic_both_mode_is_atomic_when_one_role_fails_validation(
    api_client: TestClient, seeded_admin
):
    """"both" validates every role before starting any of them -- a
    rejection (failback: no live Alice Blue session in test settings, the
    default `failover_backup_provider` as of 2026-08-25) must leave
    *nothing* running, including the otherwise-always-safe "default" role.
    See `diagnostic_session.start_many`'s own docstring for why this
    atomicity was a deliberate design choice, not incidental.
    """
    _login(api_client, seeded_admin)
    response = api_client.post("/api/v1/market-data/diagnostic/start", json={"mode": "both"})
    assert response.status_code == 409

    status_response = api_client.get("/api/v1/market-data/diagnostic/status").json()
    assert status_response["default"]["running"] is False
    assert status_response["failback"]["running"] is False
