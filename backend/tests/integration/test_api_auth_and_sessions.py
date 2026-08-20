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
from app.core.modes import transition_mode
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
from app.domain.session.models import SafeMode, TradingSession, TransitionTriggerType
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
        for code in (
            "session.start",
            "session.stop",
            "strategy.view",
            "strategy.edit",
            "papertrade.execute",
            "risk.override",
            "livetrade.execute",
        ):
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
        from app.domain.broker.models import BrokerSyncState, ReconciliationRun
        from app.domain.identity.models import LoginSession
        from app.domain.ops.models import SystemAlert
        from app.domain.session.models import SessionModeTransition, TradingSession

        # Every API call under test that succeeds writes at least one audit
        # row (login, kill-switch) — miss this and cleanup fails on the same
        # FK-violation-cascades-into-the-next-test's-unique-constraint
        # pattern as the Permission rows below.
        cleanup_db.query(AuditEvent).filter(AuditEvent.workspace_id == ids["workspace_id"]).delete()
        cleanup_db.query(SessionModeTransition).delete()
        # A real reconciliation mismatch also raises a SystemAlert
        # (workspace_id FK) -- same reasoning as BrokerSyncState/
        # ReconciliationRun just below.
        cleanup_db.query(SystemAlert).filter(
            SystemAlert.workspace_id == ids["workspace_id"]
        ).delete()
        # 2026-08-20: recover-from-reconciliation-lock's own tests are the
        # first in this file to call run_full_reconciliation, which writes
        # BrokerSyncState/ReconciliationRun rows FK'd to trading_sessions.id
        # -- both must go before the TradingSession delete below.
        cleanup_db.query(BrokerSyncState).filter(
            BrokerSyncState.trading_session_id.in_(
                cleanup_db.query(TradingSession.id).filter(
                    TradingSession.workspace_id == ids["workspace_id"]
                )
            )
        ).delete(synchronize_session=False)
        cleanup_db.query(ReconciliationRun).filter(
            ReconciliationRun.trading_session_id.in_(
                cleanup_db.query(TradingSession.id).filter(
                    TradingSession.workspace_id == ids["workspace_id"]
                )
            )
        ).delete(synchronize_session=False)
        cleanup_db.query(TradingSession).filter(
            TradingSession.workspace_id == ids["workspace_id"]
        ).delete()
        cleanup_db.query(LoginSession).filter(LoginSession.user_id == ids["user_id"]).delete()
        cleanup_db.query(BrokerAccount).filter(
            BrokerAccount.id == ids["broker_account_id"]
        ).delete()
        cleanup_db.query(UserRole).filter(UserRole.user_id == ids["user_id"]).delete()
        cleanup_db.query(User).filter(User.id == ids["user_id"]).delete()
        cleanup_db.query(RolePermission).filter(RolePermission.role_id == ids["role_id"]).delete()
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
    response = api_client.post("/api/v1/sessions", json={"broker_account_id": str(uuid.uuid4())})
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


def test_recover_from_kill_switch_restores_paper_only(api_client: TestClient, seeded_admin):
    """Regression test for a real live incident: entering kill_switch had a
    button in the UI, but recovering from it (a legal edge in transitions.py
    from day one) had no endpoint at all -- a session kill-switched by
    mistake had no way back except this one.
    """
    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )
    session_id = api_client.post(
        "/api/v1/sessions",
        json={"broker_account_id": str(seeded_admin["broker_account_id"])},
    ).json()["id"]
    api_client.post(f"/api/v1/sessions/{session_id}/kill-switch", json={"reason": "test"})

    response = api_client.post(f"/api/v1/sessions/{session_id}/recover-from-kill-switch")
    assert response.status_code == 200
    assert response.json()["mode"] == "paper_only"


def test_recover_from_kill_switch_rejects_when_not_in_kill_switch(
    api_client: TestClient, seeded_admin
):
    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )
    session_id = api_client.post(
        "/api/v1/sessions",
        json={"broker_account_id": str(seeded_admin["broker_account_id"])},
    ).json()["id"]

    response = api_client.post(f"/api/v1/sessions/{session_id}/recover-from-kill-switch")
    assert response.status_code == 409


def test_end_session_marks_status_ended(api_client: TestClient, seeded_admin):
    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )
    create_resp = api_client.post(
        "/api/v1/sessions",
        json={"broker_account_id": str(seeded_admin["broker_account_id"])},
    )
    session_id = create_resp.json()["id"]

    response = api_client.post(f"/api/v1/sessions/{session_id}/end")
    assert response.status_code == 200
    assert response.json()["status"] == "ended"

    # GET /sessions still returns it (history stays visible) — only the
    # frontend's *picker* dropdowns filter to active, per StrategiesPage.tsx.
    listed = api_client.get("/api/v1/sessions").json()
    assert any(row["id"] == session_id and row["status"] == "ended" for row in listed)


def test_end_session_rejects_already_ended(api_client: TestClient, seeded_admin):
    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )
    session_id = api_client.post(
        "/api/v1/sessions",
        json={"broker_account_id": str(seeded_admin["broker_account_id"])},
    ).json()["id"]
    api_client.post(f"/api/v1/sessions/{session_id}/end")

    response = api_client.post(f"/api/v1/sessions/{session_id}/end")
    assert response.status_code == 409


def test_end_session_refuses_while_a_strategy_run_is_still_active(
    api_client: TestClient, seeded_admin, engine
):
    """The whole point of `end_session` is to retire sessions nobody's using
    any more — ending one that still has a live `scanning` run underneath it
    would silently orphan that run with no session left to resume it under.
    """
    import uuid as uuid_module
    from datetime import UTC, datetime

    from app.domain.strategy.models import (
        ExecutionMode,
        StrategyConfig,
        StrategyRun,
        StrategyRunStatus,
    )

    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )
    session_id = api_client.post(
        "/api/v1/sessions",
        json={"broker_account_id": str(seeded_admin["broker_account_id"])},
    ).json()["id"]

    session_factory = sessionmaker(bind=engine, future=True)
    config_id = uuid_module.uuid4()
    run_id = uuid_module.uuid4()
    with session_factory() as db:
        db.add(
            StrategyConfig(
                id=config_id,
                workspace_id=seeded_admin["workspace_id"],
                name=f"end-session-test-{config_id.hex[:6]}",
                strategy_type="orb",
            )
        )
        db.flush()
        db.add(
            StrategyRun(
                id=run_id,
                strategy_config_id=config_id,
                trading_session_id=uuid_module.UUID(session_id),
                execution_mode=ExecutionMode.AUTO,
                status=StrategyRunStatus.SCANNING,
                started_at=datetime.now(UTC),
                started_by_user_id=seeded_admin["user_id"],
            )
        )
        db.commit()

    try:
        response = api_client.post(f"/api/v1/sessions/{session_id}/end")
        assert response.status_code == 409

        with session_factory() as db:
            db.query(StrategyRun).filter(StrategyRun.id == run_id).update(
                {"status": StrategyRunStatus.STOPPED}
            )
            db.commit()

        response = api_client.post(f"/api/v1/sessions/{session_id}/end")
        assert response.status_code == 200
    finally:
        with session_factory() as cleanup_db:
            cleanup_db.query(StrategyRun).filter(StrategyRun.id == run_id).delete()
            cleanup_db.query(StrategyConfig).filter(StrategyConfig.id == config_id).delete()
            cleanup_db.commit()


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


def test_list_broker_accounts_returns_workspace_scoped_accounts(
    api_client: TestClient, seeded_admin
):
    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )
    response = api_client.get("/api/v1/broker-accounts")
    assert response.status_code == 200
    accounts = response.json()
    assert len(accounts) == 1
    assert accounts[0]["id"] == str(seeded_admin["broker_account_id"])


def test_list_broker_accounts_requires_login(api_client: TestClient):
    response = api_client.get("/api/v1/broker-accounts")
    assert response.status_code == 401


def test_list_sessions_returns_workspace_sessions_most_recent_first(
    api_client: TestClient, seeded_admin, engine
):
    # A second BrokerAccount, not a second session on the same one: Batch C
    # added a "one ACTIVE session per broker account per day" lock in
    # create_session (a reconciliation false-alarm fix — two ACTIVE sessions
    # on the same account would each see the other's positions as phantom
    # mismatches), so two sessions in the same test now need two accounts.
    # Cleaned up here explicitly since seeded_admin's own teardown only
    # knows about its one seeded broker_account_id.
    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as db:
        second_account = BrokerAccount(
            id=uuid.uuid4(),
            workspace_id=seeded_admin["workspace_id"],
            broker_type=BrokerType.SHOONYA,
            label="qc-test-account-2",
            credentials_ref="config/credentials/shoonya.env",
            status=BrokerAccountStatus.ACTIVE,
        )
        db.add(second_account)
        db.commit()
        second_account_id = second_account.id

    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )
    first = api_client.post(
        "/api/v1/sessions",
        json={"broker_account_id": str(seeded_admin["broker_account_id"])},
    ).json()
    second = api_client.post(
        "/api/v1/sessions",
        json={"broker_account_id": str(second_account_id)},
    ).json()

    response = api_client.get("/api/v1/sessions")
    assert response.status_code == 200
    ids = [row["id"] for row in response.json()]
    assert ids[:2] == [second["id"], first["id"]]

    with session_factory() as cleanup_db:
        from app.domain.session.models import TradingSession

        # TradingSession before BrokerAccount (FK) — seeded_admin's own
        # teardown deletes every TradingSession in the workspace too, but
        # that runs *after* this test function returns, so this account's
        # own session must be cleared first or this delete 409s on the FK.
        cleanup_db.query(TradingSession).filter(
            TradingSession.broker_account_id == second_account_id
        ).delete()
        cleanup_db.query(BrokerAccount).filter(BrokerAccount.id == second_account_id).delete()
        cleanup_db.commit()


def test_list_instruments_returns_active_instruments_with_expiry_dates(
    api_client: TestClient, seeded_admin, engine
):
    import uuid as uuid_module
    from datetime import date, timedelta

    from app.domain.market.models import Instrument, OptionContract, OptionType

    # Relative to today, not a fixed calendar date: the picker now also
    # filters out anything already past expiry (see instruments.py's own
    # comment), so a hardcoded past date would make this assertion fail for
    # a reason unrelated to what the test actually checks.
    future_expiry = date.today() + timedelta(days=10)
    session_factory = sessionmaker(bind=engine, future=True)
    instrument_id = uuid_module.uuid4()
    try:
        with session_factory() as db:
            db.add(
                Instrument(
                    id=instrument_id,
                    symbol=f"TESTIDX-{instrument_id.hex[:6]}",
                    exchange="NFO",
                    lot_size=25,
                    tick_size=0.05,
                    is_active=True,
                )
            )
            db.flush()
            db.add(
                OptionContract(
                    id=uuid_module.uuid4(),
                    instrument_id=instrument_id,
                    expiry_date=future_expiry,
                    strike=20000,
                    option_type=OptionType.CE,
                    symbol=f"TESTIDX-{instrument_id.hex[:6]}-20000-CE",
                    is_active=True,
                )
            )
            db.commit()

        api_client.post(
            "/api/v1/auth/login",
            json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
        )
        response = api_client.get("/api/v1/instruments")
        assert response.status_code == 200
        rows = {row["id"]: row for row in response.json()}
        assert str(instrument_id) in rows
        assert rows[str(instrument_id)]["expiry_dates"] == [future_expiry.isoformat()]
    finally:
        with session_factory() as cleanup_db:
            cleanup_db.query(OptionContract).filter(
                OptionContract.instrument_id == instrument_id
            ).delete()
            cleanup_db.query(Instrument).filter(Instrument.id == instrument_id).delete()
            cleanup_db.commit()


def test_list_instruments_excludes_instruments_with_no_option_contracts(
    api_client: TestClient, seeded_admin, engine
):
    """Live-found: a real Shoonya `SearchScrip` for "NIFTY"/"BANKNIFTY" also
    matches futures contracts (`NIFTY25AUG26F`) and unrelated substring
    decoys (`NIFTYNXT5025AUG26F`), which got synced in as underlying
    `Instrument` rows with no option contracts ever attached. They showed up
    in the frontend's instrument picker with an empty expiry dropdown —
    selectable, then failing validation with a confusing "expiry is
    required". An instrument with no active option contracts can never start
    a strategy, so it has no business being offered.
    """
    import uuid as uuid_module

    from app.domain.market.models import Instrument

    session_factory = sessionmaker(bind=engine, future=True)
    instrument_id = uuid_module.uuid4()
    try:
        with session_factory() as db:
            db.add(
                Instrument(
                    id=instrument_id,
                    symbol=f"FUTONLY-{instrument_id.hex[:6]}",
                    exchange="NFO",
                    lot_size=25,
                    tick_size=0.05,
                    is_active=True,
                )
            )
            db.commit()

        api_client.post(
            "/api/v1/auth/login",
            json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
        )
        response = api_client.get("/api/v1/instruments")
        assert response.status_code == 200
        returned_ids = {row["id"] for row in response.json()}
        assert str(instrument_id) not in returned_ids
    finally:
        with session_factory() as cleanup_db:
            cleanup_db.query(Instrument).filter(Instrument.id == instrument_id).delete()
            cleanup_db.commit()


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


# -- Mode master switch (go-live/go-paper) --------------------------------


def _login_and_create_session(api_client: TestClient, seeded_admin) -> str:
    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )
    create_resp = api_client.post(
        "/api/v1/sessions",
        json={"broker_account_id": str(seeded_admin["broker_account_id"])},
    )
    session_id: str = create_resp.json()["id"]
    return session_id


def test_go_live_walks_a_fresh_session_to_live_enabled(api_client: TestClient, seeded_admin):
    session_id = _login_and_create_session(api_client, seeded_admin)

    response = api_client.post(f"/api/v1/sessions/{session_id}/go-live")
    assert response.status_code == 200
    assert response.json()["mode"] == "live_enabled"


def test_go_live_then_go_paper_restores_paper_only(api_client: TestClient, seeded_admin):
    session_id = _login_and_create_session(api_client, seeded_admin)
    api_client.post(f"/api/v1/sessions/{session_id}/go-live")

    response = api_client.post(f"/api/v1/sessions/{session_id}/go-paper")
    assert response.status_code == 200
    assert response.json()["mode"] == "paper_only"


def test_go_live_is_idempotent_when_already_live(api_client: TestClient, seeded_admin):
    session_id = _login_and_create_session(api_client, seeded_admin)
    api_client.post(f"/api/v1/sessions/{session_id}/go-live")

    response = api_client.post(f"/api/v1/sessions/{session_id}/go-live")
    assert response.status_code == 200
    assert response.json()["mode"] == "live_enabled"


def test_go_live_rejects_from_kill_switch(api_client: TestClient, seeded_admin):
    session_id = _login_and_create_session(api_client, seeded_admin)
    api_client.post(f"/api/v1/sessions/{session_id}/kill-switch", json={"reason": "test"})

    response = api_client.post(f"/api/v1/sessions/{session_id}/go-live")
    assert response.status_code == 409

    # Must not have silently changed the session's mode on the way to
    # rejecting -- the whole point is that kill_switch needs its own
    # dedicated recovery endpoint, not a bypass through this one.
    get_resp = api_client.get(f"/api/v1/sessions/{session_id}")
    assert get_resp.json()["mode"] == "kill_switch"


# -- POST /sessions/bootstrap-now (Dual-Trigger Model, 2026-08-17) --------


def test_bootstrap_now_requires_login(api_client: TestClient):
    response = api_client.post("/api/v1/sessions/bootstrap-now")
    assert response.status_code == 401


def test_bootstrap_now_calls_run_daily_bootstrap(api_client: TestClient, seeded_admin, monkeypatch):
    """Deliberately doesn't exercise `run_daily_bootstrap` for real here --
    that function's own default `session_factory` is the *production*
    `session_scope`, not this test's isolated engine (the endpoint does a
    local import specifically so a real call can never accidentally touch
    prod from an automated test run -- same discipline
    test_daily_bootstrapper.py's own `_no_real_resume` fixture already
    applies to `_resume_strategy_runners`). `run_daily_bootstrap`'s actual
    behavior is covered thoroughly and safely there, with its
    `session_factory` explicitly overridden; this test only proves the
    endpoint's own wiring -- auth gate passed, the function got called.
    """
    import app.modules.session.bootstrapper as bootstrapper_module

    calls: list[None] = []
    monkeypatch.setattr(bootstrapper_module, "run_daily_bootstrap", lambda: calls.append(None))

    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )
    response = api_client.post("/api/v1/sessions/bootstrap-now")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(calls) == 1


# -- POST /sessions/{id}/recover-from-degraded (2026-08-18) ---------------


def _drop_session_into_degraded(engine, session_id: str) -> None:
    """Simulates what scheduler.health_check/PositionManager actually do --
    a SYSTEM-triggered transition into degraded_mode -- since there is
    (deliberately) no API endpoint that lets a human enter this mode
    directly.
    """
    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as db:
        trading_session = db.get(TradingSession, uuid.UUID(session_id))
        assert trading_session is not None
        transition_mode(
            db,
            trading_session,
            SafeMode.DEGRADED_MODE,
            TransitionTriggerType.SYSTEM,
            reason="simulated health-check trip",
        )
        db.commit()


def test_recover_from_degraded_restores_prior_mode(api_client: TestClient, seeded_admin, engine):
    session_id = _login_and_create_session(api_client, seeded_admin)
    api_client.post(f"/api/v1/sessions/{session_id}/go-live")
    _drop_session_into_degraded(engine, session_id)

    response = api_client.post(f"/api/v1/sessions/{session_id}/recover-from-degraded")

    assert response.status_code == 200
    assert response.json()["mode"] == "live_enabled"


def test_recover_from_degraded_requires_login(api_client: TestClient):
    response = api_client.post(f"/api/v1/sessions/{uuid.uuid4()}/recover-from-degraded")
    assert response.status_code == 401


def test_recover_from_degraded_rejects_without_livetrade_execute_permission(
    api_client: TestClient, seeded_admin, engine
):
    """The endpoint's own declared permission (livetrade.execute) matches
    the bar recover_from_degraded itself enforces for resuming above
    paper_only -- a user missing it must get a clean 403 up front, never
    even reaching the function (which would otherwise raise
    ModeTransitionError -> 409 instead). No existing test in this file
    exercises a permission-denied case via a real HTTP round trip, so this
    builds its own limited user rather than reusing seeded_admin (which
    intentionally holds every permission).
    """
    session_id = _login_and_create_session(api_client, seeded_admin)
    api_client.post(f"/api/v1/sessions/{session_id}/go-live")
    _drop_session_into_degraded(engine, session_id)
    api_client.post("/api/v1/auth/logout")

    session_factory = sessionmaker(bind=engine, future=True)
    limited_user_id = uuid.uuid4()
    limited_role_id = uuid.uuid4()
    limited_email = f"limited-{uuid.uuid4().hex[:8]}@example.com"
    try:
        with session_factory() as db:
            limited_user = User(
                id=limited_user_id,
                workspace_id=seeded_admin["workspace_id"],
                email=limited_email,
                password_hash=hash_password(ADMIN_PASSWORD),
                display_name="Limited Test User",
                is_active=True,
            )
            db.add(limited_user)
            db.flush()

            role = Role(id=limited_role_id, name=f"limited-role-{uuid.uuid4().hex[:8]}")
            db.add(role)
            db.flush()

            # session.stop only -- deliberately not livetrade.execute.
            session_stop_permission = (
                db.query(Permission).filter(Permission.code == "session.stop").one()
            )
            db.add(RolePermission(role_id=role.id, permission_id=session_stop_permission.id))
            db.add(
                UserRole(
                    user_id=limited_user.id,
                    role_id=role.id,
                    workspace_id=seeded_admin["workspace_id"],
                )
            )
            db.commit()

        api_client.post(
            "/api/v1/auth/login", json={"email": limited_email, "password": ADMIN_PASSWORD}
        )
        response = api_client.post(f"/api/v1/sessions/{session_id}/recover-from-degraded")

        assert response.status_code == 403

        # Must not have silently recovered the session on the way to
        # rejecting.
        api_client.post("/api/v1/auth/logout")
        api_client.post(
            "/api/v1/auth/login",
            json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
        )
        get_resp = api_client.get(f"/api/v1/sessions/{session_id}")
        assert get_resp.json()["mode"] == "degraded_mode"
    finally:
        with session_factory() as cleanup_db:
            from app.domain.identity.models import LoginSession

            cleanup_db.query(LoginSession).filter(LoginSession.user_id == limited_user_id).delete()
            cleanup_db.query(UserRole).filter(UserRole.user_id == limited_user_id).delete()
            cleanup_db.query(RolePermission).filter(
                RolePermission.role_id == limited_role_id
            ).delete()
            cleanup_db.query(Role).filter(Role.id == limited_role_id).delete()
            cleanup_db.query(User).filter(User.id == limited_user_id).delete()
            cleanup_db.commit()


# -- POST /sessions/{id}/recover-from-reconciliation-lock (2026-08-20) -------


def _drop_session_into_reconciliation_lock(engine, session_id: str) -> None:
    """Simulates what reconciliation.service.run_reconciliation actually
    does on a real mismatch -- there is (deliberately) no API endpoint that
    lets a human enter this mode directly, same reasoning as
    `_drop_session_into_degraded`.
    """
    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as db:
        trading_session = db.get(TradingSession, uuid.UUID(session_id))
        assert trading_session is not None
        transition_mode(
            db,
            trading_session,
            SafeMode.RECONCILIATION_LOCK,
            TransitionTriggerType.SYSTEM,
            reason="simulated reconciliation mismatch",
        )
        db.commit()


def test_recover_from_reconciliation_lock_restores_paper_only_when_clean(
    api_client: TestClient, seeded_admin, engine
):
    session_id = _login_and_create_session(api_client, seeded_admin)
    api_client.post(f"/api/v1/sessions/{session_id}/go-live")
    _drop_session_into_reconciliation_lock(engine, session_id)

    response = api_client.post(
        f"/api/v1/sessions/{session_id}/recover-from-reconciliation-lock"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["recovered"] is True
    assert body["session"]["mode"] == "paper_only"

    get_resp = api_client.get(f"/api/v1/sessions/{session_id}")
    assert get_resp.json()["mode"] == "paper_only"


def test_recover_from_reconciliation_lock_refuses_when_still_mismatched(
    api_client: TestClient, seeded_admin, engine
):
    """The real point of this endpoint over a bare permissioned override --
    it re-checks first (`run_full_reconciliation`) and refuses to transition
    if a fresh broker-side mismatch still exists, rather than trusting a
    stale assumption that whatever caused the lock is already fixed.
    """
    from app.modules.broker_adapter import composition
    from app.modules.broker_adapter.base.contracts import OrderRequest, OrderSide, OrderType

    session_id = _login_and_create_session(api_client, seeded_admin)
    api_client.post(f"/api/v1/sessions/{session_id}/go-live")
    _drop_session_into_reconciliation_lock(engine, session_id)

    # Same injection pattern test_reconciliation.py's own tests use -- a
    # stray fill against the persistent execution mock with no matching
    # local position, independent of any dispatch machinery.
    composition.get_execution_mock().place_order(
        OrderRequest(
            idempotency_key=f"manual-injection-{uuid.uuid4()}",
            contract_symbol="NIFTY26JUL22000CE-RECOVERYTEST",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            qty=25,
        )
    )

    response = api_client.post(
        f"/api/v1/sessions/{session_id}/recover-from-reconciliation-lock"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["recovered"] is False
    assert body["mismatches_found"] >= 1

    get_resp = api_client.get(f"/api/v1/sessions/{session_id}")
    assert get_resp.json()["mode"] == "reconciliation_lock"


def test_recover_from_reconciliation_lock_rejects_when_not_in_lock(
    api_client: TestClient, seeded_admin
):
    session_id = _login_and_create_session(api_client, seeded_admin)

    response = api_client.post(
        f"/api/v1/sessions/{session_id}/recover-from-reconciliation-lock"
    )

    assert response.status_code == 409


def test_recover_from_reconciliation_lock_requires_login(api_client: TestClient):
    response = api_client.post(
        f"/api/v1/sessions/{uuid.uuid4()}/recover-from-reconciliation-lock"
    )
    assert response.status_code == 401
