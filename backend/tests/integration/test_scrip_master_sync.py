"""`ScripMasterService.sync_to_db` — the DB-backed half of the Angel One
symbol/token mapping (see `test_scrip_master.py` for the pure-function
parser/mapper tests). Seeds `Instrument`/`OptionContract` rows the same way
`test_instrument_sync.py` does (via `build_mock_universe`, through a real
`sync_instrument_master` call against the mock adapter) so this exercises
the exact same rows the sync matches against in production.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.domain.market.mock_universe import build_mock_universe
from app.domain.market.models import (
    BrokerSymbolMap,
    Instrument,
    MarketDataProviderName,
    OptionContract,
    ScripMasterSyncLog,
    SyncStatus,
)
from app.modules.broker_adapter.mock import MockBrokerAdapter
from app.modules.market_data.scrip_master import ScripMasterService
from app.modules.scheduler import sync_instrument_master

# Must stay in the future — sync_to_db only matches *active* OptionContract
# rows, and instrument_sync's own expiry sweep deactivates anything already
# past its expiry the moment it's synced.
EXPIRY = date.today() + timedelta(days=45)


def _seed_instruments_and_contracts(db: Session) -> None:
    broker = MockBrokerAdapter(instruments=build_mock_universe(EXPIRY), seed=1)
    sync_instrument_master(db, broker, exchanges=["NFO"])
    db.flush()


def _angel_row_for(nifty_contract: OptionContract, token: str) -> dict:
    return {
        "token": token,
        "symbol": f"NIFTY{EXPIRY.strftime('%d%b%y').upper()}{int(nifty_contract.strike)}"
        f"{nifty_contract.option_type}",
        "name": "NIFTY",
        "expiry": EXPIRY.strftime("%d%b%Y").upper(),
        "strike": f"{int(nifty_contract.strike) * 100}.000000",
        "lotsize": "75",
        "instrumenttype": "OPTIDX",
        "exch_seg": "NFO",
        "tick_size": "0.05",
    }


def _index_row(token: str = "26000") -> dict:
    return {
        "token": token,
        "symbol": "Nifty 50",
        "name": "NIFTY",
        "expiry": "",
        "strike": "-1.000000",
        "lotsize": "1",
        "instrumenttype": "",
        "exch_seg": "NSE",
        "tick_size": "0.05",
    }


def test_sync_matches_an_existing_option_contract_structurally(db: Session, monkeypatch):
    _seed_instruments_and_contracts(db)
    nifty = db.query(Instrument).filter(Instrument.symbol == "NIFTY").one()
    contract = (
        db.query(OptionContract)
        .filter(OptionContract.instrument_id == nifty.id, OptionContract.expiry_date == EXPIRY)
        .first()
    )
    assert contract is not None

    service = ScripMasterService()
    monkeypatch.setattr(
        service, "_download", lambda: [_index_row(), _angel_row_for(contract, "99001")]
    )
    service.fetch_and_parse()

    log = service.sync_to_db(db)

    assert log.status == SyncStatus.SUCCESS
    assert log.rows_mapped >= 1

    mapping = (
        db.query(BrokerSymbolMap)
        .filter(
            BrokerSymbolMap.option_contract_id == contract.id,
            BrokerSymbolMap.provider == MarketDataProviderName.ANGEL_ONE,
        )
        .one()
    )
    assert mapping.external_token == "99001"
    assert service.get_angel_token(contract.symbol) == "99001"


def test_sync_maps_the_underlying_index_row(db: Session, monkeypatch):
    _seed_instruments_and_contracts(db)
    nifty = db.query(Instrument).filter(Instrument.symbol == "NIFTY").one()

    service = ScripMasterService()
    monkeypatch.setattr(service, "_download", lambda: [_index_row("26000")])
    service.fetch_and_parse()
    service.sync_to_db(db)

    mapping = (
        db.query(BrokerSymbolMap)
        .filter(
            BrokerSymbolMap.instrument_id == nifty.id,
            BrokerSymbolMap.provider == MarketDataProviderName.ANGEL_ONE,
        )
        .one()
    )
    assert mapping.external_token == "26000"
    assert service.get_angel_token("NIFTY") == "26000"


def test_sync_also_writes_a_shoonya_passthrough_mapping(db: Session, monkeypatch):
    """Our own DB symbol already *is* the Shoonya tsym — this passthrough
    row is written for interface symmetry (a future execution-broker swap
    reuses this same mechanism), not because Shoonya needs translating today.
    """
    _seed_instruments_and_contracts(db)
    nifty = db.query(Instrument).filter(Instrument.symbol == "NIFTY").one()
    contract = (
        db.query(OptionContract).filter(OptionContract.instrument_id == nifty.id).first()
    )
    assert contract is not None

    service = ScripMasterService()
    monkeypatch.setattr(service, "_download", lambda: [])  # no Angel rows at all this run
    service.fetch_and_parse()
    service.sync_to_db(db)

    shoonya_row = (
        db.query(BrokerSymbolMap)
        .filter(
            BrokerSymbolMap.option_contract_id == contract.id,
            BrokerSymbolMap.provider == MarketDataProviderName.SHOONYA,
        )
        .one()
    )
    assert shoonya_row.external_symbol == contract.symbol
    assert service.get_shoonya_tsym(contract.symbol) == contract.symbol


def test_rerunning_sync_is_a_clean_upsert_no_duplicate_key_errors(db: Session, monkeypatch):
    _seed_instruments_and_contracts(db)
    nifty = db.query(Instrument).filter(Instrument.symbol == "NIFTY").one()
    contract = db.query(OptionContract).filter(OptionContract.instrument_id == nifty.id).first()
    assert contract is not None

    service = ScripMasterService()
    monkeypatch.setattr(
        service, "_download", lambda: [_index_row(), _angel_row_for(contract, "99001")]
    )
    service.fetch_and_parse()
    service.sync_to_db(db)
    db.flush()

    # A second run with a changed token for the same contract must update
    # the existing row, not violate the (option_contract_id, provider)
    # unique constraint by inserting a second one.
    monkeypatch.setattr(
        service, "_download", lambda: [_index_row(), _angel_row_for(contract, "99002")]
    )
    service.fetch_and_parse()
    second_log = service.sync_to_db(db)

    assert second_log.status == SyncStatus.SUCCESS
    rows = (
        db.query(BrokerSymbolMap)
        .filter(
            BrokerSymbolMap.option_contract_id == contract.id,
            BrokerSymbolMap.provider == MarketDataProviderName.ANGEL_ONE,
        )
        .all()
    )
    assert len(rows) == 1
    assert rows[0].external_token == "99002"


def test_sync_always_records_a_log_row_even_with_nothing_to_map(db: Session, monkeypatch):
    service = ScripMasterService()
    monkeypatch.setattr(service, "_download", lambda: [])
    service.fetch_and_parse()

    log = service.sync_to_db(db)

    assert log.status == SyncStatus.SUCCESS
    assert log.rows_mapped == 0
    assert db.query(ScripMasterSyncLog).count() == 1
