"""`latest_snapshot_tick` — the per-contract price lookup
`execution_engine.paper.service.current_contract_price` relies on to price
paper fills/checks from the same REST `OptionChainSnapshot` data a
strategy's own `rank_from_latest_snapshot` already read, instead of
`MockBrokerAdapter`'s independent synthetic price. Requires a real Postgres
(the shared `db` fixture), same reasoning as every other integration test
in this suite that reads/writes `option_chain_snapshots`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.domain.market.models import Instrument, OptionChainSnapshot, OptionContract, OptionType
from app.modules.market_data.freshness import latest_snapshot_tick

EXPIRY = date(2026, 7, 30)


def _instrument(db: Session) -> Instrument:
    inst = Instrument(
        id=uuid.uuid4(), symbol="NIFTY-FRESH", exchange="NFO", lot_size=25, tick_size=0.05
    )
    db.add(inst)
    db.flush()
    return inst


def _option_contract(db: Session, instrument: Instrument, symbol: str) -> OptionContract:
    contract = OptionContract(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        expiry_date=EXPIRY,
        strike=22000,
        option_type=OptionType.CE,
        symbol=symbol,
    )
    db.add(contract)
    db.flush()
    return contract


def test_returns_none_when_no_snapshot_exists_yet(db: Session):
    instrument = _instrument(db)
    contract = _option_contract(db, instrument, "NIFTY26JUL22000CE-FRESH1")

    result = latest_snapshot_tick(db, instrument.id, EXPIRY, contract.symbol)

    assert result is None


def test_returns_none_when_contract_is_not_in_the_snapshot(db: Session):
    instrument = _instrument(db)
    contract = _option_contract(db, instrument, "NIFTY26JUL22000CE-FRESH2")
    db.add(
        OptionChainSnapshot(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            expiry_date=EXPIRY,
            ts=datetime.now(UTC),
            chain_data=[
                {"contract_symbol": "SOME-OTHER-SYMBOL", "ltp": 50.0, "bid": 49.5, "ask": 50.5}
            ],
        )
    )
    db.flush()

    result = latest_snapshot_tick(db, instrument.id, EXPIRY, contract.symbol)

    assert result is None


def test_finds_the_contracts_price_in_the_latest_snapshot(db: Session):
    instrument = _instrument(db)
    contract = _option_contract(db, instrument, "NIFTY26JUL22000CE-FRESH3")
    db.add(
        OptionChainSnapshot(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            expiry_date=EXPIRY,
            ts=datetime.now(UTC),
            chain_data=[
                {
                    "contract_symbol": contract.symbol,
                    "strike": 22000.0,
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

    tick = latest_snapshot_tick(db, instrument.id, EXPIRY, contract.symbol)

    assert tick is not None
    assert tick.contract_symbol == contract.symbol
    assert tick.ltp == 80.0
    assert tick.bid == 79.5
    assert tick.ask == 80.5


def test_uses_the_most_recent_snapshot_when_more_than_one_exists(db: Session):
    instrument = _instrument(db)
    contract = _option_contract(db, instrument, "NIFTY26JUL22000CE-FRESH4")
    db.add(
        OptionChainSnapshot(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            expiry_date=EXPIRY,
            ts=datetime(2026, 7, 1, tzinfo=UTC),
            chain_data=[
                {"contract_symbol": contract.symbol, "ltp": 10.0, "bid": 9.5, "ask": 10.5}
            ],
        )
    )
    db.add(
        OptionChainSnapshot(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            expiry_date=EXPIRY,
            ts=datetime(2026, 7, 2, tzinfo=UTC),
            chain_data=[
                {"contract_symbol": contract.symbol, "ltp": 20.0, "bid": 19.5, "ask": 20.5}
            ],
        )
    )
    db.flush()

    tick = latest_snapshot_tick(db, instrument.id, EXPIRY, contract.symbol)

    assert tick is not None
    assert tick.ltp == 20.0
