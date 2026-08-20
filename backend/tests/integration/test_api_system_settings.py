"""Ops-Hardening Phase 7: GET/PATCH /api/v1/system-settings/instrument-firewall
-- API-level behavior (auth, validation, persistence, workspace scoping).

2026-08-20: POST /api/v1/system-settings/restart-backend added to the same
file -- same router, same risk.override gate.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import app.api.v1.system_settings as system_settings_module
from app.core.db.session import get_db
from app.core.security.passwords import hash_password
from app.domain.broker.models import BrokerSyncState, ReconciliationRun
from app.domain.execution.models import (
    Order,
    OrderEvent,
    OrderMode,
    Position,
    StopPlan,
    TrailPlan,
)
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
from app.domain.ops.models import InstrumentFirewallConfig
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import (
    ExecutionMode,
    Signal,
    SignalSide,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    TradeIntent,
    TradeIntentStatus,
)
from app.main import app
from app.modules.broker_adapter.mock.adapter import MockBrokerAdapter
from app.modules.execution_engine.paper.service import dispatch_trade_intent

ADMIN_PASSWORD = "correct horse battery staple 123!"
EXPIRY = date(2026, 7, 30)


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


# -- POST /system-settings/restart-backend -----------------------------------


@pytest.fixture
def open_live_position(engine):
    """A genuinely open, live-mode Position -- self-contained in its own
    throwaway workspace (restart-backend's own query has no workspace
    filter, so this doesn't need to share seeded_admin's workspace). Built
    via a real `dispatch_trade_intent` call (explicit `broker=`, so it
    creates a normal PAPER order+position through the real code path) and
    then flips `Order.mode` to LIVE directly -- same established trick as
    `test_risk_engine.py`'s own `_mark_last_order_live` helper -- rather
    than hand-assembling an Order/Position pair and risking getting
    `ck_order_exactly_one_of_intent_or_position` wrong.
    """
    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as db:
        workspace = Workspace(id=uuid.uuid4(), name=f"restart-test-{uuid.uuid4().hex[:8]}")
        db.add(workspace)
        db.flush()

        owner = User(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            email=f"restart-owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password(ADMIN_PASSWORD),
            display_name="Restart Test Owner",
            is_active=True,
        )
        db.add(owner)
        db.flush()

        broker_account = BrokerAccount(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            broker_type=BrokerType.SHOONYA,
            label="restart-test-account",
            credentials_ref="config/credentials/shoonya.env",
            status=BrokerAccountStatus.ACTIVE,
        )
        db.add(broker_account)
        db.flush()

        trading_session = TradingSession(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            broker_account_id=broker_account.id,
            started_by_user_id=owner.id,
            mode=SafeMode.PAPER_ONLY,
            started_at=datetime.now(UTC),
            budget_amount=1_000_000,
            daily_target_profit=1_000_000,
            daily_loss_cap=1_000_000,
            funding_mode=FundingMode.CASH,
        )
        db.add(trading_session)
        db.flush()

        # A dedicated, never-real symbol/exchange -- NOT "NIFTY"/"NFO".
        # This fixture uses real commits (session_factory, not the
        # rollback-based `db` fixture most other test files rely on), so
        # anything inserted here genuinely persists for the rest of this
        # pytest run. Reusing the real "NIFTY"/"NFO" identity broke
        # test_risk_engine.py (and others) when this file ran first in a
        # full-suite run -- their own `instrument` fixtures assume they're
        # the first and only writer of that identity within their own
        # rolled-back transaction, and collided with this one's leftover,
        # genuinely-committed row.
        instrument = Instrument(
            id=uuid.uuid4(),
            symbol="RESTARTTEST",
            exchange="NFO-TEST",
            lot_size=25,
            tick_size=0.05,
        )
        db.add(instrument)
        db.flush()
        option_contract = OptionContract(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            expiry_date=EXPIRY,
            strike=88888,
            option_type=OptionType.CE,
            symbol="RESTARTTEST26JUL88888CE",
        )
        db.add(option_contract)
        db.flush()

        strategy_config = StrategyConfig(
            id=uuid.uuid4(), workspace_id=workspace.id, name="restart-test-strategy"
        )
        db.add(strategy_config)
        db.flush()
        strategy_run = StrategyRun(
            id=uuid.uuid4(),
            strategy_config_id=strategy_config.id,
            trading_session_id=trading_session.id,
            execution_mode=ExecutionMode.AUTO,
            status=StrategyRunStatus.SCANNING,
            started_at=datetime.now(UTC),
            started_by_user_id=owner.id,
        )
        db.add(strategy_run)
        db.flush()

        now = datetime.now(UTC)
        signal = Signal(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            strategy_config_id=strategy_config.id,
            strategy_run_id=strategy_run.id,
            trading_session_id=trading_session.id,
            option_contract_id=option_contract.id,
            side=SignalSide.BUY,
            entry_price=80.0,
            stop_price=72.0,
            target_price=92.0,
            qty_lots=1,
            generated_at=now,
        )
        db.add(signal)
        db.flush()
        trade_intent = TradeIntent(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            signal_id=signal.id,
            strategy_run_id=strategy_run.id,
            trading_session_id=trading_session.id,
            option_contract_id=option_contract.id,
            idempotency_key=f"signal:{signal.id}",
            side=SignalSide.BUY,
            qty_lots=1,
            entry_price=80.0,
            stop_price=72.0,
            target_price=92.0,
            status=TradeIntentStatus.DISPATCHED,
            created_at=now,
            dispatched_at=now,
        )
        db.add(trade_intent)
        db.flush()

        order = dispatch_trade_intent(
            db, trading_session, trade_intent, broker=MockBrokerAdapter()
        )
        order.mode = OrderMode.LIVE
        db.add(order)
        db.commit()

        position = db.query(Position).filter(Position.trade_intent_id == trade_intent.id).one()
        ids = {
            "workspace_id": workspace.id,
            "position_id": position.id,
            "order_id": order.id,
            "owner_id": owner.id,
            "instrument_id": instrument.id,
        }

    yield ids

    with session_factory() as cleanup_db:
        # dispatch_trade_intent also creates a StopPlan/TrailPlan (unique
        # FK to positions.id) and an OrderEvent (FK to orders.id) -- all
        # three must go before Position/Order themselves or the delete
        # aborts on a ForeignKeyViolation, leaving nothing committed and
        # every row (including the OptionContract) stuck for the next test.
        cleanup_db.query(StopPlan).filter(StopPlan.position_id == ids["position_id"]).delete()
        cleanup_db.query(TrailPlan).filter(TrailPlan.position_id == ids["position_id"]).delete()
        cleanup_db.query(OrderEvent).filter(OrderEvent.order_id == ids["order_id"]).delete()
        cleanup_db.query(Position).filter(Position.id == ids["position_id"]).delete()
        cleanup_db.query(Order).filter(Order.id == ids["order_id"]).delete()
        cleanup_db.query(TradeIntent).filter(
            TradeIntent.trading_session_id.in_(
                cleanup_db.query(TradingSession.id).filter(
                    TradingSession.workspace_id == ids["workspace_id"]
                )
            )
        ).delete(synchronize_session=False)
        cleanup_db.query(Signal).filter(Signal.workspace_id == ids["workspace_id"]).delete()
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
        # dispatch_trade_intent's own internal event-triggered reconciliation
        # call (run_reconciliation) writes a BrokerSyncState + ReconciliationRun
        # row per dispatch -- both FK to trading_sessions.id too.
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
        cleanup_db.query(OptionContract).filter(
            OptionContract.symbol == "RESTARTTEST26JUL88888CE"
        ).delete()
        cleanup_db.query(Instrument).filter(
            Instrument.id == ids["instrument_id"]
        ).delete()
        cleanup_db.query(BrokerAccount).filter(
            BrokerAccount.workspace_id == ids["workspace_id"]
        ).delete()
        # dispatch_trade_intent's own record_event call (order.dispatched)
        # writes an AuditEvent under this workspace too -- FK to
        # workspaces.id blocks the final Workspace delete otherwise.
        from app.domain.audit.models import AuditEvent

        cleanup_db.query(AuditEvent).filter(
            AuditEvent.workspace_id == ids["workspace_id"]
        ).delete()
        cleanup_db.query(User).filter(User.id == ids["owner_id"]).delete()
        cleanup_db.query(Workspace).filter(Workspace.id == ids["workspace_id"]).delete()
        cleanup_db.commit()


def test_restart_backend_requires_login(api_client: TestClient):
    response = api_client.post(
        "/api/v1/system-settings/restart-backend", json={"reason": "test"}
    )
    assert response.status_code == 401


def test_restart_backend_refused_off_linux(
    api_client: TestClient, seeded_admin, monkeypatch
):
    """2026-08-20: explicit monkeypatch, not ambient OS state -- the
    original version of this test relied on `platform.system()` returning
    something other than "Linux" by default, true by coincidence on a
    Windows dev machine but false on GitHub Actions' actual Linux runners,
    where it silently exercised the *allowed* path instead and returned
    200, not 400. Real CI failure, caught live 2026-08-20. Forcing a
    concrete non-Linux value here makes the test deterministic regardless
    of which OS actually runs pytest, matching the other three tests in
    this file that already force "Linux" the same explicit way.
    """
    monkeypatch.setattr(system_settings_module.platform, "system", lambda: "Windows")
    _login(api_client, seeded_admin)

    response = api_client.post(
        "/api/v1/system-settings/restart-backend", json={"reason": "test"}
    )

    assert response.status_code == 400


def test_restart_backend_blocked_by_open_live_position(
    api_client: TestClient, seeded_admin, open_live_position, monkeypatch
):
    monkeypatch.setattr(system_settings_module.platform, "system", lambda: "Linux")
    called = []
    monkeypatch.setattr(system_settings_module, "_schedule_restart", lambda: called.append(True))
    _login(api_client, seeded_admin)

    response = api_client.post(
        "/api/v1/system-settings/restart-backend", json={"reason": "test"}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["open_live_positions"][0]["contract_symbol"] == (
        "RESTARTTEST26JUL88888CE"
    )
    assert called == []


def test_restart_backend_allowed_with_force(
    api_client: TestClient, seeded_admin, open_live_position, monkeypatch
):
    monkeypatch.setattr(system_settings_module.platform, "system", lambda: "Linux")
    called = []
    monkeypatch.setattr(system_settings_module, "_schedule_restart", lambda: called.append(True))
    _login(api_client, seeded_admin)

    response = api_client.post(
        "/api/v1/system-settings/restart-backend",
        json={"reason": "test", "force": True},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert called == [True]


def test_restart_backend_allowed_with_no_open_positions(
    api_client: TestClient, seeded_admin, monkeypatch
):
    monkeypatch.setattr(system_settings_module.platform, "system", lambda: "Linux")
    called = []
    monkeypatch.setattr(system_settings_module, "_schedule_restart", lambda: called.append(True))
    _login(api_client, seeded_admin)

    response = api_client.post(
        "/api/v1/system-settings/restart-backend", json={"reason": "test"}
    )

    assert response.status_code == 200
    assert called == [True]
