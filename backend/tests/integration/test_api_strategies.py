"""API-level tests for strategy lifecycle + trade-approval endpoints.
`StrategyRunner` is monkeypatched to a no-op stand-in for these
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
from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import app.api.v1.strategies as strategies_module
from app.core.db.session import get_db
from app.core.security.passwords import hash_password
from app.domain.execution.models import Position, PositionStatus
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
    """Also patches `ensure_position_manager_running`, `ensure_ingestion_running`,
    and `record_option_chain_snapshot` to no-op recorders — all three default
    to `session_scope`-bound production-DB access (background threads for the
    first two, a direct call for the third), which would otherwise touch the
    *production* DB from this isolated-test-engine request, the exact trap
    `_FakeRunner` above already exists to avoid for `StrategyRunner`.
    """
    _FakeRunner.instances.clear()
    monkeypatch.setattr(strategies_module, "StrategyRunner", _FakeRunner)

    position_manager_calls: list[uuid.UUID] = []
    monkeypatch.setattr(
        strategies_module,
        "ensure_position_manager_running",
        lambda trading_session_id: position_manager_calls.append(trading_session_id),
    )
    monkeypatch.setattr(
        strategies_module,
        "ensure_ingestion_running",
        lambda symbol, broker=None: None,
    )
    monkeypatch.setattr(
        strategies_module,
        "record_option_chain_snapshot",
        lambda instrument_id, broker, symbol, expiry, session_factory=None: None,
    )
    yield position_manager_calls
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
        for code in ("session.start", "strategy.edit", "strategy.view", "papertrade.execute"):
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
        from sqlalchemy import or_ as sa_or

        from app.domain.audit.models import AuditEvent
        from app.domain.broker.models import BrokerSyncState, ReconciliationRun
        from app.domain.execution.models import Order, OrderEvent, StopPlan, TradeOutcome, TrailPlan
        from app.domain.identity.models import LoginSession
        from app.domain.ops.models import SystemAlert
        from app.domain.risk.models import RiskDecision, RiskLimitConfig

        # Phase 2+3 additions: a test may have driven a full Signal ->
        # TradeIntent -> RiskDecision -> (PendingTradeApproval | real
        # Order/Position/.../TradeOutcome) chain through this workspace,
        # none of which the original cleanup below knew about — delete
        # leaf-first, same FK-safe-order reasoning as the rest of this
        # fixture.
        trade_intent_ids = cleanup_db.query(TradeIntent.id).filter(
            TradeIntent.workspace_id == ids["workspace_id"]
        )
        position_ids = cleanup_db.query(Position.id).filter(
            Position.trade_intent_id.in_(trade_intent_ids)
        )
        order_ids = [
            row[0]
            for row in cleanup_db.query(Order.id).filter(
                sa_or(
                    Order.trade_intent_id.in_(trade_intent_ids),
                    Order.position_id.in_(position_ids),
                )
            )
        ]
        cleanup_db.query(OrderEvent).filter(OrderEvent.order_id.in_(order_ids)).delete(
            synchronize_session=False
        )
        cleanup_db.query(TradeOutcome).filter(
            TradeOutcome.position_id.in_(position_ids)
        ).delete(synchronize_session=False)
        cleanup_db.query(StopPlan).filter(StopPlan.position_id.in_(position_ids)).delete(
            synchronize_session=False
        )
        cleanup_db.query(TrailPlan).filter(TrailPlan.position_id.in_(position_ids)).delete(
            synchronize_session=False
        )
        # orders <-> positions is a circular FK pair (see
        # app/domain/execution/models.py). Break it via
        # positions.closing_order_id, which is nullable — NOT via
        # orders.position_id, which would leave an exit order (always
        # trade_intent_id=NULL) with both FK columns null, violating
        # ck_order_exactly_one_of_intent_or_position.
        cleanup_db.query(Position).filter(Position.id.in_(position_ids)).update(
            {"closing_order_id": None}, synchronize_session=False
        )
        # Exit orders (position_id set) are now unreferenced — safe to
        # delete before the positions they point at.
        cleanup_db.query(Order).filter(Order.position_id.in_(position_ids)).delete(
            synchronize_session=False
        )
        cleanup_db.query(Position).filter(
            Position.trade_intent_id.in_(trade_intent_ids)
        ).delete(synchronize_session=False)
        # Entry orders (trade_intent_id set) are now unreferenced.
        cleanup_db.query(Order).filter(Order.trade_intent_id.in_(trade_intent_ids)).delete(
            synchronize_session=False
        )
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
        # Phase 3: dispatch_trade_intent/close_position each run an
        # event-triggered reconciliation pass, which writes these — not
        # accounted for before that existed.
        cleanup_db.query(BrokerSyncState).filter(
            BrokerSyncState.workspace_id == ids["workspace_id"]
        ).delete()
        cleanup_db.query(ReconciliationRun).filter(
            ReconciliationRun.workspace_id == ids["workspace_id"]
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


def test_list_strategies_returns_workspace_scoped_configs(
    api_client: TestClient, seeded_admin
):
    _login(api_client, seeded_admin)
    api_client.post("/api/v1/strategies", json={"name": "orb-list-test", "strategy_type": "orb"})

    response = api_client.get("/api/v1/strategies")
    assert response.status_code == 200
    names = [row["name"] for row in response.json()]
    assert "orb-list-test" in names


def test_create_strategy_then_duplicate_name_conflicts(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    first = api_client.post("/api/v1/strategies", json={"name": "orb"})
    assert first.status_code == 200

    second = api_client.post("/api/v1/strategies", json={"name": "orb"})
    assert second.status_code == 409


def test_start_strategy_creates_run_and_stop_ends_it(
    api_client: TestClient, seeded_admin, fake_runner, engine
):
    _login(api_client, seeded_admin)
    strategy_id = api_client.post("/api/v1/strategies", json={"name": "orb"}).json()["id"]
    session_id = api_client.post(
        "/api/v1/sessions", json={"broker_account_id": str(seeded_admin["broker_account_id"])}
    ).json()["id"]

    # Instrument/OptionContract have no workspace scoping (shared exchange-
    # wide data), so seeded_admin's teardown can't clean this up generically
    # — created and removed directly, in a try/finally so a failed assertion
    # still cleans up (same pattern the approval test below uses).
    instrument_id = uuid.uuid4()
    session_factory = sessionmaker(bind=engine, future=True)
    try:
        with session_factory() as db:
            db.add(
                Instrument(
                    id=instrument_id,
                    symbol="NIFTY-START",
                    exchange="NFO",
                    lot_size=25,
                    tick_size=0.05,
                )
            )
            db.commit()

        start_resp = api_client.post(
            f"/api/v1/strategies/{strategy_id}/start",
            json={
                "trading_session_id": session_id,
                "instrument_id": str(instrument_id),
                "expiry_date": date(2026, 7, 30).isoformat(),
                "execution_mode": "auto",
            },
        )
        assert start_resp.status_code == 200
        assert start_resp.json()["status"] == "scanning"
        assert len(_FakeRunner.instances) == 1
        assert _FakeRunner.instances[0].started is True
        assert fake_runner == [uuid.UUID(session_id)]

        # A second start while one is active must be rejected, not silently
        # spawn a second concurrent runner for the same strategy.
        second_start = api_client.post(
            f"/api/v1/strategies/{strategy_id}/start",
            json={
                "trading_session_id": session_id,
                "instrument_id": str(instrument_id),
                "expiry_date": date(2026, 7, 30).isoformat(),
            },
        )
        assert second_start.status_code == 409

        stop_resp = api_client.post(f"/api/v1/strategies/{strategy_id}/stop")
        assert stop_resp.status_code == 200
        assert _FakeRunner.instances[0].stopped is True
    finally:
        with session_factory() as cleanup_db:
            # strategy_runs.instrument_id now FKs to instruments.id (see that
            # column's own docstring) — must clear referencing rows first,
            # same FK-safe-order requirement as every other direct-DB test
            # cleanup in this codebase.
            cleanup_db.query(StrategyRun).filter(
                StrategyRun.instrument_id == instrument_id
            ).update({StrategyRun.instrument_id: None})
            cleanup_db.query(Instrument).filter(Instrument.id == instrument_id).delete()
            cleanup_db.commit()

    # Stopping again with nothing active is a clean 404, not a 500.
    stop_again = api_client.post(f"/api/v1/strategies/{strategy_id}/stop")
    assert stop_again.status_code == 404


def test_start_strategy_leaves_no_zombie_run_when_option_chain_fetch_fails(
    api_client: TestClient, seeded_admin, fake_runner, engine, monkeypatch
):
    """Live-found bug: `record_option_chain_snapshot` used to run *after*
    the `StrategyRun` row was already committed with status SCANNING — a
    broker failure there (e.g. a requested expiry that doesn't exist for
    this underlying) left a "zombie" run visible in `GET /strategies/running`
    with a working Stop button, even though nothing was actually scanning.
    The fix validates before creating anything; this proves both halves:
    a clean 502 (not an unhandled 500), and zero StrategyRun rows left
    behind for a caller to mistake as live.
    """
    from app.modules.broker_adapter.base.errors import BrokerError

    def _raise(instrument_id, broker, symbol, expiry, session_factory=None):
        raise BrokerError("no NFO option contract found for underlying 'BANKNIFTY' expiry ...")

    monkeypatch.setattr(strategies_module, "record_option_chain_snapshot", _raise)

    _login(api_client, seeded_admin)
    strategy_id = api_client.post("/api/v1/strategies", json={"name": "orb-zombie"}).json()["id"]
    session_id = api_client.post(
        "/api/v1/sessions", json={"broker_account_id": str(seeded_admin["broker_account_id"])}
    ).json()["id"]

    instrument_id = uuid.uuid4()
    session_factory = sessionmaker(bind=engine, future=True)
    try:
        with session_factory() as db:
            db.add(
                Instrument(
                    id=instrument_id,
                    symbol="BANKNIFTY-ZOMBIE",
                    exchange="NFO",
                    lot_size=15,
                    tick_size=0.05,
                )
            )
            db.commit()

        start_resp = api_client.post(
            f"/api/v1/strategies/{strategy_id}/start",
            json={
                "trading_session_id": session_id,
                "instrument_id": str(instrument_id),
                "expiry_date": date(2026, 8, 6).isoformat(),
                "execution_mode": "auto",
            },
        )

        assert start_resp.status_code == 502
        assert len(_FakeRunner.instances) == 0
        with session_factory() as db:
            runs = (
                db.query(StrategyRun)
                .filter(StrategyRun.strategy_config_id == uuid.UUID(strategy_id))
                .all()
            )
            assert runs == []
    finally:
        with session_factory() as cleanup_db:
            cleanup_db.query(Instrument).filter(Instrument.id == instrument_id).delete()
            cleanup_db.commit()


def test_start_strategy_rejects_when_shoonya_not_connected(
    api_client: TestClient, seeded_admin, fake_runner, engine, monkeypatch
):
    """2026-08-14: same bug class as the restart-triggered ones fixed in
    _resume_strategy_runners/MarketDataScheduler, human-triggered here
    instead -- without this check, get_broker() falls back to the mock,
    record_option_chain_snapshot "succeeds" against fabricated data instead
    of raising BrokerError, and ensure_ingestion_running starts real
    ingestion against that same mock-wrapped provider. Rejects before any
    writes, same "validate first" shape as the zombie-run test above --
    proves both a clean 409 and zero StrategyRun rows left behind.
    """
    monkeypatch.setattr(strategies_module, "is_shoonya_market_data_ready", lambda: False)

    _login(api_client, seeded_admin)
    strategy_id = api_client.post(
        "/api/v1/strategies", json={"name": "orb-not-connected"}
    ).json()["id"]
    session_id = api_client.post(
        "/api/v1/sessions", json={"broker_account_id": str(seeded_admin["broker_account_id"])}
    ).json()["id"]

    instrument_id = uuid.uuid4()
    session_factory = sessionmaker(bind=engine, future=True)
    try:
        with session_factory() as db:
            db.add(
                Instrument(
                    id=instrument_id,
                    symbol="NIFTY-NOTCONNECTED",
                    exchange="NFO",
                    lot_size=25,
                    tick_size=0.05,
                )
            )
            db.commit()

        start_resp = api_client.post(
            f"/api/v1/strategies/{strategy_id}/start",
            json={
                "trading_session_id": session_id,
                "instrument_id": str(instrument_id),
                "expiry_date": date(2026, 8, 6).isoformat(),
                "execution_mode": "auto",
            },
        )

        assert start_resp.status_code == 409
        assert len(_FakeRunner.instances) == 0
        with session_factory() as db:
            runs = (
                db.query(StrategyRun)
                .filter(StrategyRun.strategy_config_id == uuid.UUID(strategy_id))
                .all()
            )
            assert runs == []
    finally:
        with session_factory() as cleanup_db:
            cleanup_db.query(Instrument).filter(Instrument.id == instrument_id).delete()
            cleanup_db.commit()


def test_approve_unknown_trade_approval_is_404(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    response = api_client.post(f"/api/v1/trade-approvals/{uuid.uuid4()}/approve")
    assert response.status_code == 404


def test_approving_a_pending_trade_dispatches_to_a_real_position(
    api_client: TestClient, seeded_admin, engine
):
    """Regression test: approving a trade must not leave it DISPATCHED
    forever with nothing downstream — Phase 2 had no real Execution Service
    to ever act on it (it stayed a bare status flip); Phase 3's
    api.v1.strategies.approve_trade_approval now hands off to
    execution_engine.paper.service.dispatch_trade_intent, which must produce
    a real open Position, not just flip the TradeIntent's status.
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
            strategy_run_id = strategy_run.id

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

        # Regression check for the pending_approvals shape: the running-
        # strategies list must carry each pending approval's own id (not
        # just a count), since the frontend's inline Approve/Reject buttons
        # act directly on it without a separate lookup.
        running_resp = api_client.get("/api/v1/strategies/running")
        assert running_resp.status_code == 200
        run_row = next(
            row for row in running_resp.json() if row["strategy_run_id"] == str(strategy_run_id)
        )
        assert [a["approval_id"] for a in run_row["pending_approvals"]] == [approval_id]

        approve_resp = api_client.post(f"/api/v1/trade-approvals/{approval_id}/approve")
        assert approve_resp.status_code == 200
        assert approve_resp.json()["trade_intent_status"] == TradeIntentStatus.DISPATCHED

        # Regression check: a second approve on an already-approved approval
        # must be a clean 409, not a 500 — found live via manual browser QC,
        # two rapid Approve clicks on the same pending approval produced a
        # real Postgres deadlock between the two requests' unlocked
        # check-then-act updates. approve_trade_approval now wraps its body
        # in LOCK_EXECUTION_SINGLETON (same reasoning start_strategy already
        # uses it for), which serializes this instead.
        second_approve_resp = api_client.post(f"/api/v1/trade-approvals/{approval_id}/approve")
        assert second_approve_resp.status_code == 409

        with session_factory() as verify_db:
            position = (
                verify_db.query(Position)
                .filter(Position.trade_intent_id == trade_intent_id)
                .one_or_none()
            )
            assert position is not None, "approved intent must dispatch to a real Position"
            assert position.status == PositionStatus.OPEN
    finally:
        # Runs before seeded_admin's own (workspace-scoped) teardown, so the
        # trade_intent/signal chain this test created still exists at this
        # point — must be cleared here too, in FK-safe order, before
        # OptionContract/Instrument can be deleted.
        with session_factory() as cleanup_db:
            from sqlalchemy import or_ as sa_or

            from app.domain.broker.models import BrokerSyncState
            from app.domain.execution.models import (
                Order,
                OrderEvent,
                StopPlan,
                TradeOutcome,
                TrailPlan,
            )
            from app.domain.risk.models import RiskDecision

            if trade_intent_id is not None:
                position = (
                    cleanup_db.query(Position)
                    .filter(Position.trade_intent_id == trade_intent_id)
                    .one_or_none()
                )
                if position is not None:
                    order_ids = [
                        row[0]
                        for row in cleanup_db.query(Order.id).filter(
                            sa_or(
                                Order.trade_intent_id == trade_intent_id,
                                Order.position_id == position.id,
                            )
                        )
                    ]
                    cleanup_db.query(OrderEvent).filter(
                        OrderEvent.order_id.in_(order_ids)
                    ).delete(synchronize_session=False)
                    cleanup_db.query(TradeOutcome).filter(
                        TradeOutcome.position_id == position.id
                    ).delete()
                    cleanup_db.query(StopPlan).filter(
                        StopPlan.position_id == position.id
                    ).delete()
                    cleanup_db.query(TrailPlan).filter(
                        TrailPlan.position_id == position.id
                    ).delete()
                    # orders <-> positions is a circular FK pair. Break it
                    # via positions.closing_order_id, which is nullable —
                    # NOT via orders.position_id, which would leave an exit
                    # order (always trade_intent_id=NULL) with both FK
                    # columns null, violating
                    # ck_order_exactly_one_of_intent_or_position.
                    cleanup_db.query(Position).filter(Position.id == position.id).update(
                        {"closing_order_id": None}, synchronize_session=False
                    )
                    cleanup_db.query(Order).filter(Order.position_id == position.id).delete(
                        synchronize_session=False
                    )
                    cleanup_db.query(Position).filter(Position.id == position.id).delete()
                    cleanup_db.query(Order).filter(Order.trade_intent_id == trade_intent_id).delete(
                        synchronize_session=False
                    )
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
            # Phase 3: the approve endpoint's dispatch_trade_intent call runs
            # an event-triggered reconciliation pass, which writes these.
            cleanup_db.query(BrokerSyncState).filter(
                BrokerSyncState.option_contract_id.in_(
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


# -- Ops-Hardening Phase 1: PATCH /strategies/{id} ---------------------------


def test_update_strategy_requires_login(api_client: TestClient):
    response = api_client.patch(
        f"/api/v1/strategies/{uuid.uuid4()}", json={"is_enabled": False}
    )
    assert response.status_code == 401


def test_update_strategy_unknown_id_is_404(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    response = api_client.patch(
        f"/api/v1/strategies/{uuid.uuid4()}", json={"is_enabled": False}
    )
    assert response.status_code == 404


def test_new_strategy_defaults_to_enabled_with_no_runtime_override(
    api_client: TestClient, seeded_admin
):
    _login(api_client, seeded_admin)
    created = api_client.post("/api/v1/strategies", json={"name": "orb-patch-default"}).json()

    assert created["is_enabled"] is True
    assert created["runtime_mode"] is None


def test_patch_toggles_is_enabled(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    strategy_id = api_client.post(
        "/api/v1/strategies", json={"name": "orb-patch-enable"}
    ).json()["id"]

    response = api_client.patch(
        f"/api/v1/strategies/{strategy_id}", json={"is_enabled": False}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["is_enabled"] is False
    assert body["runtime_mode"] is None

    refetched = api_client.get("/api/v1/strategies").json()
    updated = next(row for row in refetched if row["id"] == strategy_id)
    assert updated["is_enabled"] is False


def test_patch_sets_and_clears_runtime_mode(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    strategy_id = api_client.post(
        "/api/v1/strategies", json={"name": "orb-patch-runtime-mode"}
    ).json()["id"]

    set_resp = api_client.patch(
        f"/api/v1/strategies/{strategy_id}", json={"runtime_mode": "force_paper"}
    )
    assert set_resp.status_code == 200
    assert set_resp.json()["runtime_mode"] == "force_paper"
    assert set_resp.json()["is_enabled"] is True  # untouched by this call

    # Explicit null clears the override -- distinct from simply omitting
    # the field, which the next test covers.
    clear_resp = api_client.patch(
        f"/api/v1/strategies/{strategy_id}", json={"runtime_mode": None}
    )
    assert clear_resp.status_code == 200
    assert clear_resp.json()["runtime_mode"] is None


def test_patch_omitting_a_field_leaves_it_untouched(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    strategy_id = api_client.post(
        "/api/v1/strategies", json={"name": "orb-patch-partial"}
    ).json()["id"]
    api_client.patch(f"/api/v1/strategies/{strategy_id}", json={"runtime_mode": "force_paper"})

    # Omitting runtime_mode entirely (not passing it as null) while only
    # updating is_enabled must not clear the override set above.
    response = api_client.patch(f"/api/v1/strategies/{strategy_id}", json={"is_enabled": False})

    assert response.status_code == 200
    assert response.json()["is_enabled"] is False
    assert response.json()["runtime_mode"] == "force_paper"


def test_patch_rejects_unknown_runtime_mode_value(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    strategy_id = api_client.post(
        "/api/v1/strategies", json={"name": "orb-patch-invalid"}
    ).json()["id"]

    response = api_client.patch(
        f"/api/v1/strategies/{strategy_id}", json={"runtime_mode": "force_live"}
    )

    assert response.status_code == 422


@contextmanager
def _seeded_instrument(engine, symbol: str):
    """Instrument has no workspace scoping (shared exchange-wide data), same
    as the instrument seeded in test_start_strategy_creates_run_and_stop_ends_it
    above — a plain `session_factory(bind=engine)` commit bypasses the `db`
    fixture's per-test rollback, so it must be deleted explicitly or it
    leaks into every later test in the same run (found live: broke
    test_instrument_sync.py's exact-count assertions).
    """
    instrument_id = uuid.uuid4()
    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as db:
        db.add(
            Instrument(
                id=instrument_id, symbol=symbol, exchange="NFO", lot_size=25, tick_size=0.05
            )
        )
        db.commit()
    try:
        yield instrument_id
    finally:
        with session_factory() as cleanup_db:
            cleanup_db.query(Instrument).filter(Instrument.id == instrument_id).delete()
            cleanup_db.commit()


def test_create_strategy_with_valid_underlying_symbol(api_client: TestClient, seeded_admin, engine):
    with _seeded_instrument(engine, "NIFTY-UND-1"):
        _login(api_client, seeded_admin)

        response = api_client.post(
            "/api/v1/strategies",
            json={"name": "orb-underlying-valid", "underlying_symbol": "NIFTY-UND-1"},
        )

        assert response.status_code == 200
        assert response.json()["underlying_symbol"] == "NIFTY-UND-1"


def test_create_strategy_with_unknown_underlying_symbol_is_400(
    api_client: TestClient, seeded_admin
):
    _login(api_client, seeded_admin)

    response = api_client.post(
        "/api/v1/strategies",
        json={"name": "orb-underlying-unknown", "underlying_symbol": "NOSUCHTHING"},
    )

    assert response.status_code == 400


def test_patch_sets_and_clears_underlying_symbol(api_client: TestClient, seeded_admin, engine):
    with _seeded_instrument(engine, "NIFTY-UND-2"):
        _login(api_client, seeded_admin)
        strategy_id = api_client.post(
            "/api/v1/strategies", json={"name": "orb-patch-underlying"}
        ).json()["id"]

        set_resp = api_client.patch(
            f"/api/v1/strategies/{strategy_id}", json={"underlying_symbol": "NIFTY-UND-2"}
        )
        assert set_resp.status_code == 200
        assert set_resp.json()["underlying_symbol"] == "NIFTY-UND-2"

        clear_resp = api_client.patch(
            f"/api/v1/strategies/{strategy_id}", json={"underlying_symbol": None}
        )
        assert clear_resp.status_code == 200
        assert clear_resp.json()["underlying_symbol"] is None


def test_patch_rejects_unknown_underlying_symbol(api_client: TestClient, seeded_admin):
    _login(api_client, seeded_admin)
    strategy_id = api_client.post(
        "/api/v1/strategies", json={"name": "orb-patch-underlying-invalid"}
    ).json()["id"]

    response = api_client.patch(
        f"/api/v1/strategies/{strategy_id}", json={"underlying_symbol": "NOSUCHTHING"}
    )

    assert response.status_code == 400
