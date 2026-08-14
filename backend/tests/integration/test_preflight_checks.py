"""Ops-Hardening Phase 5: broker_adapter.preflight.run_preflight_checks --
connectivity, freshness, and margin gates run immediately before any real
place_order call.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.orm import Session

from app.domain.market.models import Instrument, OptionChainSnapshot, OptionContract, OptionType
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.modules.broker_adapter import composition
from app.modules.broker_adapter.base.contracts import MarginInfo
from app.modules.broker_adapter.base.errors import BrokerConnectivityError, ConfigurationError
from app.modules.broker_adapter.preflight import run_preflight_checks
from tests.unit.test_broker_composition import _FakeRealBroker

EXPIRY = date(2026, 8, 18)


@pytest.fixture(autouse=True)
def _reset_broker():
    composition.reset_for_tests()
    yield
    composition.reset_for_tests()


@pytest.fixture
def instrument(db: Session) -> Instrument:
    inst = Instrument(id=uuid.uuid4(), symbol="NIFTY", exchange="NFO", lot_size=25, tick_size=0.05)
    db.add(inst)
    db.flush()
    return inst


@pytest.fixture
def option_contract(db: Session, instrument: Instrument) -> OptionContract:
    contract = OptionContract(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        expiry_date=EXPIRY,
        strike=24000,
        option_type=OptionType.CE,
        symbol="NIFTY18AUG26C24000",
    )
    db.add(contract)
    db.flush()
    return contract


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


def _fresh_chain_snapshot(
    db: Session, instrument: Instrument, option_contract: OptionContract
) -> None:
    db.add(
        OptionChainSnapshot(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            expiry_date=EXPIRY,
            ts=datetime.now(UTC),
            chain_data=[
                {
                    "contract_symbol": option_contract.symbol,
                    "strike": float(option_contract.strike),
                    "option_type": "CE",
                    "ltp": 80.0,
                    "bid": 79.5,
                    "ask": 80.5,
                    "volume": 5000,
                    "oi": 20000,
                }
            ],
        )
    )
    db.flush()


def test_raises_when_shoonya_not_connected(db, trading_session, option_contract):
    with pytest.raises(ConfigurationError, match="not connected"):
        run_preflight_checks(
            db, _FakeRealBroker(), trading_session=trading_session, option_contract=option_contract
        )


def test_raises_when_option_chain_is_stale(db, trading_session, instrument, option_contract):
    composition.set_broker(_FakeRealBroker())
    # No OptionChainSnapshot seeded at all -> classify_option_chain returns DEAD.

    with pytest.raises(ConfigurationError, match="dead"):
        run_preflight_checks(
            db, _FakeRealBroker(), trading_session=trading_session, option_contract=option_contract
        )


def test_raises_when_margin_call_fails(db, trading_session, instrument, option_contract):
    composition.set_broker(_FakeRealBroker())
    _fresh_chain_snapshot(db, instrument, option_contract)
    broker = _FakeRealBroker()
    broker.margin_raises = BrokerConnectivityError("timeout")

    with pytest.raises(ConfigurationError, match="could not confirm margin"):
        run_preflight_checks(
            db, broker, trading_session=trading_session, option_contract=option_contract
        )


def test_raises_when_no_available_margin(
    db, trading_session, instrument, option_contract, monkeypatch
):
    composition.set_broker(_FakeRealBroker())
    _fresh_chain_snapshot(db, instrument, option_contract)
    broker = _FakeRealBroker()
    monkeypatch.setattr(broker, "get_margin", lambda: MarginInfo(0.0, 0.0, 0.0, datetime.now(UTC)))

    with pytest.raises(ConfigurationError, match="no available margin"):
        run_preflight_checks(
            db, broker, trading_session=trading_session, option_contract=option_contract
        )


def test_passes_when_everything_is_healthy(
    db, trading_session, instrument, option_contract, monkeypatch
):
    composition.set_broker(_FakeRealBroker())
    _fresh_chain_snapshot(db, instrument, option_contract)
    broker = _FakeRealBroker()
    monkeypatch.setattr(
        broker, "get_margin", lambda: MarginInfo(50_000.0, 0.0, 50_000.0, datetime.now(UTC))
    )

    run_preflight_checks(
        db, broker, trading_session=trading_session, option_contract=option_contract
    )
