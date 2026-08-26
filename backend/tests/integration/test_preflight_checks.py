"""Ops-Hardening Phase 5: broker_adapter.preflight.run_preflight_checks --
connectivity and margin gates run immediately before any real place_order
call. The option-chain freshness gate that used to live here moved to
`execution_engine.paper.service._raise_if_option_chain_stale` 2026-08-26
(see that module's own test file for its coverage) -- this module no
longer touches `market_data` at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.modules.broker_adapter import composition
from app.modules.broker_adapter.base.contracts import MarginInfo
from app.modules.broker_adapter.base.errors import BrokerConnectivityError, ConfigurationError
from app.modules.broker_adapter.preflight import run_preflight_checks
from tests.unit.test_broker_composition import _FakeRealBroker


@pytest.fixture(autouse=True)
def _reset_broker():
    composition.reset_for_tests()
    yield
    composition.reset_for_tests()


@pytest.fixture
def trading_session(workspace) -> TradingSession:
    return TradingSession(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        mode=SafeMode.LIVE_ENABLED,
        started_at=datetime.now(UTC),
        budget_amount=100_000,
        daily_target_profit=5_000,
        daily_loss_cap=5_000,
        funding_mode=FundingMode.CASH,
    )


def test_raises_when_shoonya_not_connected(trading_session):
    with pytest.raises(ConfigurationError, match="not connected"):
        run_preflight_checks(_FakeRealBroker(), trading_session=trading_session)


def test_raises_when_margin_call_fails(trading_session):
    composition.set_broker(_FakeRealBroker())
    broker = _FakeRealBroker()
    broker.margin_raises = BrokerConnectivityError("timeout")

    with pytest.raises(ConfigurationError, match="could not confirm margin"):
        run_preflight_checks(broker, trading_session=trading_session)


def test_raises_when_no_available_margin(trading_session, monkeypatch):
    composition.set_broker(_FakeRealBroker())
    broker = _FakeRealBroker()
    monkeypatch.setattr(broker, "get_margin", lambda: MarginInfo(0.0, 0.0, 0.0, datetime.now(UTC)))

    with pytest.raises(ConfigurationError, match="no available margin"):
        run_preflight_checks(broker, trading_session=trading_session)


def test_passes_when_everything_is_healthy(trading_session, monkeypatch):
    composition.set_broker(_FakeRealBroker())
    broker = _FakeRealBroker()
    monkeypatch.setattr(
        broker, "get_margin", lambda: MarginInfo(50_000.0, 0.0, 50_000.0, datetime.now(UTC))
    )

    run_preflight_checks(broker, trading_session=trading_session)
