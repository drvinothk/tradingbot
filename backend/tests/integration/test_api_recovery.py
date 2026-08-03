"""Tests for the two new recovery-panel read endpoints — `GET /system-alerts`
(no read path existed for `system_alerts` before this, same gap `audit.py`/
`metrics.py` closed for their own tables) and
`GET /sessions/{id}/reconciliation-runs` (the manual `/reconcile` endpoint
only ever returns the single run it just performed, not history). Calls the
route functions directly, same reasoning as `test_api_audit.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.api.v1.sessions import list_reconciliation_runs
from app.api.v1.system_alerts import list_system_alerts
from app.domain.broker.models import BrokerSyncState, ReconciliationRun, ReconciliationTrigger
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.identity.models import Workspace as WorkspaceRow
from app.domain.market.models import Instrument, OptionContract, OptionType
from app.domain.ops.models import AlertSeverity, SystemAlert
from app.domain.session.models import FundingMode, SafeMode, TradingSession

EXPIRY = datetime(2026, 7, 30, tzinfo=UTC).date()


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="recovery-test-account",
        credentials_ref="config/credentials/shoonya.env",
        status=BrokerAccountStatus.ACTIVE,
    )
    db.add(account)
    db.flush()
    return account


@pytest.fixture
def trading_session(db: Session, workspace, broker_account, user: User) -> TradingSession:
    ts = TradingSession(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_account_id=broker_account.id,
        started_by_user_id=user.id,
        mode=SafeMode.PAPER_ONLY,
        started_at=datetime.now(UTC),
        budget_amount=1_000_000,
        daily_target_profit=1_000_000,
        daily_loss_cap=1_000_000,
        funding_mode=FundingMode.CASH,
    )
    db.add(ts)
    db.flush()
    return ts


@pytest.fixture
def option_contract(db: Session, workspace) -> OptionContract:
    instrument = Instrument(
        id=uuid.uuid4(), symbol="NIFTY", exchange="NFO", lot_size=25, tick_size=0.05
    )
    db.add(instrument)
    db.flush()
    contract = OptionContract(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        expiry_date=EXPIRY,
        strike=22000,
        option_type=OptionType.CE,
        symbol="NIFTY26JUL22000CE-RECOVERY",
    )
    db.add(contract)
    db.flush()
    return contract


def test_list_system_alerts_is_workspace_scoped(db: Session, workspace: WorkspaceRow, user: User):
    other_workspace = WorkspaceRow(id=uuid.uuid4(), name=f"other-{uuid.uuid4().hex[:8]}")
    db.add(other_workspace)
    db.flush()

    db.add(
        SystemAlert(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            severity=AlertSeverity.CRITICAL,
            category="test.mine",
            message="mine",
            created_at=datetime.now(UTC),
        )
    )
    db.add(
        SystemAlert(
            id=uuid.uuid4(),
            workspace_id=other_workspace.id,
            severity=AlertSeverity.CRITICAL,
            category="test.not_mine",
            message="not mine",
            created_at=datetime.now(UTC),
        )
    )
    db.flush()

    alerts = list_system_alerts(
        trading_session_id=None, is_resolved=None, limit=100, db=db, user=user
    )

    categories = {a.category for a in alerts}
    assert "test.mine" in categories
    assert "test.not_mine" not in categories


def test_list_system_alerts_filters_by_resolved(db: Session, workspace: WorkspaceRow, user: User):
    db.add(
        SystemAlert(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            severity=AlertSeverity.WARNING,
            category="test.open",
            message="open",
            created_at=datetime.now(UTC),
            is_resolved=False,
        )
    )
    db.add(
        SystemAlert(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            severity=AlertSeverity.WARNING,
            category="test.resolved",
            message="resolved",
            created_at=datetime.now(UTC),
            is_resolved=True,
        )
    )
    db.flush()

    unresolved = list_system_alerts(
        trading_session_id=None, is_resolved=False, limit=100, db=db, user=user
    )

    assert {a.category for a in unresolved} == {"test.open"}


def test_list_reconciliation_runs_returns_history_and_current_mismatches(
    db: Session, workspace, user: User, trading_session, option_contract
):
    now = datetime.now(UTC)
    db.add(
        ReconciliationRun(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            trading_session_id=trading_session.id,
            trigger_type=ReconciliationTrigger.EVENT,
            mismatches_found=1,
            action_taken="alert_raised",
            started_at=now,
            finished_at=now,
        )
    )
    db.add(
        BrokerSyncState(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            trading_session_id=trading_session.id,
            option_contract_id=option_contract.id,
            local_qty=25,
            broker_qty=0,
            is_mismatched=True,
            checked_at=now,
        )
    )
    db.flush()

    result = list_reconciliation_runs(
        session_id=trading_session.id, limit=20, db=db, user=user
    )

    assert len(result.runs) == 1
    assert result.runs[0].mismatches_found == 1
    assert len(result.current_mismatches) == 1
    assert result.current_mismatches[0].option_contract_id == option_contract.id


def test_list_reconciliation_runs_excludes_non_mismatched_sync_states(
    db: Session, workspace, user: User, trading_session, option_contract
):
    db.add(
        BrokerSyncState(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            trading_session_id=trading_session.id,
            option_contract_id=option_contract.id,
            local_qty=25,
            broker_qty=25,
            is_mismatched=False,
            checked_at=datetime.now(UTC),
        )
    )
    db.flush()

    result = list_reconciliation_runs(
        session_id=trading_session.id, limit=20, db=db, user=user
    )

    assert result.current_mismatches == []
