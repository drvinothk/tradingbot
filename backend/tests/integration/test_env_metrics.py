"""strategy_engine.env_metrics.get_env_metrics/get_latest_env_metrics -- real
DB-backed lookups (VIX QuoteTick, OptionChainSnapshot-derived PCR) including
the `as_of_utc` historical-reconstruction path `reporting.exporter` relies on.
Pure `compute_pcr` aggregation logic is covered separately in
tests/unit/test_env_metrics.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.domain.market.models import Instrument, OptionChainSnapshot, QuoteTick
from app.modules.strategy_engine.env_metrics import get_env_metrics, get_latest_env_metrics

EXPIRY = date(2026, 8, 18)


@pytest.fixture
def instrument(db: Session) -> Instrument:
    inst = Instrument(id=uuid.uuid4(), symbol="NIFTY", exchange="NFO", lot_size=25, tick_size=0.05)
    db.add(inst)
    db.flush()
    return inst


@pytest.fixture
def vix_instrument(db: Session) -> Instrument:
    inst = Instrument(
        id=uuid.uuid4(), symbol="INDIA VIX", exchange="NSE", lot_size=1, tick_size=0.05
    )
    db.add(inst)
    db.flush()
    return inst


def _vix_tick(db: Session, vix_instrument: Instrument, *, ltp: float, ts: datetime) -> None:
    db.add(
        QuoteTick(
            id=uuid.uuid4(),
            instrument_id=vix_instrument.id,
            ltp=ltp,
            bid=ltp,
            ask=ltp,
            volume=0,
            ts=ts,
        )
    )
    db.flush()


def _snapshot(
    db: Session, instrument: Instrument, *, ts: datetime, chain_data: list[dict]
) -> None:
    db.add(
        OptionChainSnapshot(
            id=uuid.uuid4(),
            instrument_id=instrument.id,
            expiry_date=EXPIRY,
            ts=ts,
            chain_data=chain_data,
        )
    )
    db.flush()


def _chain(call_oi: int, call_vol: int, put_oi: int, put_vol: int) -> list[dict]:
    return [
        {"option_type": "CE", "oi": call_oi, "volume": call_vol},
        {"option_type": "PE", "oi": put_oi, "volume": put_vol},
    ]


def test_returns_none_when_nothing_available_at_all(db: Session, instrument):
    assert get_latest_env_metrics(db, instrument.id, EXPIRY) is None


def test_returns_none_when_vix_instrument_does_not_exist_and_no_snapshot(
    db: Session, instrument
):
    # No vix_instrument fixture used here -- simulates a DB that predates
    # migration 0020 / the VIX pipeline entirely.
    assert get_latest_env_metrics(db, instrument.id, EXPIRY) is None


def test_vix_only_populates_vix_leaves_pcr_none(db: Session, instrument, vix_instrument):
    _vix_tick(db, vix_instrument, ltp=13.5, ts=datetime.now(UTC))

    env = get_latest_env_metrics(db, instrument.id, EXPIRY)

    assert env == {"vix": 13.5, "pcr_oi": None, "pcr_vol": None}


def test_chain_only_populates_pcr_leaves_vix_none(db: Session, instrument, vix_instrument):
    _snapshot(db, instrument, ts=datetime.now(UTC), chain_data=_chain(1000, 200, 500, 100))

    env = get_latest_env_metrics(db, instrument.id, EXPIRY)

    assert env is not None
    assert env["vix"] is None
    assert env["pcr_oi"] == 0.5
    assert env["pcr_vol"] == 0.5


def test_both_present_returns_full_payload(db: Session, instrument, vix_instrument):
    now = datetime.now(UTC)
    _vix_tick(db, vix_instrument, ltp=14.0, ts=now)
    _snapshot(db, instrument, ts=now, chain_data=_chain(1000, 200, 1000, 200))

    env = get_latest_env_metrics(db, instrument.id, EXPIRY)

    assert env == {"vix": 14.0, "pcr_oi": 1.0, "pcr_vol": 1.0}


def test_latest_lookup_picks_the_most_recent_vix_tick_and_snapshot(
    db: Session, instrument, vix_instrument
):
    base = datetime.now(UTC)
    _vix_tick(db, vix_instrument, ltp=13.0, ts=base - timedelta(minutes=5))
    _vix_tick(db, vix_instrument, ltp=15.0, ts=base)
    _snapshot(db, instrument, ts=base - timedelta(minutes=5), chain_data=_chain(1000, 100, 500, 50))
    _snapshot(db, instrument, ts=base, chain_data=_chain(1000, 100, 1000, 100))

    env = get_latest_env_metrics(db, instrument.id, EXPIRY)

    assert env == {"vix": 15.0, "pcr_oi": 1.0, "pcr_vol": 1.0}


def test_latest_lookup_uses_a_stale_vix_tick_regardless_of_age_no_staleness_filter(
    db: Session, instrument, vix_instrument
):
    """India VIX is a computed index, not continuously traded -- deliberately
    no staleness filtering (see env_metrics.py's own module docstring), so
    even a tick from an hour ago is still the right answer for "current".
    """
    stale_ts = datetime.now(UTC) - timedelta(hours=1)
    _vix_tick(db, vix_instrument, ltp=12.3, ts=stale_ts)

    env = get_latest_env_metrics(db, instrument.id, EXPIRY)

    assert env is not None
    assert env["vix"] == 12.3


def test_as_of_utc_reconstructs_what_was_known_at_a_past_moment(
    db: Session, instrument, vix_instrument
):
    """reporting.exporter's own use case: report a historical trade's env
    metrics as of its entry time, not whatever is current when the report
    runs later that day.
    """
    entry_time = datetime(2026, 8, 18, 6, 0, tzinfo=UTC)
    _vix_tick(db, vix_instrument, ltp=13.0, ts=entry_time - timedelta(minutes=1))
    _vix_tick(db, vix_instrument, ltp=20.0, ts=entry_time + timedelta(minutes=10))  # after entry
    _snapshot(
        db, instrument, ts=entry_time - timedelta(seconds=30), chain_data=_chain(1000, 100, 500, 50)
    )
    _snapshot(
        db,
        instrument,
        ts=entry_time + timedelta(minutes=10),  # after entry
        chain_data=_chain(1000, 100, 1000, 100),
    )

    env = get_env_metrics(db, instrument.id, EXPIRY, as_of_utc=entry_time)

    assert env == {"vix": 13.0, "pcr_oi": 0.5, "pcr_vol": 0.5}


def test_as_of_utc_before_any_data_existed_returns_none(
    db: Session, instrument, vix_instrument
):
    entry_time = datetime(2026, 8, 18, 6, 0, tzinfo=UTC)
    _vix_tick(db, vix_instrument, ltp=13.0, ts=entry_time + timedelta(minutes=1))
    _snapshot(
        db, instrument, ts=entry_time + timedelta(minutes=1), chain_data=_chain(1000, 100, 500, 50)
    )

    env = get_env_metrics(db, instrument.id, EXPIRY, as_of_utc=entry_time)

    assert env is None


def test_env_metrics_scoped_to_instrument_and_expiry(db: Session, vix_instrument):
    """A different underlying/expiry's own snapshot must not leak in --
    the PCR side is per (instrument_id, expiry_date), not global."""
    nifty = Instrument(id=uuid.uuid4(), symbol="NIFTY", exchange="NFO", lot_size=25, tick_size=0.05)
    banknifty = Instrument(
        id=uuid.uuid4(), symbol="BANKNIFTY", exchange="NFO", lot_size=15, tick_size=0.05
    )
    db.add_all([nifty, banknifty])
    db.flush()

    now = datetime.now(UTC)
    _snapshot(db, nifty, ts=now, chain_data=_chain(1000, 100, 500, 50))
    _snapshot(db, banknifty, ts=now, chain_data=_chain(1000, 100, 1000, 100))

    nifty_env = get_latest_env_metrics(db, nifty.id, EXPIRY)
    banknifty_env = get_latest_env_metrics(db, banknifty.id, EXPIRY)

    assert nifty_env is not None and nifty_env["pcr_oi"] == 0.5
    assert banknifty_env is not None and banknifty_env["pcr_oi"] == 1.0
