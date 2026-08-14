"""Ops-Hardening Phase 6: strategy_engine.auto_spawner -- trading-day guard,
DB-only nearest-expiry resolution + DTE guard, and idempotent StrategyRun
creation for every is_enabled StrategyConfig. Deliberately does not assert
anything about runner threads/ingestion -- see the module's own docstring
for why that's `_resume_strategy_runners`'s job, covered elsewhere.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

import app.modules.strategy_engine.auto_spawner as auto_spawner_module
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.market.models import Instrument, OptionContract, OptionType
from app.domain.ops.models import SystemAlert
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import StrategyConfig, StrategyRun, StrategyRunStatus
from app.modules.broker_adapter.base.errors import BrokerConnectivityError
from app.modules.strategy_engine.auto_spawner import (
    resolve_nearest_expiry,
    spawn_enabled_strategies,
)

TODAY = date(2026, 8, 18)  # a real Tuesday -- see test_market_utils.py
SATURDAY = date(2026, 8, 15)


@pytest.fixture(autouse=True)
def _fake_snapshot_and_broker(monkeypatch):
    """`record_option_chain_snapshot`/`get_broker` default to the real
    `session_scope`-bound production DB/broker singleton -- same "never let
    a background write path touch prod inside a test" discipline as every
    other phase (see api.v1.strategies's own `fake_runner` fixture for the
    identical pattern this mirrors). `is_shoonya_market_data_ready` defaults
    True here (matches "always True for mock/angel_one" in production) so
    tests opt in to the not-ready branch explicitly.
    """
    calls: list[tuple[uuid.UUID, str, date]] = []

    def _fake_record(instrument_id, broker, symbol, expiry):
        calls.append((instrument_id, symbol, expiry))

    monkeypatch.setattr(auto_spawner_module, "record_option_chain_snapshot", _fake_record)
    monkeypatch.setattr(auto_spawner_module, "get_broker", lambda: object())
    monkeypatch.setattr(auto_spawner_module, "is_shoonya_market_data_ready", lambda: True)
    return calls


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="auto-spawn-test-account",
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
        budget_amount=100_000,
        daily_target_profit=5_000,
        daily_loss_cap=5_000,
        funding_mode=FundingMode.CASH,
    )
    db.add(ts)
    db.flush()
    return ts


@pytest.fixture
def nifty(db: Session) -> Instrument:
    inst = Instrument(id=uuid.uuid4(), symbol="NIFTY", exchange="NFO", lot_size=25, tick_size=0.05)
    db.add(inst)
    db.flush()
    return inst


def _contract(db: Session, instrument: Instrument, expiry: date, strike: float, is_active=True):
    contract = OptionContract(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        expiry_date=expiry,
        strike=strike,
        option_type=OptionType.CE,
        symbol=f"NIFTY{expiry.strftime('%d%b%y').upper()}C{int(strike)}-{uuid.uuid4().hex[:4]}",
        is_active=is_active,
    )
    db.add(contract)
    db.flush()
    return contract


def _alerts_for(db: Session, trading_session: TradingSession) -> list[SystemAlert]:
    return (
        db.query(SystemAlert)
        .filter(SystemAlert.trading_session_id == trading_session.id)
        .all()
    )


def _enabled_config(db: Session, workspace, *, name="orb-nifty", underlying_symbol="NIFTY"):
    config = StrategyConfig(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        name=name,
        strategy_type="orb",
        is_enabled=True,
        underlying_symbol=underlying_symbol,
    )
    db.add(config)
    db.flush()
    return config


# -- resolve_nearest_expiry ---------------------------------------------


def test_resolve_nearest_expiry_picks_the_soonest_active_future_expiry(db, nifty):
    _contract(db, nifty, date(2026, 8, 11), 24000, is_active=False)  # past
    _contract(db, nifty, date(2026, 8, 25), 24100)
    near = _contract(db, nifty, date(2026, 8, 18), 24000)

    result = resolve_nearest_expiry(db, nifty.id, TODAY)

    assert result == date(2026, 8, 18)
    assert near.expiry_date == result


def test_resolve_nearest_expiry_ignores_inactive_contracts(db, nifty):
    _contract(db, nifty, date(2026, 8, 18), 24000, is_active=False)
    _contract(db, nifty, date(2026, 8, 25), 24100, is_active=True)

    assert resolve_nearest_expiry(db, nifty.id, TODAY) == date(2026, 8, 25)


def test_resolve_nearest_expiry_returns_none_when_nothing_active(db, nifty):
    _contract(db, nifty, date(2026, 8, 18), 24000, is_active=False)

    assert resolve_nearest_expiry(db, nifty.id, TODAY) is None


# -- spawn_enabled_strategies ---------------------------------------------


def test_weekend_skips_spawn_entirely(db, workspace, trading_session):
    _enabled_config(db, workspace)

    spawn_enabled_strategies(db, trading_session, SATURDAY)

    assert db.query(StrategyRun).count() == 0


def test_no_enabled_configs_is_a_noop(db, workspace, trading_session):
    spawn_enabled_strategies(db, trading_session, TODAY)

    assert db.query(StrategyRun).count() == 0


def test_spawns_a_run_for_an_enabled_config_with_valid_expiry(
    db, workspace, trading_session, nifty, _fake_snapshot_and_broker
):
    _contract(db, nifty, date(2026, 8, 20), 24000)
    config = _enabled_config(db, workspace)

    spawn_enabled_strategies(db, trading_session, TODAY)

    run = db.query(StrategyRun).filter(StrategyRun.strategy_config_id == config.id).one()
    assert run.status == StrategyRunStatus.SCANNING
    assert run.instrument_id == nifty.id
    assert run.expiry_date == date(2026, 8, 20)
    assert run.execution_mode == "auto"
    assert run.started_by_user_id == trading_session.started_by_user_id
    assert len(_fake_snapshot_and_broker) == 1


def test_idempotent_skips_when_an_active_run_already_exists(
    db, workspace, trading_session, nifty, _fake_snapshot_and_broker
):
    _contract(db, nifty, date(2026, 8, 20), 24000)
    config = _enabled_config(db, workspace)
    db.add(
        StrategyRun(
            id=uuid.uuid4(),
            strategy_config_id=config.id,
            trading_session_id=trading_session.id,
            execution_mode="auto",
            status=StrategyRunStatus.SCANNING,
            started_at=datetime.now(UTC),
            started_by_user_id=trading_session.started_by_user_id,
        )
    )
    db.flush()

    spawn_enabled_strategies(db, trading_session, TODAY)

    assert db.query(StrategyRun).filter(StrategyRun.strategy_config_id == config.id).count() == 1
    assert len(_fake_snapshot_and_broker) == 0


def test_missing_underlying_symbol_alerts_and_skips(db, workspace, trading_session):
    config = _enabled_config(db, workspace, underlying_symbol=None)

    spawn_enabled_strategies(db, trading_session, TODAY)

    assert db.query(StrategyRun).filter(StrategyRun.strategy_config_id == config.id).count() == 0
    alerts = _alerts_for(db, trading_session)
    assert len(alerts) == 1
    assert alerts[0].category == "auto_spawn_no_underlying"


def test_unknown_instrument_alerts_and_skips(db, workspace, trading_session):
    config = _enabled_config(db, workspace, underlying_symbol="DOESNOTEXIST")

    spawn_enabled_strategies(db, trading_session, TODAY)

    assert db.query(StrategyRun).filter(StrategyRun.strategy_config_id == config.id).count() == 0
    assert _alerts_for(db, trading_session)[0].category == "auto_spawn_unknown_instrument"


def test_no_active_contracts_alerts_and_skips(db, workspace, trading_session, nifty):
    config = _enabled_config(db, workspace)

    spawn_enabled_strategies(db, trading_session, TODAY)

    assert db.query(StrategyRun).filter(StrategyRun.strategy_config_id == config.id).count() == 0
    assert _alerts_for(db, trading_session)[0].category == "auto_spawn_no_expiry"


def test_dte_exceeding_max_alerts_and_skips(db, workspace, trading_session, nifty):
    _contract(db, nifty, date(2026, 9, 15), 24000)  # far monthly expiry, DTE > 7
    config = _enabled_config(db, workspace)

    spawn_enabled_strategies(db, trading_session, TODAY)

    assert db.query(StrategyRun).filter(StrategyRun.strategy_config_id == config.id).count() == 0
    assert _alerts_for(db, trading_session)[0].category == "auto_spawn_dte_exceeded"


def test_dte_exactly_at_boundary_is_allowed(
    db, workspace, trading_session, nifty, _fake_snapshot_and_broker
):
    _contract(db, nifty, date(2026, 8, 25), 24000)  # DTE == 7
    config = _enabled_config(db, workspace)

    spawn_enabled_strategies(db, trading_session, TODAY)

    assert db.query(StrategyRun).filter(StrategyRun.strategy_config_id == config.id).count() == 1


def test_broker_error_on_snapshot_alerts_and_creates_no_run(
    db, workspace, trading_session, nifty, monkeypatch
):
    _contract(db, nifty, date(2026, 8, 20), 24000)
    config = _enabled_config(db, workspace)

    def _raise(*args, **kwargs):
        raise BrokerConnectivityError("timeout")

    monkeypatch.setattr(auto_spawner_module, "record_option_chain_snapshot", _raise)
    monkeypatch.setattr(auto_spawner_module, "get_broker", lambda: object())
    monkeypatch.setattr(auto_spawner_module, "is_shoonya_market_data_ready", lambda: True)

    spawn_enabled_strategies(db, trading_session, TODAY)

    assert db.query(StrategyRun).filter(StrategyRun.strategy_config_id == config.id).count() == 0
    assert _alerts_for(db, trading_session)[0].category == "auto_spawn_broker_error"


def test_shoonya_not_ready_still_spawns_idle_without_a_snapshot_call(
    db, workspace, trading_session, nifty, monkeypatch
):
    _contract(db, nifty, date(2026, 8, 20), 24000)
    config = _enabled_config(db, workspace)

    calls: list[object] = []
    monkeypatch.setattr(
        auto_spawner_module, "record_option_chain_snapshot", lambda *a, **kw: calls.append(a)
    )
    monkeypatch.setattr(auto_spawner_module, "is_shoonya_market_data_ready", lambda: False)

    spawn_enabled_strategies(db, trading_session, TODAY)

    assert db.query(StrategyRun).filter(StrategyRun.strategy_config_id == config.id).count() == 1
    assert calls == []


def test_one_snapshot_call_shared_across_configs_on_the_same_underlying_and_expiry(
    db, workspace, trading_session, nifty, _fake_snapshot_and_broker
):
    _contract(db, nifty, date(2026, 8, 20), 24000)
    _enabled_config(db, workspace, name="orb-nifty")
    _enabled_config(db, workspace, name="vwap-nifty")

    spawn_enabled_strategies(db, trading_session, TODAY)

    assert db.query(StrategyRun).count() == 2
    assert len(_fake_snapshot_and_broker) == 1


def test_one_bad_config_does_not_block_others(
    db, workspace, trading_session, nifty, _fake_snapshot_and_broker
):
    _contract(db, nifty, date(2026, 8, 20), 24000)
    bad = _enabled_config(db, workspace, name="bad-config", underlying_symbol=None)
    good = _enabled_config(db, workspace, name="good-config")

    spawn_enabled_strategies(db, trading_session, TODAY)

    assert db.query(StrategyRun).filter(StrategyRun.strategy_config_id == bad.id).count() == 0
    assert db.query(StrategyRun).filter(StrategyRun.strategy_config_id == good.id).count() == 1
