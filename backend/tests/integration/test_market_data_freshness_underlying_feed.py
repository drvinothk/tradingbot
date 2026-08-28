"""`classify_latest_bar` / `underlying_feed_state` / `any_underlying_feed_fresh`
— the "is the underlying feed live right now" signal that drives
`GET /shoonya/status` and gates the health check's `market_data_stale` alert.
Requires a real Postgres (the shared `db` fixture) since all three read
`quote_ticks` / `price_bars`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.domain.market.models import Instrument, PriceBar, QuoteTick
from app.modules.market_data.freshness import (
    FreshnessState,
    FreshnessThresholds,
    any_underlying_feed_fresh,
    classify_latest_bar,
    underlying_feed_state,
)


def _instrument(db: Session, symbol: str) -> Instrument:
    inst = Instrument(
        id=uuid.uuid4(), symbol=symbol, exchange="NSE", lot_size=75, tick_size=0.05
    )
    db.add(inst)
    db.flush()
    return inst


def _tick(db: Session, instrument_id: uuid.UUID, *, seconds_ago: float) -> None:
    db.add(
        QuoteTick(
            id=uuid.uuid4(),
            instrument_id=instrument_id,
            ltp=100.0,
            bid=99.5,
            ask=100.5,
            volume=0,
            oi=None,
            ts=datetime.now(UTC) - timedelta(seconds=seconds_ago),
        )
    )
    db.flush()


def _bar(
    db: Session, instrument_id: uuid.UUID, *, bucket_seconds_ago: float, timeframe: str = "60s"
) -> None:
    db.add(
        PriceBar(
            id=uuid.uuid4(),
            instrument_id=instrument_id,
            timeframe=timeframe,
            bucket_start=datetime.now(UTC) - timedelta(seconds=bucket_seconds_ago),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1000,
        )
    )
    db.flush()


_THRESHOLDS = FreshnessThresholds(degraded_after_seconds=30.0, stale_after_seconds=120.0)


# --- classify_latest_bar --------------------------------------------------


def test_classify_latest_bar_dead_when_no_bar(db: Session):
    inst = _instrument(db, "NIFTY-BARFEED-1")
    assert classify_latest_bar(db, inst.id, thresholds=_THRESHOLDS) == FreshnessState.DEAD


def test_classify_latest_bar_live_for_a_just_completed_bucket(db: Session):
    inst = _instrument(db, "NIFTY-BARFEED-2")
    # bucket_start 70s ago + 60s timeframe -> effective_ts 10s ago -> LIVE
    _bar(db, inst.id, bucket_seconds_ago=70)
    assert classify_latest_bar(db, inst.id, thresholds=_THRESHOLDS) == FreshnessState.LIVE


def test_classify_latest_bar_stale_for_an_old_bucket(db: Session):
    inst = _instrument(db, "NIFTY-BARFEED-3")
    # effective_ts ~240s ago -> past the 120s stale threshold
    _bar(db, inst.id, bucket_seconds_ago=300)
    assert classify_latest_bar(db, inst.id, thresholds=_THRESHOLDS) == FreshnessState.STALE


def test_classify_latest_bar_ignores_other_timeframes(db: Session):
    inst = _instrument(db, "NIFTY-BARFEED-4")
    _bar(db, inst.id, bucket_seconds_ago=70, timeframe="300s")
    assert classify_latest_bar(db, inst.id, thresholds=_THRESHOLDS) == FreshnessState.DEAD


# --- underlying_feed_state ----------------------------------------------


def test_underlying_feed_state_live_when_only_tick_is_fresh(db: Session):
    inst = _instrument(db, "NIFTY-FEED-1")
    _tick(db, inst.id, seconds_ago=3)
    _bar(db, inst.id, bucket_seconds_ago=6000)  # ancient
    assert underlying_feed_state(db, inst.id) == FreshnessState.LIVE


def test_underlying_feed_state_live_when_only_bar_is_fresh(db: Session):
    """The REST-fallback case: WS ticks stopped, but `price_bars` keep
    flowing via REST polling — the feed is up.
    """
    inst = _instrument(db, "NIFTY-FEED-2")
    _tick(db, inst.id, seconds_ago=6000)  # ancient
    _bar(db, inst.id, bucket_seconds_ago=80)  # effective_ts ~20s ago
    assert underlying_feed_state(db, inst.id) in (FreshnessState.LIVE, FreshnessState.DEGRADED)


def test_underlying_feed_state_stale_when_both_are_stale(db: Session):
    inst = _instrument(db, "NIFTY-FEED-3")
    _tick(db, inst.id, seconds_ago=6000)
    _bar(db, inst.id, bucket_seconds_ago=6000)
    assert underlying_feed_state(db, inst.id) in (FreshnessState.STALE, FreshnessState.DEAD)


# --- any_underlying_feed_fresh ---------------------------------------------


def test_any_underlying_feed_fresh_true_when_one_symbol_is_live(db: Session):
    nifty = _instrument(db, "NIFTY-ANY-1")
    _instrument(db, "BANKNIFTY-ANY-1")  # no data at all
    _tick(db, nifty.id, seconds_ago=3)
    assert any_underlying_feed_fresh(db, ("NIFTY-ANY-1", "BANKNIFTY-ANY-1")) is True


def test_any_underlying_feed_fresh_false_when_all_stale(db: Session):
    nifty = _instrument(db, "NIFTY-ANY-2")
    bank = _instrument(db, "BANKNIFTY-ANY-2")
    _tick(db, nifty.id, seconds_ago=6000)
    _bar(db, bank.id, bucket_seconds_ago=6000)
    assert any_underlying_feed_fresh(db, ("NIFTY-ANY-2", "BANKNIFTY-ANY-2")) is False


def test_any_underlying_feed_fresh_false_when_symbols_are_unknown(db: Session):
    assert any_underlying_feed_fresh(db, ("NO-SUCH-SYMBOL",)) is False
