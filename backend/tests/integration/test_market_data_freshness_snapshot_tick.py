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
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from app.domain.market.models import (
    Instrument,
    OptionChainSnapshot,
    OptionContract,
    OptionType,
    QuoteTick,
)
from app.modules.market_data.freshness import fresh_reference_premium, latest_snapshot_tick

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


# -- fresh_reference_premium -------------------------------------------------


def _snapshot(db: Session, instrument, contract, *, ltp: float, age: timedelta) -> None:
    db.add(
        OptionChainSnapshot(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            expiry_date=EXPIRY,
            ts=datetime.now(UTC) - age,
            chain_data=[{"contract_symbol": contract.symbol, "ltp": ltp, "bid": ltp, "ask": ltp}],
        )
    )
    db.flush()


def _quote_tick(db: Session, contract, *, ltp: float, age: timedelta) -> None:
    db.add(
        QuoteTick(
            id=uuid.uuid4(),
            option_contract_id=contract.id,
            ltp=ltp,
            bid=ltp,
            ask=ltp,
            volume=100,
            oi=1000,
            ts=datetime.now(UTC) - age,
        )
    )
    db.flush()


def _ref(db, instrument, contract):
    return fresh_reference_premium(
        db,
        option_contract_id=contract.id,
        instrument_id=instrument.id,
        expiry_date=EXPIRY,
        contract_symbol=contract.symbol,
    )


def test_prefers_a_fresh_streamed_tick(db: Session):
    instrument = _instrument(db)
    contract = _option_contract(db, instrument, "NIFTY26JUL22000CE-REF1")
    _quote_tick(db, contract, ltp=91.0, age=timedelta(seconds=5))
    _snapshot(db, instrument, contract, ltp=80.0, age=timedelta(seconds=30))

    assert _ref(db, instrument, contract) == 91.0


def test_falls_back_to_the_snapshot_when_the_tick_is_stale(db: Session):
    instrument = _instrument(db)
    contract = _option_contract(db, instrument, "NIFTY26JUL22000CE-REF2")
    _quote_tick(db, contract, ltp=999.0, age=timedelta(hours=4))
    _snapshot(db, instrument, contract, ltp=80.0, age=timedelta(seconds=30))

    assert _ref(db, instrument, contract) == 80.0


def test_returns_none_when_neither_tick_nor_snapshot_is_fresh(db: Session):
    instrument = _instrument(db)
    contract = _option_contract(db, instrument, "NIFTY26JUL22000CE-REF3")
    _quote_tick(db, contract, ltp=999.0, age=timedelta(hours=4))
    _snapshot(db, instrument, contract, ltp=80.0, age=timedelta(hours=4))

    assert _ref(db, instrument, contract) is None


def test_ignores_a_zero_priced_tick_and_uses_the_snapshot(db: Session):
    instrument = _instrument(db)
    contract = _option_contract(db, instrument, "NIFTY26JUL22000CE-REF4")
    _quote_tick(db, contract, ltp=0.0, age=timedelta(seconds=5))
    _snapshot(db, instrument, contract, ltp=80.0, age=timedelta(seconds=30))

    assert _ref(db, instrument, contract) == 80.0
