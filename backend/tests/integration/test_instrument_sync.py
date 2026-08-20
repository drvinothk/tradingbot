from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.domain.market.mock_universe import build_mock_universe
from app.domain.market.models import Instrument, InstrumentMasterSyncLog, OptionContract, SyncStatus
from app.modules.broker_adapter.base.contracts import InstrumentInfo, OptionType
from app.modules.broker_adapter.mock import MockBrokerAdapter
from app.modules.scheduler import sync_instrument_master

EXPIRY = date(2026, 7, 31)


def test_first_sync_creates_instruments_and_contracts(db: Session):
    broker = MockBrokerAdapter(instruments=build_mock_universe(EXPIRY), seed=1)
    log = sync_instrument_master(db, broker, exchanges=["NFO"])

    assert log.status == SyncStatus.SUCCESS
    assert log.instruments_updated == 2
    assert log.contracts_added == 84
    assert db.query(Instrument).count() == 2
    assert db.query(OptionContract).count() == 84


def test_second_sync_with_unchanged_data_is_a_true_no_op(db: Session):
    broker = MockBrokerAdapter(instruments=build_mock_universe(EXPIRY), seed=1)
    sync_instrument_master(db, broker, exchanges=["NFO"])
    db.flush()

    second_log = sync_instrument_master(db, broker, exchanges=["NFO"])

    assert second_log.instruments_updated == 0
    assert second_log.contracts_added == 0
    assert second_log.contracts_expired == 0
    assert db.query(Instrument).count() == 2
    assert db.query(OptionContract).count() == 84


def test_sync_is_not_fooled_by_decimal_vs_float_precision(db: Session):
    """Regression test: comparing a Numeric column read back as Decimal
    against a raw Python float (e.g. Decimal('0.0500') != 0.05) is unreliable
    and previously caused every sync run to report a spurious update even
    when nothing had changed."""
    broker = MockBrokerAdapter(instruments=build_mock_universe(EXPIRY), seed=1)
    sync_instrument_master(db, broker, exchanges=["NFO"])
    db.flush()

    for _ in range(3):
        log = sync_instrument_master(db, broker, exchanges=["NFO"])
        assert log.instruments_updated == 0


def test_sync_detects_a_real_lot_size_change(db: Session):
    broker = MockBrokerAdapter(instruments=build_mock_universe(EXPIRY), seed=1)
    sync_instrument_master(db, broker, exchanges=["NFO"])
    db.flush()

    changed_universe = [
        InstrumentInfo(symbol="NIFTY", exchange="NFO", lot_size=30, tick_size=0.05)
        if i.symbol == "NIFTY" and not i.is_option
        else i
        for i in build_mock_universe(EXPIRY)
    ]
    broker2 = MockBrokerAdapter(instruments=changed_universe, seed=1)
    log = sync_instrument_master(db, broker2, exchanges=["NFO"])

    assert log.instruments_updated == 1
    nifty = db.query(Instrument).filter(Instrument.symbol == "NIFTY").one()
    assert nifty.lot_size == 30


def test_sync_raises_a_system_alert_on_lot_size_change(db: Session, workspace):
    """2026-08-20 live incident: this used to be a silent overwrite,
    discovered only via a real live order rejection hours after the value
    had actually changed. A SystemAlert can't validate the *new* value is
    correct (lot sizes are legitimately revised by the exchange
    periodically, unlike price which has a stable floor) -- but it makes
    the change visible instead of silent.
    """
    from app.domain.ops.models import SystemAlert

    broker = MockBrokerAdapter(instruments=build_mock_universe(EXPIRY), seed=1)
    sync_instrument_master(db, broker, exchanges=["NFO"])
    db.flush()

    changed_universe = [
        InstrumentInfo(symbol="NIFTY", exchange="NFO", lot_size=65, tick_size=0.05)
        if i.symbol == "NIFTY" and not i.is_option
        else i
        for i in build_mock_universe(EXPIRY)
    ]
    broker2 = MockBrokerAdapter(instruments=changed_universe, seed=1)
    sync_instrument_master(db, broker2, exchanges=["NFO"])

    alert = (
        db.query(SystemAlert)
        .filter(SystemAlert.category == "instrument_master_changed")
        .one()
    )
    assert alert.workspace_id == workspace.id
    assert "NIFTY" in alert.message
    assert "lot_size" in alert.message


def test_sync_with_no_actual_change_raises_no_alert(db: Session, workspace):
    """The Decimal-vs-float precision guard already prevents a spurious
    `instruments_updated` count for an unchanged sync (see
    test_sync_is_not_fooled_by_decimal_vs_float_precision) -- this pins
    that the alert path inherits that same correctness, not just the
    counter.
    """
    from app.domain.ops.models import SystemAlert

    broker = MockBrokerAdapter(instruments=build_mock_universe(EXPIRY), seed=1)
    sync_instrument_master(db, broker, exchanges=["NFO"])
    db.flush()

    sync_instrument_master(db, broker, exchanges=["NFO"])

    count = (
        db.query(SystemAlert)
        .filter(SystemAlert.category == "instrument_master_changed")
        .count()
    )
    assert count == 0


def test_sync_populates_freeze_qty_from_broker(db: Session):
    broker = MockBrokerAdapter(instruments=build_mock_universe(EXPIRY), seed=1)
    sync_instrument_master(db, broker, exchanges=["NFO"])

    nifty = db.query(Instrument).filter(Instrument.symbol == "NIFTY").one()
    assert nifty.freeze_qty == 1800  # mock_universe's illustrative placeholder


def test_sync_never_blanks_an_operator_set_freeze_qty_with_a_missing_value(db: Session):
    """freeze_qty is operator-supplied (see Instrument.freeze_qty's own
    docstring) — a sync source that doesn't carry the field (real Shoonya
    data today) must never overwrite a value an operator already set.
    """
    broker = MockBrokerAdapter(instruments=build_mock_universe(EXPIRY), seed=1)
    sync_instrument_master(db, broker, exchanges=["NFO"])
    db.flush()

    nifty = db.query(Instrument).filter(Instrument.symbol == "NIFTY").one()
    nifty.freeze_qty = 2500  # operator override
    db.add(nifty)
    db.flush()

    # A "broker" universe that changes lot_size (forcing an update) but
    # carries no freeze_qty at all — same shape real Shoonya data has today.
    no_freeze_qty_universe = [
        InstrumentInfo(symbol="NIFTY", exchange="NFO", lot_size=30, tick_size=0.05)
        if i.symbol == "NIFTY" and not i.is_option
        else i
        for i in build_mock_universe(EXPIRY)
    ]
    broker2 = MockBrokerAdapter(instruments=no_freeze_qty_universe, seed=1)
    log = sync_instrument_master(db, broker2, exchanges=["NFO"])

    assert log.instruments_updated == 1
    db.refresh(nifty)
    assert nifty.lot_size == 30
    assert nifty.freeze_qty == 2500  # untouched, not blanked to None


def test_expired_contracts_are_deactivated_not_deleted(db: Session):
    # The expiry sweep runs unconditionally at the end of every call, so a
    # contract inserted with an already-past expiry is self-deactivated in
    # that same run — there's no reason to let it sit "active" for one extra
    # cycle just because it was created and expired in the same call.
    past_expiry = date.today() - timedelta(days=1)
    broker = MockBrokerAdapter(instruments=build_mock_universe(past_expiry), seed=1)
    first_log = sync_instrument_master(db, broker, exchanges=["NFO"])
    db.flush()

    assert first_log.contracts_added == 84
    assert first_log.contracts_expired == 84
    still_present = db.query(OptionContract).filter(OptionContract.expiry_date == past_expiry).all()
    assert len(still_present) == 84
    assert all(not c.is_active for c in still_present)

    # A later run shouldn't re-report contracts that are already inactive.
    second_log = sync_instrument_master(db, broker, exchanges=["NFO"])
    assert second_log.contracts_expired == 0


def test_a_still_future_contract_survives_a_later_sync_that_omits_it(db: Session):
    """Regression test for the reverted 2026-08-11 diff-deactivation
    attempt: live evidence on 2026-08-12 showed Shoonya's real `SearchScrip`
    returns different, non-overlapping partial subsets of an underlying's
    true option chain across separate calls (confirmed via a real spot-
    price cross-check — see this module's own docstring). A not-yet-expired
    contract must stay active even when a later sync's response happens not
    to mention it again — deactivating on "not seen in this one call" flips
    genuinely-valid data inactive whenever a call returns a partial result,
    which this broker demonstrably does.
    """
    first_expiry = date.today() + timedelta(days=7)
    second_expiry = date.today() + timedelta(days=14)

    broker = MockBrokerAdapter(instruments=build_mock_universe(first_expiry), seed=1)
    sync_instrument_master(db, broker, exchanges=["NFO"])
    db.flush()
    first_run_contracts = (
        db.query(OptionContract).filter(OptionContract.expiry_date == first_expiry).all()
    )
    assert len(first_run_contracts) == 84
    assert all(c.is_active for c in first_run_contracts)

    # A later sync's response happens not to mention `first_expiry` at all
    # (a partial/different subset, not a genuine "this expiry is gone").
    broker2 = MockBrokerAdapter(instruments=build_mock_universe(second_expiry), seed=1)
    sync_instrument_master(db, broker2, exchanges=["NFO"])

    still_there = db.query(OptionContract).filter(OptionContract.expiry_date == first_expiry).all()
    assert len(still_there) == 84
    assert all(c.is_active for c in still_there), (
        "a still-future contract must never be deactivated just because one "
        "later sync call didn't happen to mention it"
    )

    fresh = db.query(OptionContract).filter(OptionContract.expiry_date == second_expiry).all()
    assert len(fresh) == 84
    assert all(c.is_active for c in fresh)


def test_option_with_unknown_underlying_is_skipped_not_crashed(db: Session):
    orphan = InstrumentInfo(
        symbol="GHOST31JUL2610000CE",
        exchange="NFO",
        lot_size=25,
        tick_size=0.05,
        is_option=True,
        underlying="GHOST",
        expiry=EXPIRY,
        strike=10000,
        option_type=OptionType.CE,
    )
    broker = MockBrokerAdapter(instruments=[orphan], seed=1)
    log = sync_instrument_master(db, broker, exchanges=["NFO"])

    assert log.status == SyncStatus.SUCCESS
    assert log.contracts_added == 0
    assert db.query(OptionContract).count() == 0


def test_sync_records_failure_log_on_broker_error(db: Session):
    class BrokenBroker(MockBrokerAdapter):
        def get_instrument_master(self, exchange: str) -> list[InstrumentInfo]:
            raise ConnectionError("simulated broker outage")

    broker = BrokenBroker(instruments=[], seed=1)
    log = sync_instrument_master(db, broker, exchanges=["NFO"])

    assert log.status == SyncStatus.FAILED
    assert "simulated broker outage" in log.detail
    assert db.query(InstrumentMasterSyncLog).count() == 1
