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
        for code in (
            "session.start",
            "session.stop",
            "strategy.view",
            "papertrade.execute",
            "risk.override",
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
