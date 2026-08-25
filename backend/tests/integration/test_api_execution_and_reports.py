"""API-level tests for the Phase 3 read/report/manual-trigger endpoints:
`GET /orders`, `GET /positions`, `GET /reports/.../daily`,
`GET /reports/.../scorecard`, `POST /sessions/{id}/square-off`,
`POST /sessions/{id}/reconcile`. Scope matches test_api_strategies.py's own
stated scope — route wiring, permissions, and workspace scoping; the
underlying math is already covered by test_reporting.py and
test_reconciliation.py. `submit_signal` is called directly (not via the
strategy-start HTTP route) so this file never needs to touch
`StrategyRunner`/`PositionManager` background threads at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from app.core.clock import IST
from app.core.db.session import get_db
from app.core.security.passwords import hash_password
from app.domain.execution.models import Order, Position
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
from app.domain.market.models import Instrument, OptionContract, OptionType, QuoteTick
from app.domain.session.models import TradingSession
from app.domain.strategy.models import (
    ExecutionMode,
    SignalSide,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
)
from app.main import app
from app.modules.scheduler.eod_square_off import UnresolvableOptionContractError
from app.modules.strategy_engine.interface import TradeProposal
from app.modules.strategy_engine.service import submit_signal

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
        workspace = Workspace(id=uuid.uuid4(), name=f"exec-api-test-{uuid.uuid4().hex[:8]}")
        db.add(workspace)
        db.flush()

        role = Role(id=uuid.uuid4(), name=f"exec-api-admin-{uuid.uuid4().hex[:8]}")
        db.add(role)
        db.flush()
        permission_ids: list[uuid.UUID] = []
        for code in ("session.start", "session.stop", "strategy.view", "strategy.edit"):
            permission = Permission(id=uuid.uuid4(), code=code, description="")
            db.add(permission)
            db.flush()
            permission_ids.append(permission.id)
            db.add(RolePermission(role_id=role.id, permission_id=permission.id))

        user = User(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            email=f"exec-api-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password(ADMIN_PASSWORD),
            display_name="Exec API Test Admin",
            is_active=True,
        )
        db.add(user)
        db.flush()
        db.add(UserRole(user_id=user.id, role_id=role.id, workspace_id=workspace.id))

        broker_account = BrokerAccount(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            broker_type=BrokerType.SHOONYA,
            label="exec-api-test-account",
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
        from app.domain.execution.models import OrderEvent, StopPlan, TradeOutcome, TrailPlan
        from app.domain.identity.models import LoginSession
        from app.domain.ops.models import SystemAlert
        from app.domain.risk.models import RiskDecision, RiskLimitConfig
        from app.domain.strategy.models import PendingTradeApproval, Signal, TradeIntent

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
        # Entry orders (trade_intent_id set) are now unreferenced (their
        # position, if any, is already gone).
        cleanup_db.query(Order).filter(Order.trade_intent_id.in_(trade_intent_ids)).delete(
            synchronize_session=False
        )
        cleanup_db.query(BrokerSyncState).filter(
            BrokerSyncState.workspace_id == ids["workspace_id"]
        ).delete()
        cleanup_db.query(ReconciliationRun).filter(
            ReconciliationRun.workspace_id == ids["workspace_id"]
        ).delete()
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


def _dispatch_one_position(engine, seeded_admin, session_id: str) -> uuid.UUID:
    """Directly drives Signal -> TradeIntent -> RiskDecision -> (real,
    auto-mode) dispatch, same as test_api_strategies.py's approval test does
    for the approval-required path — bypasses the HTTP strategy-start route
    entirely, so no background thread is ever spawned by this file. Returns
    the created Instrument's id so the caller can clean it up (see
    `_cleanup_instrument_and_dependents` below) — Instrument/OptionContract
    have no workspace scoping (shared, exchange-wide data), so
    `seeded_admin`'s own workspace-scoped teardown can't reach them.
    """
    tag = uuid.uuid4().hex[:8]
    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as db:
        instrument = Instrument(
            id=uuid.uuid4(),
            symbol=f"NIFTY-EXECAPI-{tag}",
            exchange="NFO",
            lot_size=25,
            tick_size=0.05,
        )
        db.add(instrument)
        db.flush()
        option_contract = OptionContract(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            expiry_date=EXPIRY,
            strike=22000,
            option_type=OptionType.CE,
            symbol=f"NIFTY-EXECAPI-{tag}-26JUL22000CE",
        )
        db.add(option_contract)
        db.flush()

        trading_session = db.get(TradingSession, uuid.UUID(session_id))
        assert trading_session is not None

        strategy_config = StrategyConfig(
            id=uuid.uuid4(), workspace_id=seeded_admin["workspace_id"], name="exec-api-strategy"
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

        return instrument.id


def _cleanup_instrument_and_dependents(engine, instrument_id: uuid.UUID) -> None:
    """Deletes an Instrument created by `_dispatch_one_position` and
    everything that ended up referencing its OptionContract — the
    execution-domain rows are workspace-scoped and would eventually be
    cleaned by `seeded_admin`'s own teardown anyway, but that runs *after*
    this function (fixture teardown happens after the test body returns),
    and Instrument/OptionContract can't be deleted while anything still
    references them. Same FK-safe-order reasoning as
    test_api_strategies.py's own equivalent cleanup.
    """
    from sqlalchemy import or_ as sa_or

    from app.domain.broker.models import BrokerSyncState
    from app.domain.execution.models import (
        Order,
        OrderEvent,
        Position,
        StopPlan,
        TradeOutcome,
        TrailPlan,
    )
    from app.domain.market.models import OptionChainSnapshot
    from app.domain.risk.models import RiskDecision
    from app.domain.strategy.models import PendingTradeApproval, Signal, TradeIntent

    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as db:
        contract_ids = db.query(OptionContract.id).filter(
            OptionContract.instrument_id == instrument_id
        )
        trade_intent_ids = db.query(TradeIntent.id).filter(
            TradeIntent.option_contract_id.in_(contract_ids)
        )
        position_ids = db.query(Position.id).filter(
            Position.trade_intent_id.in_(trade_intent_ids)
        )
        order_ids = [
            row[0]
            for row in db.query(Order.id).filter(
                sa_or(
                    Order.trade_intent_id.in_(trade_intent_ids),
                    Order.position_id.in_(position_ids),
                )
            )
        ]

        db.query(OrderEvent).filter(OrderEvent.order_id.in_(order_ids)).delete(
            synchronize_session=False
        )
        db.query(TradeOutcome).filter(TradeOutcome.position_id.in_(position_ids)).delete(
            synchronize_session=False
        )
        db.query(StopPlan).filter(StopPlan.position_id.in_(position_ids)).delete(
            synchronize_session=False
        )
        db.query(TrailPlan).filter(TrailPlan.position_id.in_(position_ids)).delete(
            synchronize_session=False
        )
        db.query(Position).filter(Position.id.in_(position_ids)).update(
            {"closing_order_id": None}, synchronize_session=False
        )
        db.query(Order).filter(Order.position_id.in_(position_ids)).delete(
            synchronize_session=False
        )
        db.query(Position).filter(Position.trade_intent_id.in_(trade_intent_ids)).delete(
            synchronize_session=False
        )
        db.query(Order).filter(Order.trade_intent_id.in_(trade_intent_ids)).delete(
            synchronize_session=False
        )
        db.query(BrokerSyncState).filter(
            BrokerSyncState.option_contract_id.in_(contract_ids)
        ).delete(synchronize_session=False)
        db.query(PendingTradeApproval).filter(
            PendingTradeApproval.trade_intent_id.in_(trade_intent_ids)
        ).delete(synchronize_session=False)
        db.query(RiskDecision).filter(RiskDecision.trade_intent_id.in_(trade_intent_ids)).delete(
            synchronize_session=False
        )
        db.query(TradeIntent).filter(TradeIntent.option_contract_id.in_(contract_ids)).delete(
            synchronize_session=False
        )
        db.query(Signal).filter(Signal.option_contract_id.in_(contract_ids)).delete(
            synchronize_session=False
        )
        db.query(OptionContract).filter(OptionContract.instrument_id == instrument_id).delete(
            synchronize_session=False
        )
        # New since the Stage 1 price-source fix: real EOD/margin-breach
        # square-off (current_contract_price -> ensure_fresh_option_chain)
        # can now genuinely write an OptionChainSnapshot row for this
        # instrument, which didn't happen before (that path used
        # broker.get_quote() directly, no DB write) -- must be cleaned up
        # before the Instrument delete or it FK-violates.
        db.query(OptionChainSnapshot).filter(
            OptionChainSnapshot.instrument_id == instrument_id
        ).delete(synchronize_session=False)
        db.query(Instrument).filter(Instrument.id == instrument_id).delete()
        db.commit()


def test_orders_and_positions_require_login(api_client: TestClient):
    params = {"trading_session_id": str(uuid.uuid4())}
    assert api_client.get("/api/v1/orders", params=params).status_code == 401
    assert api_client.get("/api/v1/positions", params=params).status_code == 401


def test_square_off_position_requires_login(api_client: TestClient):
    resp = api_client.post(f"/api/v1/positions/{uuid.uuid4()}/square-off")
    assert resp.status_code == 401


def test_square_off_position_closes_a_single_open_paper_position_and_audits_it(
    api_client: TestClient, seeded_admin, engine
):
    """The narrower sibling of `test_full_flow_orders_positions_reports_square_off`
    below -- `POST /positions/{id}/square-off` closes exactly the one
    position named, reuses `close_position`'s own idempotency/locking (no
    new Order/Position lifecycle invented here), and writes a `USER`-actor
    `MANUAL_OVERRIDE` audit event distinct from `close_position`'s own
    `SYSTEM`-actor `position.closed` event -- both must exist afterward.
    """
    _login(api_client, seeded_admin)
    session_id = api_client.post(
        "/api/v1/sessions", json={"broker_account_id": str(seeded_admin["broker_account_id"])}
    ).json()["id"]

    instrument_id = _dispatch_one_position(engine, seeded_admin, session_id)
    try:
        position_id = api_client.get(
            "/api/v1/positions", params={"trading_session_id": session_id}
        ).json()[0]["id"]

        resp = api_client.post(f"/api/v1/positions/{position_id}/square-off")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["position_id"] == position_id
        assert body["exit_reason"] == "manual"
        assert body["exit_price"] is not None
        assert body["closed_at"] is not None

        positions_after = api_client.get(
            "/api/v1/positions", params={"trading_session_id": session_id}
        ).json()
        assert positions_after[0]["status"] == "closed"
        assert positions_after[0]["exit_reason"] == "manual"

        session_factory = sessionmaker(bind=engine, future=True)
        with session_factory() as db:
            from app.domain.audit.models import ActorType, AuditEvent

            manual_events = (
                db.query(AuditEvent)
                .filter(
                    AuditEvent.workspace_id == seeded_admin["workspace_id"],
                    AuditEvent.event_type == "position.manual_square_off_requested",
                )
                .all()
            )
            assert len(manual_events) == 1
            assert manual_events[0].actor_type == ActorType.USER
            assert manual_events[0].actor_id == seeded_admin["user_id"]
            assert str(manual_events[0].entity_id) == position_id
            assert manual_events[0].payload["success"] is True

            system_events = (
                db.query(AuditEvent)
                .filter(
                    AuditEvent.workspace_id == seeded_admin["workspace_id"],
                    AuditEvent.event_type == "position.closed",
                )
                .all()
            )
            assert len(system_events) == 1
    finally:
        _cleanup_instrument_and_dependents(engine, instrument_id)


def test_square_off_position_rejects_an_already_closed_position(
    api_client: TestClient, seeded_admin, engine
):
    _login(api_client, seeded_admin)
    session_id = api_client.post(
        "/api/v1/sessions", json={"broker_account_id": str(seeded_admin["broker_account_id"])}
    ).json()["id"]

    instrument_id = _dispatch_one_position(engine, seeded_admin, session_id)
    try:
        position_id = api_client.get(
            "/api/v1/positions", params={"trading_session_id": session_id}
        ).json()[0]["id"]

        first = api_client.post(f"/api/v1/positions/{position_id}/square-off")
        assert first.status_code == 200
        assert first.json()["success"] is True

        second = api_client.post(f"/api/v1/positions/{position_id}/square-off")
        assert second.status_code == 409
    finally:
        _cleanup_instrument_and_dependents(engine, instrument_id)


def test_square_off_position_denies_unknown_or_cross_workspace_position_id(
    api_client: TestClient, seeded_admin
):
    _login(api_client, seeded_admin)
    resp = api_client.post(f"/api/v1/positions/{uuid.uuid4()}/square-off")
    assert resp.status_code == 404


def test_full_flow_orders_positions_reports_square_off(
    api_client: TestClient, seeded_admin, engine
):
    _login(api_client, seeded_admin)
    session_id = api_client.post(
        "/api/v1/sessions", json={"broker_account_id": str(seeded_admin["broker_account_id"])}
    ).json()["id"]

    instrument_id = _dispatch_one_position(engine, seeded_admin, session_id)
    try:
        orders_resp = api_client.get("/api/v1/orders", params={"trading_session_id": session_id})
        assert orders_resp.status_code == 200
        orders = orders_resp.json()
        assert len(orders) == 1
        assert orders[0]["status"] == "filled"

        positions_resp = api_client.get(
            "/api/v1/positions", params={"trading_session_id": session_id}
        )
        assert positions_resp.status_code == 200
        positions = positions_resp.json()
        assert len(positions) == 1
        assert positions[0]["status"] == "open"
        # mode is the entry order's actual recorded mode (ground truth for
        # Live/Paper bucketing), not the session's current config -- see
        # PositionOut.mode's own docstring. _dispatch_one_position dispatches
        # with no broker passed explicitly, so this resolves through the
        # default (mock) broker and should be tagged 'paper'.
        assert positions[0]["mode"] == "paper"

        # Daily report before any close: one signal/dispatch/fill, zero closed trades.
        daily_before = api_client.get(f"/api/v1/reports/sessions/{session_id}/daily")
        assert daily_before.status_code == 200
        body_before = daily_before.json()
        assert body_before["trade_count"] == 0
        assert body_before["signal_count"] == 1
        assert body_before["dispatched_count"] == 1
        assert body_before["filled_count"] == 1

        reconcile_resp = api_client.post(f"/api/v1/sessions/{session_id}/reconcile")
        assert reconcile_resp.status_code == 200
        assert reconcile_resp.json()["mismatches_found"] == 0

        square_off_resp = api_client.post(f"/api/v1/sessions/{session_id}/square-off")
        assert square_off_resp.status_code == 200
        assert square_off_resp.json()["closed_count"] == 1

        positions_after = api_client.get(
            "/api/v1/positions", params={"trading_session_id": session_id}
        ).json()
        assert positions_after[0]["status"] == "closed"

        # Daily report after the square-off: exactly one closed trade now.
        daily_after = api_client.get(f"/api/v1/reports/sessions/{session_id}/daily").json()
        assert daily_after["trade_count"] == 1
    finally:
        _cleanup_instrument_and_dependents(engine, instrument_id)


def test_orders_endpoint_denies_cross_workspace_session_id(
    api_client: TestClient, seeded_admin, engine
):
    _login(api_client, seeded_admin)
    session_id = api_client.post(
        "/api/v1/sessions", json={"broker_account_id": str(seeded_admin["broker_account_id"])}
    ).json()["id"]

    # A brand-new, never-logged-in-as user has their own session (none) —
    # simplest cross-workspace check is a session_id that doesn't belong to
    # *this* logged-in user at all.
    unrelated_id = str(uuid.uuid4())
    resp = api_client.get("/api/v1/orders", params={"trading_session_id": unrelated_id})
    assert resp.status_code == 404
    assert session_id  # sanity: the real session_id is a different, valid one


def test_scorecard_reflects_dispatched_strategy(api_client: TestClient, seeded_admin, engine):
    _login(api_client, seeded_admin)
    session_id = api_client.post(
        "/api/v1/sessions", json={"broker_account_id": str(seeded_admin["broker_account_id"])}
    ).json()["id"]
    instrument_id = _dispatch_one_position(engine, seeded_admin, session_id)
    try:
        session_factory = sessionmaker(bind=engine, future=True)
        with session_factory() as db:
            strategy_config_id = str(
                db.query(StrategyConfig)
                .filter(StrategyConfig.workspace_id == seeded_admin["workspace_id"])
                .one()
                .id
            )

        resp = api_client.get(f"/api/v1/reports/strategies/{strategy_config_id}/scorecard")
        assert resp.status_code == 200
        body = resp.json()
        assert body["strategy_config_id"] == strategy_config_id
        assert body["dispatched_count"] == 1
        assert body["filled_count"] == 1
    finally:
        _cleanup_instrument_and_dependents(engine, instrument_id)


# -- GET /positions: ltp freshness (QC fix #2) --------------------------------


def test_positions_reports_a_fresh_ltp_as_not_stale(
    api_client: TestClient, seeded_admin, engine
):
    """A just-written tick (well within `TICK_THRESHOLDS.degraded_after_
    seconds`) must classify as not-stale via the shared `market_data
    .freshness` module -- `ltp_stale`/`ltp_age_seconds` didn't exist at all
    before this fix, so a position's `ltp` was returned with no way for a
    caller to tell an ancient tick from a live one.
    """
    _login(api_client, seeded_admin)
    session_id = api_client.post(
        "/api/v1/sessions", json={"broker_account_id": str(seeded_admin["broker_account_id"])}
    ).json()["id"]
    instrument_id = _dispatch_one_position(engine, seeded_admin, session_id)
    try:
        position = api_client.get(
            "/api/v1/positions", params={"trading_session_id": session_id}
        ).json()[0]
        contract_id = uuid.UUID(position["option_contract_id"])

        session_factory = sessionmaker(bind=engine, future=True)
        with session_factory() as db:
            db.add(
                QuoteTick(
                    id=uuid.uuid4(),
                    option_contract_id=contract_id,
                    ltp=85.5,
                    bid=85.0,
                    ask=86.0,
                    volume=10,
                    ts=datetime.now(UTC) - timedelta(seconds=2),
                )
            )
            db.commit()

        after = api_client.get(
            "/api/v1/positions", params={"trading_session_id": session_id}
        ).json()[0]
        assert after["ltp"] == pytest.approx(85.5)
        assert after["ltp_stale"] is False
        assert after["ltp_age_seconds"] is not None
        assert after["ltp_age_seconds"] < 10.0
        # entry_price=80.0 (see _dispatch_one_position's TradeProposal), qty
        # 25 (lot_size) * 1 lot, BUY side: (85.5 - 80.0) * 25.
        assert after["unrealized_pnl"] == pytest.approx(137.5)

        with session_factory() as db:
            db.query(QuoteTick).filter(QuoteTick.option_contract_id == contract_id).delete()
            db.commit()
    finally:
        _cleanup_instrument_and_dependents(engine, instrument_id)


def test_positions_reports_an_old_ltp_as_stale(api_client: TestClient, seeded_admin, engine):
    """A tick older than `TICK_THRESHOLDS.stale_after_seconds` must classify
    as stale -- same freshness module every other price read in this
    codebase already goes through (`classify_option_chain` etc.), reused
    here rather than a new, ad hoc staleness policy.
    """
    _login(api_client, seeded_admin)
    session_id = api_client.post(
        "/api/v1/sessions", json={"broker_account_id": str(seeded_admin["broker_account_id"])}
    ).json()["id"]
    instrument_id = _dispatch_one_position(engine, seeded_admin, session_id)
    try:
        position = api_client.get(
            "/api/v1/positions", params={"trading_session_id": session_id}
        ).json()[0]
        contract_id = uuid.UUID(position["option_contract_id"])

        session_factory = sessionmaker(bind=engine, future=True)
        with session_factory() as db:
            db.add(
                QuoteTick(
                    id=uuid.uuid4(),
                    option_contract_id=contract_id,
                    ltp=90.0,
                    bid=89.0,
                    ask=91.0,
                    volume=10,
                    ts=datetime.now(UTC) - timedelta(seconds=300),
                )
            )
            db.commit()

        after = api_client.get(
            "/api/v1/positions", params={"trading_session_id": session_id}
        ).json()[0]
        assert after["ltp"] == pytest.approx(90.0)
        assert after["ltp_stale"] is True
        assert after["ltp_age_seconds"] >= 300.0

        with session_factory() as db:
            db.query(QuoteTick).filter(QuoteTick.option_contract_id == contract_id).delete()
            db.commit()
    finally:
        _cleanup_instrument_and_dependents(engine, instrument_id)


def test_positions_with_no_tick_leaves_ltp_and_staleness_fields_none(
    api_client: TestClient, seeded_admin, engine
):
    _login(api_client, seeded_admin)
    session_id = api_client.post(
        "/api/v1/sessions", json={"broker_account_id": str(seeded_admin["broker_account_id"])}
    ).json()["id"]
    instrument_id = _dispatch_one_position(engine, seeded_admin, session_id)
    try:
        position = api_client.get(
            "/api/v1/positions", params={"trading_session_id": session_id}
        ).json()[0]
        assert position["ltp"] is None
        assert position["ltp_stale"] is None
        assert position["ltp_age_seconds"] is None
    finally:
        _cleanup_instrument_and_dependents(engine, instrument_id)


# -- _latest_ticks: N+1 fix (QC fix #3) ----------------------------------------


def test_latest_ticks_batches_every_contract_into_a_single_query(engine, seeded_admin):
    """`_latest_ticks` must issue exactly one SQL statement regardless of how
    many `option_contract_id`s are passed -- previously `_latest_ltp` ran
    once per open position inside `list_positions`'s own loop (an N+1 that
    scaled with open-position count). Also pins that `DISTINCT ON
    (option_contract_id) ... ORDER BY option_contract_id, ts DESC` actually
    picks the *latest* tick per contract, not an arbitrary one.
    """
    from app.api.v1.execution import _latest_ticks

    session_factory = sessionmaker(bind=engine, future=True)
    tag = uuid.uuid4().hex[:8]
    with session_factory() as db:
        instrument = Instrument(
            id=uuid.uuid4(),
            symbol=f"NIFTY-LTPBATCH-{tag}",
            exchange="NFO",
            lot_size=25,
            tick_size=0.05,
        )
        db.add(instrument)
        db.flush()

        now = datetime.now(UTC)
        contract_ids: list[uuid.UUID] = []
        for i in range(3):
            contract_id = uuid.uuid4()
            contract = OptionContract(
                id=contract_id,
                instrument_id=instrument.id,
                expiry_date=EXPIRY,
                strike=22000 + i * 100,
                option_type=OptionType.CE,
                symbol=f"NIFTY-LTPBATCH-{tag}-{i}",
            )
            db.add(contract)
            db.flush()
            # An older tick and a newer one per contract -- the batched
            # query must return the newer ltp, not just any row.
            db.add(
                QuoteTick(
                    id=uuid.uuid4(),
                    option_contract_id=contract_id,
                    ltp=10.0 + i,
                    bid=9.0,
                    ask=11.0,
                    volume=1,
                    ts=now - timedelta(seconds=30),
                )
            )
            db.add(
                QuoteTick(
                    id=uuid.uuid4(),
                    option_contract_id=contract_id,
                    ltp=20.0 + i,
                    bid=19.0,
                    ask=21.0,
                    volume=1,
                    ts=now,
                )
            )
            contract_ids.append(contract_id)
        db.commit()

        # contract_ids is a plain list of UUIDs captured before commit --
        # deliberately not `[c.id for c in contracts]` read after commit,
        # which would itself trigger one SELECT per object to refresh
        # attributes expired by commit (`expire_on_commit` default) and
        # pollute the query count this test is trying to pin.
        query_count = 0

        def _count(*_args, **_kwargs) -> None:
            nonlocal query_count
            query_count += 1

        event.listen(engine, "before_cursor_execute", _count)
        try:
            result = _latest_ticks(db, contract_ids)
        finally:
            event.remove(engine, "before_cursor_execute", _count)

        assert query_count == 1
        assert len(result) == 3
        for i, contract_id in enumerate(contract_ids):
            ltp, ts = result[contract_id]
            assert ltp == pytest.approx(20.0 + i)
            assert ts == now

        db.query(QuoteTick).filter(QuoteTick.option_contract_id.in_(contract_ids)).delete(
            synchronize_session=False
        )
        db.query(OptionContract).filter(OptionContract.instrument_id == instrument.id).delete(
            synchronize_session=False
        )
        db.query(Instrument).filter(Instrument.id == instrument.id).delete()
        db.commit()


def test_latest_ticks_with_no_ids_returns_empty_without_querying(engine):
    """The empty-input short circuit -- an all-closed-positions session must
    never issue a `WHERE option_contract_id IN ()` query at all.
    """
    from app.api.v1.execution import _latest_ticks

    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as db:
        assert _latest_ticks(db, []) == {}


# -- POST /positions/{id}/square-off: differentiated failure (QC fix #4) ------


def test_square_off_position_reports_unresolvable_option_contract_distinctly(
    api_client: TestClient, seeded_admin, engine, monkeypatch
):
    """`run_single_position_square_off` can fail two different ways -- an
    exit order that didn't fill synchronously (a normal timing outcome) and
    an unresolvable `option_contract_id` (a data-integrity problem) -- the
    endpoint used to show the same misleading "wait for reconciliation/
    retry" message for both. Simulated here by monkeypatching the function
    to raise the new, distinct exception -- against the pre-fix code this
    test can't even be constructed (`UnresolvableOptionContractError`
    didn't exist, and both failure modes collapsed into the same `None`
    return), which is itself the proof the two used to be indistinguishable.
    """
    _login(api_client, seeded_admin)
    session_id = api_client.post(
        "/api/v1/sessions", json={"broker_account_id": str(seeded_admin["broker_account_id"])}
    ).json()["id"]
    instrument_id = _dispatch_one_position(engine, seeded_admin, session_id)
    try:
        position_id = api_client.get(
            "/api/v1/positions", params={"trading_session_id": session_id}
        ).json()[0]["id"]

        bad_contract_id = uuid.uuid4()

        def _raise(*_args, **_kwargs):
            raise UnresolvableOptionContractError(bad_contract_id)

        monkeypatch.setattr(
            "app.api.v1.execution.run_single_position_square_off", _raise
        )

        resp = api_client.post(f"/api/v1/positions/{position_id}/square-off")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert body["reason"] == "unresolvable_option_contract"
        assert str(bad_contract_id) in body["detail"]
        # The old, generic message both failure reasons used to share --
        # must not appear here now that the two are distinguished.
        assert "exit order did not fill synchronously" not in body["detail"]

        # The endpoint reported the failure but never actually closed
        # anything -- the position is still open.
        positions_after = api_client.get(
            "/api/v1/positions", params={"trading_session_id": session_id}
        ).json()
        assert positions_after[0]["status"] == "open"

        session_factory = sessionmaker(bind=engine, future=True)
        with session_factory() as db:
            from app.domain.audit.models import AuditEvent

            event_row = (
                db.query(AuditEvent)
                .filter(
                    AuditEvent.workspace_id == seeded_admin["workspace_id"],
                    AuditEvent.event_type == "position.manual_square_off_requested",
                )
                .one()
            )
            assert event_row.payload["success"] is False
            assert event_row.payload["reason"] == "unresolvable_option_contract"
    finally:
        _cleanup_instrument_and_dependents(engine, instrument_id)


def test_square_off_position_reports_not_filled_synchronously_reason(
    api_client: TestClient, seeded_admin, engine, monkeypatch
):
    """The other, pre-existing failure reason (`outcome is None` --
    `close_position`'s own idempotent-no-op/unfilled-exit-order case) must
    keep its own distinct `reason` tag now that a second failure reason
    exists, not collapse into the same generic message.
    """
    _login(api_client, seeded_admin)
    session_id = api_client.post(
        "/api/v1/sessions", json={"broker_account_id": str(seeded_admin["broker_account_id"])}
    ).json()["id"]
    instrument_id = _dispatch_one_position(engine, seeded_admin, session_id)
    try:
        position_id = api_client.get(
            "/api/v1/positions", params={"trading_session_id": session_id}
        ).json()[0]["id"]

        monkeypatch.setattr(
            "app.api.v1.execution.run_single_position_square_off",
            lambda *_args, **_kwargs: None,
        )

        resp = api_client.post(f"/api/v1/positions/{position_id}/square-off")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert body["reason"] == "not_filled_synchronously"
        assert "reconciliation/retry" in body["detail"]
    finally:
        _cleanup_instrument_and_dependents(engine, instrument_id)


# ---------- trade-log-export / ws-quality-export (report downloads) ----------


def test_trade_log_export_requires_login(api_client: TestClient):
    assert api_client.get("/api/v1/reports/trade-log-export").status_code == 401


def test_trade_log_export_404s_when_no_workbook_exists_yet(
    api_client: TestClient, seeded_admin
):
    _login(api_client, seeded_admin)
    response = api_client.get("/api/v1/reports/trade-log-export")
    assert response.status_code == 404


def test_trade_log_export_streams_the_existing_workbook(
    api_client: TestClient, seeded_admin, tmp_path, monkeypatch
):
    import app.api.v1.reports as reports_api

    fake_reports_dir = tmp_path
    monkeypatch.setattr(reports_api, "REPORTS_DIR", fake_reports_dir)
    workspace_id = seeded_admin["workspace_id"]
    fake_path = fake_reports_dir / f"trade_log_{workspace_id}.xlsx"
    fake_path.write_bytes(b"not a real workbook, just proving the download plumbing works")

    _login(api_client, seeded_admin)
    response = api_client.get("/api/v1/reports/trade-log-export")

    assert response.status_code == 200
    assert response.content == fake_path.read_bytes()
    assert (
        response.headers["content-type"]
        == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert f"trade_log_{workspace_id}.xlsx" in response.headers["content-disposition"]


def test_ws_quality_export_requires_login(api_client: TestClient):
    assert api_client.get("/api/v1/reports/ws-quality-export").status_code == 401


def test_ws_quality_export_returns_header_only_csv_when_nothing_recorded(
    api_client: TestClient, seeded_admin
):
    _login(api_client, seeded_admin)
    response = api_client.get("/api/v1/reports/ws-quality-export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    lines = response.text.strip().splitlines()
    assert lines == ["recorded_at_ist,role,provider,symbol,connected,ltp,tick_ts_ist"]


def test_ws_quality_export_includes_this_workspaces_snapshots_only(
    api_client: TestClient, seeded_admin, engine
):
    import uuid as uuid_mod
    from datetime import UTC, datetime

    from app.domain.ops.models import MarketDataDiagnosticRun, MarketDataDiagnosticSnapshot

    session_factory = sessionmaker(bind=engine, future=True)
    other_workspace_id = uuid_mod.uuid4()
    run_id = uuid_mod.uuid4()
    other_run_id = uuid_mod.uuid4()
    now = datetime.now(UTC)

    with session_factory() as db:
        db.add(Workspace(id=other_workspace_id, name=f"other-ws-{other_workspace_id.hex[:8]}"))
        db.flush()
        db.add(
            MarketDataDiagnosticRun(
                id=run_id,
                workspace_id=seeded_admin["workspace_id"],
                role="default",
                provider="truedata",
                started_at=now,
                status="running",
            )
        )
        db.add(
            MarketDataDiagnosticRun(
                id=other_run_id,
                workspace_id=other_workspace_id,
                role="default",
                provider="truedata",
                started_at=now,
                status="running",
            )
        )
        db.commit()
        db.add(
            MarketDataDiagnosticSnapshot(
                id=uuid_mod.uuid4(),
                run_id=run_id,
                recorded_at=now,
                symbol="NIFTY",
                connected=True,
                ltp=24250.5,
                tick_ts=now,
            )
        )
        db.add(
            MarketDataDiagnosticSnapshot(
                id=uuid_mod.uuid4(),
                run_id=other_run_id,
                recorded_at=now,
                symbol="NIFTY",
                connected=True,
                ltp=99999.0,
                tick_ts=now,
            )
        )
        db.commit()

    try:
        _login(api_client, seeded_admin)
        # `on` is an IST calendar date (see the endpoint's own docstring),
        # not `now`'s own UTC date -- these diverge for ~5.5 hours a day
        # (midnight-5:30am IST), which is exactly the window this test
        # flaked in when run late at night: `now.date()` (UTC) landed on
        # the *previous* IST day, so the snapshot recorded "now" fell
        # outside the queried day's IST boundary the endpoint filters by.
        response = api_client.get(
            "/api/v1/reports/ws-quality-export",
            params={"on": now.astimezone(IST).date().isoformat()},
        )

        assert response.status_code == 200
        assert "24250.5" in response.text
        assert "99999" not in response.text
    finally:
        with session_factory() as cleanup_db:
            cleanup_db.query(MarketDataDiagnosticSnapshot).filter(
                MarketDataDiagnosticSnapshot.run_id.in_([run_id, other_run_id])
            ).delete(synchronize_session=False)
            cleanup_db.query(MarketDataDiagnosticRun).filter(
                MarketDataDiagnosticRun.id.in_([run_id, other_run_id])
            ).delete(synchronize_session=False)
            cleanup_db.query(Workspace).filter(Workspace.id == other_workspace_id).delete()
            cleanup_db.commit()
