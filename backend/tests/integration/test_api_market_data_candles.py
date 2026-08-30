"""GET /market-data/candles and GET /market-data/streaming-symbols (Market
Terminal live chart, 2026-08-30) — read-only against `price_bars` /
`market_data.registry`'s subscribed-symbols set. Exercised as direct
function calls (same lighter-weight style `test_running_strategies_is_live
.py` already uses for `list_running_strategies`), not full HTTP+auth, since
the permission wiring itself (`require_permission("strategy.view")`) is the
identical, already-covered mechanism `/strategies/running` and `/instruments`
use — the real risk surface here is the query/serialization logic.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.api.v1.market_data import get_candles, get_streaming_symbols
from app.domain.market.models import Instrument, PriceBar
from app.modules.market_data import registry as market_data_registry


def _instrument(db: Session) -> Instrument:
    inst = Instrument(id=uuid.uuid4(), symbol="NIFTY", exchange="NFO", lot_size=25, tick_size=0.05)
    db.add(inst)
    db.flush()
    return inst


def test_get_candles_returns_bars_in_chronological_order(db: Session, user):
    instrument = _instrument(db)
    base = datetime(2026, 7, 30, 9, 15, tzinfo=UTC)
    for i in range(3):
        db.add(
            PriceBar(
                id=uuid.uuid4(),
                instrument_id=instrument.id,
                timeframe="60s",
                bucket_start=base + timedelta(minutes=i),
                open=100 + i,
                high=101 + i,
                low=99 + i,
                close=100.5 + i,
                volume=10 * (i + 1),
            )
        )
    db.flush()

    result = get_candles(instrument_id=instrument.id, timeframe="60s", limit=200, db=db, user=user)

    assert [row.close for row in result] == [100.5, 101.5, 102.5]
    assert result[0].bucket_start == base


def test_get_candles_empty_when_nothing_persisted(db: Session, user):
    instrument = _instrument(db)

    result = get_candles(instrument_id=instrument.id, timeframe="60s", limit=200, db=db, user=user)

    assert result == []


def test_get_streaming_symbols_reflects_registry_state(user):
    # Directly poking the private _subscribed_symbols set (same "touch the
    # module-level private state directly in a test" pattern already
    # established for _RUNNERS in test_startup_recovery.py) -- the real
    # ensure_ingestion_running path needs a live provider/broker, well
    # outside what this endpoint's own read logic needs to prove.
    market_data_registry.reset()
    try:
        market_data_registry._subscribed_symbols.add("NIFTY")

        result = get_streaming_symbols(user=user)

        assert result.symbols == ["NIFTY"]
    finally:
        market_data_registry.reset()
