"""`strategy_engine.sizing.resolve_qty_lots` — the mode-aware qty_lots default,
keyed on `is_strategy_routed_live` (not the vestigial `StrategyConfig.status`),
plus the explicit-`params["qty_lots"]`-always-wins contract.

Regression guard for the 2026-08-28 live bug: an `ema_micro_pullback` strategy
graduated to live via the session master switch (`SafeMode.LIVE_ENABLED`) kept
the 10-lot paper default while Risk Service gated it as live, so every signal
was rejected for `per_trade_lot_cap_exceeded`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.domain.identity.models import (
    BrokerAccount,
    BrokerAccountStatus,
    BrokerType,
    User,
)
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import (
    ExecutionMode,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    StrategyRuntimeMode,
)
from app.modules.strategy_engine.sizing import (
    DEFAULT_QTY_LOTS_LIVE,
    DEFAULT_QTY_LOTS_PAPER,
    resolve_qty_lots,
)


def _session(db: Session, workspace, user: User, mode: SafeMode) -> TradingSession:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label=f"siz-{uuid.uuid4().hex[:6]}",
        credentials_ref="config/credentials/shoonya.env",
        status=BrokerAccountStatus.ACTIVE,
    )
    db.add(account)
    db.flush()
    ts = TradingSession(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_account_id=account.id,
        started_by_user_id=user.id,
        mode=mode,
        started_at=datetime.now(UTC),
        budget_amount=100_000,
        daily_target_profit=5_000,
        daily_loss_cap=5_000,
        funding_mode=FundingMode.CASH,
    )
    db.add(ts)
    db.flush()
    return ts


def _config_and_run(
    db: Session,
    workspace,
    user: User,
    trading_session: TradingSession,
    *,
    params: dict | None = None,
    runtime_mode: StrategyRuntimeMode | None = None,
) -> tuple[StrategyConfig, StrategyRun]:
    config = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        name=f"siz-{uuid.uuid4().hex[:6]}",
        strategy_type="ema_micro_pullback",
        params=params or {},
        runtime_mode=runtime_mode,
    )
    db.add(config)
    db.flush()
    run = StrategyRun(
        id=uuid.uuid4(),
        strategy_config_id=config.id,
        trading_session_id=trading_session.id,
        execution_mode=ExecutionMode.AUTO,
        status=StrategyRunStatus.SCANNING,
        started_at=trading_session.started_at,
        started_by_user_id=user.id,
    )
    db.add(run)
    db.flush()
    return config, run


def test_paper_only_session_gets_the_paper_default(db, workspace, user):
    ts = _session(db, workspace, user, SafeMode.PAPER_ONLY)
    config, run = _config_and_run(db, workspace, user, ts)
    assert resolve_qty_lots(config, ts, run) == DEFAULT_QTY_LOTS_PAPER


def test_live_enabled_session_gets_the_live_default(db, workspace, user):
    ts = _session(db, workspace, user, SafeMode.LIVE_ENABLED)
    config, run = _config_and_run(db, workspace, user, ts)
    assert resolve_qty_lots(config, ts, run) == DEFAULT_QTY_LOTS_LIVE


def test_force_paper_strategy_in_a_live_session_gets_the_paper_default(db, workspace, user):
    ts = _session(db, workspace, user, SafeMode.LIVE_ENABLED)
    config, run = _config_and_run(
        db, workspace, user, ts, runtime_mode=StrategyRuntimeMode.FORCE_PAPER
    )
    assert resolve_qty_lots(config, ts, run) == DEFAULT_QTY_LOTS_PAPER


@pytest.mark.parametrize("mode", [SafeMode.PAPER_ONLY, SafeMode.LIVE_ENABLED])
def test_explicit_param_always_wins(db, workspace, user, mode):
    ts = _session(db, workspace, user, mode)
    config, run = _config_and_run(db, workspace, user, ts, params={"qty_lots": 4})
    assert resolve_qty_lots(config, ts, run) == 4


def test_no_session_falls_back_to_paper_default(db, workspace, user):
    ts = _session(db, workspace, user, SafeMode.LIVE_ENABLED)
    config, _ = _config_and_run(db, workspace, user, ts)
    assert resolve_qty_lots(config, None, None) == DEFAULT_QTY_LOTS_PAPER
