"""2026-08-12 regression coverage for a real bug found live: `app.main.
_sync_mock_instrument_universe` used to run unconditionally whenever
`get_broker()` resolved to the mock — correct for a genuinely fresh dev/
test DB, but on every real restart (which always starts with the mock,
before a user reconnects Shoonya) it silently clobbered already-correct
real Shoonya-synced NIFTY/BANKNIFTY data with the mock's own synthetic
expiry, since both share the same `Instrument`/`OptionContract` rows.

`_sync_mock_instrument_universe` calls the real `app.core.db.session
.session_scope()` internally (not a test-injected session) — deliberately
patched here to the test's own isolated `db` fixture rather than left
alone, per this codebase's own documented trap (`ensure_ingestion_
running`'s identical incident, see CLAUDE.md's Phase 4 QC section):
leaving it unpatched would run real writes against the production/dev
database from a test.
"""

from __future__ import annotations

import contextlib
import uuid

import app.core.db.session as db_session_module
import app.main as main_module
from app.domain.market.models import Instrument, InstrumentMasterSyncLog
from app.modules.broker_adapter import composition


def _patch_session_scope(monkeypatch, db):
    @contextlib.contextmanager
    def _fake_session_scope():
        yield db

    monkeypatch.setattr(db_session_module, "session_scope", _fake_session_scope)


def test_skips_sync_when_a_known_underlying_already_has_an_instrument_row(db, monkeypatch):
    _patch_session_scope(monkeypatch, db)
    # Deliberately not `composition.set_broker(...)` — see the identical
    # note in test_runs_sync_when_no_known_underlying_instrument_exists
    # below; the lazy default is what actually satisfies `isinstance(broker,
    # MockBrokerAdapter)` inside the function under test.

    db.add(
        Instrument(
            id=uuid.uuid4(),
            symbol="NIFTY",
            exchange="NFO",
            lot_size=65,
            tick_size=0.05,
            is_active=True,
        )
    )
    db.flush()

    main_module._sync_mock_instrument_universe()

    # No mock sync ran -- no InstrumentMasterSyncLog row, and the real
    # NIFTY row above is still the only NIFTY Instrument row (the mock
    # sync would have found it "already exists" and only updated it, but
    # would still have gone on to upsert its own synthetic OptionContract
    # rows, which is exactly the clobbering this fix prevents).
    assert db.query(InstrumentMasterSyncLog).count() == 0
    assert db.query(Instrument).filter(Instrument.symbol == "NIFTY").count() == 1


def test_runs_sync_when_no_known_underlying_instrument_exists(db, monkeypatch):
    _patch_session_scope(monkeypatch, db)
    # Deliberately not `composition.set_broker(...)` here — that always
    # wraps in `_AuthAwareBroker` (see its own docstring), which would make
    # `isinstance(broker, MockBrokerAdapter)` false and defeat this test's
    # own point. `_reset_broker_singleton` (autouse) already guarantees
    # `get_broker()`'s lazy default resolves to a raw, unwrapped
    # `MockBrokerAdapter` here.

    main_module._sync_mock_instrument_universe()

    assert db.query(InstrumentMasterSyncLog).count() == 1


def test_does_nothing_for_a_non_mock_broker(db, monkeypatch):
    _patch_session_scope(monkeypatch, db)

    class _NotMockBroker:
        pass

    composition.set_broker(_NotMockBroker())  # type: ignore[arg-type]

    main_module._sync_mock_instrument_universe()  # must not raise

    assert db.query(InstrumentMasterSyncLog).count() == 0
