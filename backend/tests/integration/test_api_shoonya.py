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
    # oauth_callback also runs a real instrument-master sync against the
    # freshly-installed adapter (see that function's own docstring on why —
    # closes the gap where the frontend's expiry picker stayed permanently
    # stuck on the mock adapter's seed data after a real login). Left
    # unmocked, this test's `tok-123` doesn't authenticate against anything
    # real, so it was quietly leaving a FAILED InstrumentMasterSyncLog row
    # behind (sync_instrument_master never raises, it records) — harmless
    # to this test's own assertions, but a real, order-dependent leak into
    # test_instrument_sync.py's count-based assertions when the full suite
    # runs. Mocked here for the same reason exchange_code_for_token already
    # is: no real network calls from a test.
    sync_calls: list[tuple] = []
    monkeypatch.setattr(
        shoonya_module,
        "sync_instrument_master",
        lambda db, broker, exchanges: sync_calls.append((broker, exchanges)),
    )

    response = api_client.get("/shoonya/callback", params={"code": "auth-code"})
    assert response.status_code == 200
    assert "connected" in response.text.lower()
    assert composition.is_shoonya_configured() is True
    assert len(sync_calls) == 1
    assert sync_calls[0][1] == ["NFO"]

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


def test_callback_resets_market_data_registry_when_shoonya_is_the_configured_provider(
    api_client: TestClient, seeded_admin, monkeypatch
):
    """2026-08-12 regression: without this reset, a Shoonya reconnect under
    `MARKET_DATA_PROVIDER=shoonya` would leave ingestion silently stuck on
    whatever it first resolved to at process startup (always the mock —
    see `market_data.registry.reset_for_reconnect`'s own docstring for
    the full ordering-gap writeup).
    """
    _login(api_client, seeded_admin)

    fake_session = OAuthSession(
        auth_result=AuthResult(session_token="tok-123", account_id="FA1"), refresh_token=None
    )
    monkeypatch.setattr(
        shoonya_module, "exchange_code_for_token", lambda settings, code: fake_session
    )
    monkeypatch.setattr(
        shoonya_module, "sync_instrument_master", lambda db, broker, exchanges: None
    )

    real_settings = shoonya_module.get_settings()

    class _FakeMarketDataSettings:
        provider = "shoonya"

    class _FakeSettings:
        shoonya = real_settings.shoonya
        market_data = _FakeMarketDataSettings()

    monkeypatch.setattr(shoonya_module, "get_settings", lambda: _FakeSettings())

    reset_calls: list[None] = []
    monkeypatch.setattr(
        "app.modules.market_data.registry.reset_for_reconnect",
        lambda: reset_calls.append(None),
    )

    response = api_client.get("/shoonya/callback", params={"code": "auth-code"})

    assert response.status_code == 200
    assert len(reset_calls) == 1


def test_callback_survives_reset_for_reconnect_raising(
    api_client: TestClient, seeded_admin, monkeypatch
):
    """2026-08-12 QC finding, fixed: reset_for_reconnect makes a real WS
    subscribe call that can genuinely raise (unlike sync_instrument_master/
    _seed_option_anchors above it, both exception-safe by construction) --
    a transient failure there must not turn a successful login into a 500,
    and the audit event for the successful login must still be recorded.
    """
    _login(api_client, seeded_admin)

    fake_session = OAuthSession(
        auth_result=AuthResult(session_token="tok-123", account_id="FA1"), refresh_token=None
    )
    monkeypatch.setattr(
        shoonya_module, "exchange_code_for_token", lambda settings, code: fake_session
    )
    monkeypatch.setattr(
        shoonya_module, "sync_instrument_master", lambda db, broker, exchanges: None
    )

    real_settings = shoonya_module.get_settings()

    class _FakeMarketDataSettings:
        provider = "shoonya"

    class _FakeSettings:
        shoonya = real_settings.shoonya
        market_data = _FakeMarketDataSettings()

    monkeypatch.setattr(shoonya_module, "get_settings", lambda: _FakeSettings())

    def _raise():
        raise ConnectionError("simulated WS subscribe failure")

    monkeypatch.setattr("app.modules.market_data.registry.reset_for_reconnect", _raise)

    response = api_client.get("/shoonya/callback", params={"code": "auth-code"})

    assert response.status_code == 200
    assert "connected" in response.text.lower()
    assert composition.is_shoonya_configured() is True


def test_callback_does_not_reset_market_data_registry_for_a_non_shoonya_provider(
    api_client: TestClient, seeded_admin, monkeypatch
):
    """Default test settings' `market_data.provider` is `"mock"` — no
    reason to churn Angel One/TrueData's own ingestion on a Shoonya
    reconnect when Shoonya isn't even the configured market-data source.
    """
    _login(api_client, seeded_admin)

    fake_session = OAuthSession(
        auth_result=AuthResult(session_token="tok-123", account_id="FA1"), refresh_token=None
    )
    monkeypatch.setattr(
        shoonya_module, "exchange_code_for_token", lambda settings, code: fake_session
    )
    monkeypatch.setattr(
        shoonya_module, "sync_instrument_master", lambda db, broker, exchanges: None
    )

    reset_calls: list[None] = []
    monkeypatch.setattr(
        "app.modules.market_data.registry.reset_for_reconnect",
        lambda: reset_calls.append(None),
    )

    response = api_client.get("/shoonya/callback", params={"code": "auth-code"})

    assert response.status_code == 200
    assert reset_calls == []


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


class _FakeAdapterWithSeedMethod:
    def __init__(self):
        self.seeded: list[tuple] = []

    def seed_option_anchor(self, underlying, expiry, tsym):
        self.seeded.append((underlying, expiry, tsym))


def test_seed_option_anchors_seeds_every_active_expiry_from_the_db(db):
    """2026-08-12: closes the gap that made starting a strategy against an
    already-correctly-synced expiry still depend on a live `SearchScrip`
    call (unreliable — see `ShoonyaBrokerAdapter.seed_option_anchor`'s own
    docstring). Only active contracts get seeded; a second expiry with no
    active rows must be left alone.
    """
    import uuid as uuid_module
    from datetime import date

    from app.domain.market.models import Instrument, OptionContract, OptionType

    instrument_id = uuid_module.uuid4()
    db.add(
        Instrument(
            id=instrument_id, symbol="NIFTY", exchange="NFO", lot_size=65, tick_size=0.05
        )
    )
    db.flush()
    db.add(
        OptionContract(
            id=uuid_module.uuid4(),
            instrument_id=instrument_id,
            expiry_date=date(2026, 8, 18),
            strike=24400,
            option_type=OptionType.CE,
            symbol="NIFTY18AUG26C24400",
            is_active=True,
        )
    )
    db.add(
        OptionContract(
            id=uuid_module.uuid4(),
            instrument_id=instrument_id,
            expiry_date=date(2026, 8, 6),
            strike=24000,
            option_type=OptionType.CE,
            symbol="NIFTY06AUG26C24000",
            is_active=False,
        )
    )
    db.flush()

    fake_adapter = _FakeAdapterWithSeedMethod()
    shoonya_module._seed_option_anchors(db, fake_adapter)

    assert ("NIFTY", date(2026, 8, 18), "NIFTY18AUG26C24400") in fake_adapter.seeded
    seeded_expiries = {call[1] for call in fake_adapter.seeded if call[0] == "NIFTY"}
    assert date(2026, 8, 6) not in seeded_expiries, "an inactive contract must never be seeded"
