"""API-level tests for strategy lifecycle + trade-approval endpoints.
`SyntheticStrategyRunner` is monkeypatched to a no-op stand-in for these
tests — the real runner spawns a background thread that talks to the
*production* DB via the default `session_scope` (see its own docstring), not
the isolated test database these tests use, so letting a real one start
during a test would silently query the wrong database from an untracked
thread. The runner's actual scan-cycle behavior is covered directly (no HTTP,
no threading) in test_synthetic_strategy.py; this file only exercises the
route wiring: permissions, request validation, and StrategyRun bookkeeping.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import app.api.v1.strategies as strategies_module
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
from app.domain.market.models import Instrument, OptionContract, OptionType
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import (
    ExecutionMode,
    PendingTradeApproval,
    Signal,
    SignalSide,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    SyntheticTradeOutcome,
    TradeIntent,
    TradeIntentStatus,
)
from app.main import app
from app.modules.strategy_engine.interface import TradeProposal
from app.modules.strategy_engine.service import submit_signal

ADMIN_PASSWORD = "correct horse battery staple 123!"


class _FakeRunner:
    """Records start/stop calls; never spawns a thread or touches any DB."""

    instances: list[_FakeRunner] = []

    def __init__(self, strategy, strategy_run_id, interval_seconds=30.0, **kwargs):
        self.strategy = strategy
        self.strategy_run_id = strategy_run_id
        self.started = False
        self.stopped = False
        _FakeRunner.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture(autouse=True)
def fake_runner(monkeypatch):
    _FakeRunner.instances.clear()
    monkeypatch.setattr(strategies_module, "SyntheticStrategyRunner", _FakeRunner)
    yield
    strategies_module._RUNNERS.clear()


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
        workspace = Workspace(id=uuid.uuid4(), name=f"strat-test-{uuid.uuid4().hex[:8]}")
        db.add(workspace)
        db.flush()

        role = Role(id=uuid.uuid4(), name=f"strat-admin-{uuid.uuid4().hex[:8]}")
        db.add(role)
        db.flush()
        permission_ids: list[uuid.UUID] = []
        for code in ("session.start", "strategy.edit", "papertrade.execute"):
            permission = Permission(id=uuid.uuid4(), code=code, description="")
            db.add(permission)
            db.flush()
            permission_ids.append(permission.id)
            db.add(RolePermission(role_id=role.id, permission_id=permission.id))

        user = User(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            email=f"strat-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password(ADMIN_PASSWORD),
            display_name="Strategy Test Admin",
            is_active=True,
        )
        db.add(user)
        db.flush()
        db.add(UserRole(user_id=user.id, role_id=role.id, workspace_id=workspace.id))

        broker_account = BrokerAccount(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            broker_type=BrokerType.SHOONYA,
            label="strat-test-account",
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

    with session_factory() as cleanup_db:
        from app.domain.audit.models import AuditEvent
        from app.domain.identity.models import LoginSession
        from app.domain.ops.models import SystemAlert
        from app.domain.risk.models import RiskDecision, RiskLimitConfig

        # Phase 2 additions: a test may have driven a full Signal ->
        # TradeIntent -> RiskDecision -> (PendingTradeApproval |
        # SyntheticTradeOutcome) chain through this workspace, none of which
        # the original cleanup below knew about — delete leaf-first, same
        # FK-safe-order reasoning as the rest of this fixture.
        trade_intent_ids = cleanup_db.query(TradeIntent.id).filter(
            TradeIntent.workspace_id == ids["workspace_id"]
        )
        cleanup_db.query(SyntheticTradeOutcome).filter(
            SyntheticTradeOutcome.trade_intent_id.in_(trade_intent_ids)
        ).delete(synchronize_session=False)
        cleanup_db.query(PendingTradeApproval).filter(
            PendingTradeApproval.trade_intent_id.in_(trade_intent_ids)
        ).delete(synchronize_session=False)
        cleanup_db.query(RiskDecision).filter(
            RiskDecision.trade_intent_id.in_(trade_intent_ids)
        ).delete(synchronize_session=False)
        cleanup_db.query(TradeIntent).filter(
            TradeIntent.workspace_id == ids["workspace_id"]
        ).delete()
        cleanup_db.query(Signal).filter(Signal.workspace_id == ids["workspace_id"]).delete()
        cleanup_db.query(SystemAlert).filter(
            SystemAlert.workspace_id == ids["workspace_id"]
        ).delete()
        cleanup_db.query(RiskLimitConfig).filter(
            RiskLimitConfig.workspace_id == ids["workspace_id"]
        ).delete()

        cleanup_db.query(AuditEvent).filter(
            AuditEvent.workspace_id == ids["workspace_id"]
        ).delete()
        cleanup_db.query(StrategyRun).filter(
            StrategyRun.strategy_config_id.in_(
                cleanup_db.query(StrategyConfig.id).filter(
                    StrategyConfig.workspace_id == ids["workspace_id"]
                )
            )
        ).delete(synchronize_session=False)
        cleanup_db.query(StrategyConfig).filter(
            StrategyConfig.workspace_id == ids["workspace_id"]
        ).delete()
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


def _login(api_client: TestClient, seeded_admin) -> None:
    api_client.post(
        "/api/v1/auth/login",
        json={"email": seeded_admin["email"], "password": ADMIN_PASSWORD},
    )


def test_create_strategy_requires_login(api_client: TestClient):
    response = api_client.post("/api/v1/strategies", json={"name": "orb"})
    assert response.status_code == 401


def test_create_strategy_then_duplicate_name_conflicts(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    first = api_client.post("/api/v1/strategies", json={"name": "orb"})
    assert first.status_code == 200

    second = api_client.post("/api/v1/strategies", json={"name": "orb"})
    assert second.status_code == 409


def test_start_strategy_creates_run_and_stop_ends_it(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    strategy_id = api_client.post("/api/v1/strategies", json={"name": "orb"}).json()["id"]
    session_id = api_client.post(
        "/api/v1/sessions", json={"broker_account_id": str(seeded_admin["broker_account_id"])}
    ).json()["id"]

    start_resp = api_client.post(
        f"/api/v1/strategies/{strategy_id}/start",
        json={
            "trading_session_id": session_id,
            "instrument_id": str(uuid.uuid4()),
            "expiry_date": date(2026, 7, 30).isoformat(),
            "execution_mode": "auto",
        },
    )
    assert start_resp.status_code == 200
    assert start_resp.json()["status"] == "scanning"
    assert len(_FakeRunner.instances) == 1
    assert _FakeRunner.instances[0].started is True

    # A second start while one is active must be rejected, not silently
    # spawn a second concurrent runner for the same strategy.
    second_start = api_client.post(
        f"/api/v1/strategies/{strategy_id}/start",
        json={
            "trading_session_id": session_id,
            "instrument_id": str(uuid.uuid4()),
            "expiry_date": date(2026, 7, 30).isoformat(),
        },
    )
    assert second_start.status_code == 409

    stop_resp = api_client.post(f"/api/v1/strategies/{strategy_id}/stop")
    assert stop_resp.status_code == 200
    assert _FakeRunner.instances[0].stopped is True

    # Stopping again with nothing active is a clean 404, not a 500.
    stop_again = api_client.post(f"/api/v1/strategies/{strategy_id}/stop")
    assert stop_again.status_code == 404


def test_approve_unknown_trade_approval_is_404(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    response = api_client.post(f"/api/v1/trade-approvals/{uuid.uuid4()}/approve")
    assert response.status_code == 404


def test_approving_a_pending_trade_synthetically_closes_it(
    api_client: TestClient, seeded_admin, engine
):
    """Regression test: approving a trade must not leave it DISPATCHED
    forever — that would permanently occupy a concurrency slot and a
    same-strike lock for the rest of the session, since Phase 2 has no real
    Execution Service to ever close it otherwise.
    """
    _login(api_client, seeded_admin)
    session_id = api_client.post(
        "/api/v1/sessions", json={"broker_account_id": str(seeded_admin["broker_account_id"])}
    ).json()["id"]

    session_factory = sessionmaker(bind=engine, future=True)

    # Instrument/OptionContract have no workspace scoping (they're shared
    # exchange-wide data, not per-workspace), so seeded_admin's teardown
    # can't clean them up generically — this test creates and removes its
    # own, in a try/finally so a failed assertion still cleans up rather
    # than leaking rows into the shared test engine for later tests (this is
    # exactly the isolation trap CLAUDE.md's "Test DB isolation" convention
    # warns about).
    instrument_id = uuid.uuid4()
    trade_intent_id: uuid.UUID | None = None
    try:
        with session_factory() as db:
            instrument = Instrument(
                id=instrument_id, symbol="NIFTY-APPR", exchange="NFO", lot_size=25, tick_size=0.05
            )
            db.add(instrument)
            db.flush()
            option_contract = OptionContract(
                id=uuid.uuid4(),
                instrument_id=instrument.id,
                expiry_date=date(2026, 7, 30),
                strike=22000,
                option_type=OptionType.CE,
                symbol="NIFTY-APPR-26JUL22000CE",
            )
            db.add(option_contract)
            db.flush()

            trading_session = db.get(TradingSession, uuid.UUID(session_id))
            assert trading_session is not None
            trading_session.mode = SafeMode.PAPER_ONLY
            trading_session.funding_mode = FundingMode.CASH

            strategy_config = StrategyConfig(
                id=uuid.uuid4(),
                workspace_id=seeded_admin["workspace_id"],
                name="approval-flow-test",
            )
            db.add(strategy_config)
            db.flush()

            strategy_run = StrategyRun(
                id=uuid.uuid4(),
                strategy_config_id=strategy_config.id,
                trading_session_id=trading_session.id,
                execution_mode=ExecutionMode.APPROVAL_REQUIRED,
                status=StrategyRunStatus.SCANNING,
                started_at=datetime.now(UTC),
                started_by_user_id=seeded_admin["user_id"],
            )
            db.add(strategy_run)
            db.flush()

            proposal = TradeProposal(
                option_contract_id=option_contract.id,
                side=SignalSide.BUY,
                qty_lots=1,
                entry_price=80.0,
                stop_price=72.0,
                target_price=92.0,
            )
            decision = submit_signal(db, strategy_run, trading_session, strategy_config, proposal)
            assert decision.decision == "approved"
            db.commit()

            approval_id = str(
                db.query(PendingTradeApproval)
                .filter_by(trade_intent_id=decision.trade_intent_id)
                .one()
                .id
            )
            trade_intent_id = decision.trade_intent_id

        approve_resp = api_client.post(f"/api/v1/trade-approvals/{approval_id}/approve")
        assert approve_resp.status_code == 200
        assert approve_resp.json()["trade_intent_status"] == TradeIntentStatus.DISPATCHED

        with session_factory() as verify_db:
            outcome = (
                verify_db.query(SyntheticTradeOutcome)
                .filter(SyntheticTradeOutcome.trade_intent_id == trade_intent_id)
                .one_or_none()
            )
            assert outcome is not None, (
                "approved intent must be synthetically closed, not left open"
            )
    finally:
        # Runs before seeded_admin's own (workspace-scoped) teardown, so the
        # trade_intent/signal chain this test created still exists at this
        # point — must be cleared here too, in FK-safe order, before
        # OptionContract/Instrument can be deleted.
        with session_factory() as cleanup_db:
            from app.domain.risk.models import RiskDecision

            if trade_intent_id is not None:
                cleanup_db.query(SyntheticTradeOutcome).filter(
                    SyntheticTradeOutcome.trade_intent_id == trade_intent_id
                ).delete()
                cleanup_db.query(PendingTradeApproval).filter(
                    PendingTradeApproval.trade_intent_id == trade_intent_id
                ).delete()
                cleanup_db.query(RiskDecision).filter(
                    RiskDecision.trade_intent_id == trade_intent_id
                ).delete()
                cleanup_db.query(TradeIntent).filter(TradeIntent.id == trade_intent_id).delete()
            cleanup_db.query(Signal).filter(
                Signal.option_contract_id.in_(
                    cleanup_db.query(OptionContract.id).filter(
                        OptionContract.instrument_id == instrument_id
                    )
                )
            ).delete(synchronize_session=False)
            cleanup_db.query(OptionContract).filter(
                OptionContract.instrument_id == instrument_id
            ).delete()
            cleanup_db.query(Instrument).filter(Instrument.id == instrument_id).delete()
            cleanup_db.commit()
