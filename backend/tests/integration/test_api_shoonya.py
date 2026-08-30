"""API-level tests for the Shoonya OAuth login routes. `exchange_code_for_
token` is monkeypatched everywhere here — hitting the real GenAcsTok
endpoint needs a real Shoonya account and a human completing a browser
login, neither of which exist in a test environment (see `broker_adapter/
shoonya/auth.py`'s own "researched, not live-verified" caveat).
"""

from __future__ import annotations

import threading
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

# Captured before any fixture below can monkeypatch it, so the two tests that
# want to exercise real thread-spawning (below) can restore the real
# implementation on top of `synchronous_background_work`'s autouse patch.
_REAL_SPAWN_POST_LOGIN_BACKGROUND_WORK = shoonya_module._spawn_post_login_background_work


@pytest.fixture(autouse=True)
def reset_broker_singleton():
    """`composition.set_broker` mutates process-global state — every other
    test in this suite assumes `get_broker()` resolves to a mock adapter,
    so a fake `ShoonyaBrokerAdapter` installed here must not leak past this
    file's own tests.
    """
    yield
    composition.set_broker(None)


@pytest.fixture(autouse=True)
def isolate_shoonya_session_cache(tmp_path, monkeypatch):
    """2026-08-25: `oauth_callback` now disk-caches every successful login
    (`session_cache.set_cached_shoonya_session`, so a later backend restart
    can reconnect automatically — see `session_cache.py`'s own docstring).
    Without this, every one of this file's successful-callback tests would
    write a real `tok-123` entry to this machine's actual
    `config/credentials/.shoonya_session_cache.json` — autouse so no test
    below has to remember it individually.
    """
    from app.modules.broker_adapter.shoonya import session_cache as session_cache_module

    monkeypatch.setattr(
        session_cache_module, "_CACHE_PATH", tmp_path / ".shoonya_session_cache.json"
    )
    session_cache_module._session = None
    session_cache_module._loaded_from_disk = True
    yield
    session_cache_module._session = None
    session_cache_module._loaded_from_disk = True


@pytest.fixture(autouse=True)
def stub_product_capabilities(monkeypatch):
    """2026-08-21: bracket-order research Phase A added
    `ShoonyaBrokerAdapter.get_product_capabilities`, called unconditionally
    from `oauth_callback` — like `exchange_code_for_token` above, a real
    call makes a genuine outbound `UserDetails` request against
    `settings.shoonya.api_host` (the real `https://api.shoonya.com/...`
    host by default, since none of this file's fake-settings blocks
    override it). Stubbed here, autouse, so every test in this file gets
    this for free rather than each one having to remember it individually
    — the same class of gap `sync_instrument_master`'s own mock comment
    above already flags for a different call.
    """
    from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter

    monkeypatch.setattr(
        ShoonyaBrokerAdapter,
        "get_product_capabilities",
        lambda self: {"read_only": True, "source": "UserDetails", "stubbed": True},
    )


@pytest.fixture(autouse=True)
def stub_run_daily_bootstrap(monkeypatch):
    """2026-08-25: `oauth_callback` now also calls `session.bootstrapper.
    run_daily_bootstrap()` on every successful login (closes the "auto-spawn
    doesn't self-retry after a same-morning reconnect" gap). That function's
    own default `session_factory` is the *production* `session_scope`, not
    this suite's isolated engine — same reasoning
    `test_bootstrap_now_calls_run_daily_bootstrap`
    (test_api_auth_and_sessions.py) already documents for the sibling
    login-triggered endpoint. Stubbed here, autouse, so every test in this
    file gets this for free rather than each one having to remember it
    individually — same shape as `stub_product_capabilities` above.
    """
    import app.modules.session.bootstrapper as bootstrapper_module

    monkeypatch.setattr(bootstrapper_module, "run_daily_bootstrap", lambda: None)


@pytest.fixture(autouse=True)
def synchronous_background_work(monkeypatch, engine):
    """2026-08-26: `oauth_callback` now defers instrument-master sync,
    market-data reset, and the daily-bootstrap retry to a background thread
    (see `_run_post_login_background_work`'s own docstring — this closes a
    real nginx-504 incident). Every test below that asserts on those side
    effects right after the HTTP response returns implicitly assumes
    synchronous completion, same as before this change — so run the
    background work inline instead of spawning a real thread, using this
    suite's own isolated `engine` (not `_run_post_login_background_work`'s
    default `session_factory=session_scope`, which would touch the real
    production DB — same trap `stub_run_daily_bootstrap` above already
    documents for the sibling function it wraps).

    The two tests that specifically want to prove real threading behavior
    restore `_REAL_SPAWN_POST_LOGIN_BACKGROUND_WORK` on top of this.
    """
    test_session_factory = sessionmaker(bind=engine, future=True)

    def _sync_spawn(adapter, *, market_data_provider: str) -> None:
        shoonya_module._run_post_login_background_work(
            adapter,
            market_data_provider=market_data_provider,
            session_factory=test_session_factory,
        )

    monkeypatch.setattr(shoonya_module, "_spawn_post_login_background_work", _sync_spawn)


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
    assert response.json() == {
        "connected": False,
        "session_valid": False,
        "feed_age_seconds": None,
        "feed_state": None,
    }


def test_status_connected_when_configured_and_feed_is_fresh(
    api_client: TestClient, seeded_admin, monkeypatch
):
    import app.modules.market_data.freshness as freshness_module

    monkeypatch.setattr(shoonya_module, "is_shoonya_configured", lambda: True)
    monkeypatch.setattr(freshness_module, "any_underlying_feed_fresh", lambda db, symbols: True)
    monkeypatch.setattr(
        freshness_module,
        "underlying_feed_freshness",
        lambda db, symbols: (3.0, freshness_module.FreshnessState.LIVE),
    )

    _login(api_client, seeded_admin)
    response = api_client.get("/shoonya/status")

    assert response.status_code == 200
    assert response.json() == {
        "connected": True,
        "session_valid": True,
        "feed_age_seconds": 3.0,
        "feed_state": "live",
    }


def test_status_session_valid_but_not_connected_when_feed_is_stale(
    api_client: TestClient, seeded_admin, monkeypatch
):
    """A real adapter is installed (session_valid) but no fresh tick/bar
    exists — e.g. before the morning feed warms up, or a mid-session WS drop.
    """
    import app.modules.market_data.freshness as freshness_module

    monkeypatch.setattr(shoonya_module, "is_shoonya_configured", lambda: True)
    monkeypatch.setattr(freshness_module, "any_underlying_feed_fresh", lambda db, symbols: False)
    monkeypatch.setattr(
        freshness_module,
        "underlying_feed_freshness",
        lambda db, symbols: (None, freshness_module.FreshnessState.DEAD),
    )

    _login(api_client, seeded_admin)
    response = api_client.get("/shoonya/status")

    assert response.status_code == 200
    assert response.json() == {
        "connected": False,
        "session_valid": True,
        "feed_age_seconds": None,
        "feed_state": "dead",
    }


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


def test_callback_success_caches_the_session_to_disk(
    api_client: TestClient, seeded_admin, monkeypatch
):
    """2026-08-25: a successful login must persist its `AuthResult` via
    `session_cache.set_cached_shoonya_session`, so `app.main._attempt_
    shoonya_reconnect_from_cache` can restore it on a later backend restart
    without a fresh manual browser login.
    """
    _login(api_client, seeded_admin)

    fake_auth_result = AuthResult(session_token="tok-123", account_id="FA1")
    fake_session = OAuthSession(auth_result=fake_auth_result, refresh_token=None)
    monkeypatch.setattr(
        shoonya_module, "exchange_code_for_token", lambda settings, code: fake_session
    )
    monkeypatch.setattr(
        shoonya_module, "sync_instrument_master", lambda db, broker, exchanges: None
    )

    response = api_client.get("/shoonya/callback", params={"code": "auth-code"})
    assert response.status_code == 200

    from app.modules.broker_adapter.shoonya.session_cache import get_cached_shoonya_session

    assert get_cached_shoonya_session() == fake_auth_result


def test_callback_failure_does_not_cache_a_session(
    api_client: TestClient, seeded_admin, monkeypatch
):
    _login(api_client, seeded_admin)

    def _raise(settings, code):
        raise ShoonyaAuthError("bad checksum")

    monkeypatch.setattr(shoonya_module, "exchange_code_for_token", _raise)

    response = api_client.get("/shoonya/callback", params={"code": "auth-code"})
    assert response.status_code == 200

    from app.modules.broker_adapter.shoonya.session_cache import get_cached_shoonya_session

    assert get_cached_shoonya_session() is None


def test_callback_invokes_product_capabilities_diagnostic(
    api_client: TestClient, seeded_admin, monkeypatch
):
    """Bracket-order research Phase A — a successful login should trigger
    the read-only product-capabilities diagnostic exactly once, alongside
    everything else `oauth_callback` already does.
    """
    from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter

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

    calls: list[None] = []

    def _spy_get_product_capabilities(self):
        calls.append(None)
        return {"read_only": True}

    monkeypatch.setattr(
        ShoonyaBrokerAdapter, "get_product_capabilities", _spy_get_product_capabilities
    )

    response = api_client.get("/shoonya/callback", params={"code": "auth-code"})

    assert response.status_code == 200
    assert len(calls) == 1


def test_callback_survives_product_capabilities_failure(
    api_client: TestClient, seeded_admin, monkeypatch
):
    """A diagnostic failure must never turn a successful login into a 500
    — same discipline already proven for `reset_for_reconnect` above.
    """
    from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter

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

    def _raise(self):
        raise ConnectionError("simulated UserDetails failure")

    monkeypatch.setattr(ShoonyaBrokerAdapter, "get_product_capabilities", _raise)

    response = api_client.get("/shoonya/callback", params={"code": "auth-code"})

    assert response.status_code == 200
    assert "connected" in response.text.lower()


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


def test_callback_retries_auto_spawn_via_run_daily_bootstrap(
    api_client: TestClient, seeded_admin, monkeypatch
):
    """2026-08-25: a successful Shoonya connect must re-attempt the daily
    auto-spawn sweep, closing the gap where a strategy that failed to spawn
    at 09:00 (Shoonya not connected yet) never retried until the next day.
    """
    import app.modules.session.bootstrapper as bootstrapper_module

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

    calls: list[None] = []
    monkeypatch.setattr(bootstrapper_module, "run_daily_bootstrap", lambda: calls.append(None))

    response = api_client.get("/shoonya/callback", params={"code": "auth-code"})

    assert response.status_code == 200
    assert "connected" in response.text.lower()
    assert len(calls) == 1


def test_callback_survives_run_daily_bootstrap_raising(
    api_client: TestClient, seeded_admin, monkeypatch
):
    """A retry failure must never turn a successful login into a 500 -- same
    discipline already proven for `reset_for_reconnect` above.
    """
    import app.modules.session.bootstrapper as bootstrapper_module

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

    def _raise():
        raise RuntimeError("simulated bootstrap failure")

    monkeypatch.setattr(bootstrapper_module, "run_daily_bootstrap", _raise)

    response = api_client.get("/shoonya/callback", params={"code": "auth-code"})

    assert response.status_code == 200
    assert "connected" in response.text.lower()
    assert composition.is_shoonya_configured() is True


def test_callback_response_does_not_wait_for_background_work(
    api_client: TestClient, seeded_admin, monkeypatch
):
    """Core proof of the 2026-08-26 fix: the HTTP response must return
    before the background work finishes, not after -- that's the whole
    point of backgrounding it (see `_run_post_login_background_work`'s own
    docstring for the nginx-504 incident this closes). Restores the real
    `_spawn_post_login_background_work` (the `synchronous_background_work`
    autouse fixture replaces it for every other test in this file) so a
    real thread actually gets spawned here, with a fake background body
    (no real DB session concerns to worry about) so the test controls
    exactly when it "finishes".
    """
    monkeypatch.setattr(
        shoonya_module,
        "_spawn_post_login_background_work",
        _REAL_SPAWN_POST_LOGIN_BACKGROUND_WORK,
    )

    _login(api_client, seeded_admin)

    fake_session = OAuthSession(
        auth_result=AuthResult(session_token="tok-123", account_id="FA1"), refresh_token=None
    )
    monkeypatch.setattr(
        shoonya_module, "exchange_code_for_token", lambda settings, code: fake_session
    )

    started = threading.Event()
    release = threading.Event()

    def _fake_background_work(adapter, *, market_data_provider, session_factory=None):
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(shoonya_module, "_run_post_login_background_work", _fake_background_work)

    response = api_client.get("/shoonya/callback", params={"code": "auth-code"})

    assert response.status_code == 200
    assert "connected" in response.text.lower()
    # The response above already returned -- if backgrounding didn't work,
    # this would have blocked for up to 5s inside the request itself.
    assert started.wait(timeout=2), "background work was never started"
    release.set()  # let the background thread finish so it doesn't leak past the test


def test_spawn_post_login_background_work_skips_when_already_running(monkeypatch):
    """A second reconnect landing while the first's background work is
    still in flight must not race a duplicate `sync_instrument_master`/
    `run_daily_bootstrap` pass -- it should log and skip instead.
    """
    started = threading.Event()
    release = threading.Event()
    call_count = 0

    def _fake_background_work(adapter, *, market_data_provider, session_factory=None):
        nonlocal call_count
        call_count += 1
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(shoonya_module, "_run_post_login_background_work", _fake_background_work)

    _REAL_SPAWN_POST_LOGIN_BACKGROUND_WORK(object(), market_data_provider="mock")
    assert started.wait(timeout=2)

    # A second spawn while the first is still running must be a no-op.
    _REAL_SPAWN_POST_LOGIN_BACKGROUND_WORK(object(), market_data_provider="mock")

    release.set()
    # Give the first thread a moment to fully release the lock before
    # asserting the total call count and trying a third spawn.
    import time

    time.sleep(0.1)
    assert call_count == 1

    release.clear()
    _REAL_SPAWN_POST_LOGIN_BACKGROUND_WORK(object(), market_data_provider="mock")
    assert started.wait(timeout=2)
    release.set()
    time.sleep(0.1)
    assert call_count == 2


def test_run_post_login_background_work_isolates_step_failures(monkeypatch, engine):
    """`sync_instrument_master` raising must not prevent `reset_for_
    reconnect`/`run_daily_bootstrap` from still running afterward -- an
    uncaught exception in a background thread has no request left to fail
    loudly, so each step must be isolated.
    """

    def _raise(db, broker, exchanges):
        raise RuntimeError("simulated instrument-master sync failure")

    monkeypatch.setattr(shoonya_module, "sync_instrument_master", _raise)

    reset_calls: list[None] = []
    monkeypatch.setattr(
        "app.modules.market_data.registry.reset_for_reconnect",
        lambda: reset_calls.append(None),
    )
    bootstrap_calls: list[None] = []
    import app.modules.session.bootstrapper as bootstrapper_module

    monkeypatch.setattr(
        bootstrapper_module, "run_daily_bootstrap", lambda: bootstrap_calls.append(None)
    )

    test_session_factory = sessionmaker(bind=engine, future=True)
    shoonya_module._run_post_login_background_work(
        object(), market_data_provider="shoonya", session_factory=test_session_factory
    )

    assert reset_calls == [None]
    assert bootstrap_calls == [None]


def test_run_post_login_background_work_uses_its_own_session_not_the_request_session(
    monkeypatch, engine
):
    """The session passed into `sync_instrument_master`/`_seed_option_
    anchors` must come from the explicit `session_factory`, never a
    request-scoped session -- proven here by asserting it's a distinct
    `Session` instance bound to the test's own isolated engine.
    """
    test_session_factory = sessionmaker(bind=engine, future=True)
    seen_sessions: list[object] = []

    def _spy_sync_instrument_master(db, broker, exchanges):
        seen_sessions.append(db)

    monkeypatch.setattr(shoonya_module, "sync_instrument_master", _spy_sync_instrument_master)
    monkeypatch.setattr(
        "app.modules.market_data.registry.reset_for_reconnect", lambda: None
    )
    import app.modules.session.bootstrapper as bootstrapper_module

    monkeypatch.setattr(bootstrapper_module, "run_daily_bootstrap", lambda: None)

    shoonya_module._run_post_login_background_work(
        object(), market_data_provider="shoonya", session_factory=test_session_factory
    )

    assert len(seen_sessions) == 1
    assert seen_sessions[0].bind is engine


def test_callback_does_not_retry_auto_spawn_on_failed_login(
    api_client: TestClient, seeded_admin, monkeypatch
):
    import app.modules.session.bootstrapper as bootstrapper_module

    _login(api_client, seeded_admin)

    def _raise(settings, code):
        raise ShoonyaAuthError("bad checksum")

    monkeypatch.setattr(shoonya_module, "exchange_code_for_token", _raise)

    calls: list[None] = []
    monkeypatch.setattr(bootstrapper_module, "run_daily_bootstrap", lambda: calls.append(None))

    response = api_client.get("/shoonya/callback", params={"code": "auth-code"})

    assert response.status_code == 200
    assert "failed" in response.text.lower()
    assert calls == []


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


def test_search_scrip_diagnostic_requires_live_session(api_client: TestClient, seeded_admin):
    """No `set_broker(real_adapter)` call in this test -- `get_broker()`
    defaults to the mock, matching every other test in this suite unless a
    test explicitly installs a real ShoonyaBrokerAdapter (see the two
    request-body tests below).
    """
    _login(api_client, seeded_admin)
    response = api_client.get("/shoonya/search-scrip?exchange=NSE&text=VIX")
    assert response.status_code == 409


def test_subscribe_diagnostic_requires_live_session(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    response = api_client.get("/shoonya/subscribe-diagnostic?symbols=NIFTY18AUG26C24400")
    assert response.status_code == 409


def _install_fake_shoonya_adapter():
    """Lightweight, network-free `ShoonyaBrokerAdapter` construction --
    same pattern `tests/unit/test_shoonya_adapter.py`'s own `_adapter()`
    helper uses, needed here only to pass this endpoint's `isinstance`
    check; no REST/WS calls are exercised by the tests below.
    """
    from pydantic import SecretStr

    from app.config.settings import ShoonyaSettings
    from app.modules.broker_adapter.base.contracts import AuthResult
    from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter

    class _FakeRestClient:
        def close(self):
            pass

    settings = ShoonyaSettings(
        client_id="TESTCID",
        secret_code=SecretStr("TESTSECRET"),
        user_id="FA12345",
        api_host="https://api.shoonya.test/NorenWClientAPI",
        ws_host="wss://api.shoonya.test/NorenWSAPI/",
    )
    auth_result = AuthResult(session_token="tok", account_id="FA12345")
    adapter = ShoonyaBrokerAdapter(settings, auth_result, rest_client=_FakeRestClient())
    composition.set_broker(adapter)


def test_subscribe_diagnostic_requires_symbols(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    _install_fake_shoonya_adapter()
    response = api_client.get("/shoonya/subscribe-diagnostic?symbols=")
    assert response.status_code == 422
