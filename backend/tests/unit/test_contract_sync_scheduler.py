"""Ops-Hardening Phase 7: scheduler.contract_sync_scheduler
.ContractSyncScheduler -- pure trigger/throttle logic, driven with a
monkeypatched clock and a counting fake in place of run_contract_sync.
"""

from __future__ import annotations

from datetime import datetime

import pytest

import app.modules.scheduler.contract_sync_scheduler as contract_sync_module
from app.core.clock import IST
from app.modules.scheduler.contract_sync_scheduler import (
    CONTRACT_SYNC_TIME,
    ContractSyncScheduler,
)


@pytest.fixture
def calls(monkeypatch) -> list[None]:
    recorded: list[None] = []
    monkeypatch.setattr(
        contract_sync_module, "run_contract_sync", lambda: recorded.append(None)
    )
    return recorded


def _at(hour: int, minute: int, day: int = 18) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=IST)


def test_does_not_trigger_before_sync_time(monkeypatch, calls):
    monkeypatch.setattr(contract_sync_module, "now_ist", lambda: _at(8, 29))
    scheduler = ContractSyncScheduler()

    scheduler.run_once()

    assert calls == []


def test_triggers_on_first_tick_at_or_after_sync_time(monkeypatch, calls):
    monkeypatch.setattr(contract_sync_module, "now_ist", lambda: _at(8, 30))
    scheduler = ContractSyncScheduler()

    scheduler.run_once()

    assert len(calls) == 1


def test_does_not_retrigger_later_the_same_day(monkeypatch, calls):
    monkeypatch.setattr(contract_sync_module, "now_ist", lambda: _at(8, 30))
    scheduler = ContractSyncScheduler()
    scheduler.run_once()

    monkeypatch.setattr(contract_sync_module, "now_ist", lambda: _at(12, 0))
    scheduler.run_once()

    assert len(calls) == 1


def test_triggers_again_the_next_day(monkeypatch, calls):
    monkeypatch.setattr(contract_sync_module, "now_ist", lambda: _at(8, 30, day=18))
    scheduler = ContractSyncScheduler()
    scheduler.run_once()

    monkeypatch.setattr(contract_sync_module, "now_ist", lambda: _at(8, 30, day=19))
    scheduler.run_once()

    assert len(calls) == 2


def test_does_not_mark_the_day_done_when_sync_raises(monkeypatch):
    def _raise():
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(contract_sync_module, "run_contract_sync", _raise)
    monkeypatch.setattr(contract_sync_module, "now_ist", lambda: _at(8, 30))
    scheduler = ContractSyncScheduler()

    with pytest.raises(RuntimeError):
        scheduler.run_once()

    assert scheduler._last_sync_date is None


def test_sync_time_constant_is_08_30():
    assert CONTRACT_SYNC_TIME.hour == 8
    assert CONTRACT_SYNC_TIME.minute == 30


def test_run_contract_sync_skips_when_shoonya_not_connected(monkeypatch):
    monkeypatch.setattr(contract_sync_module, "is_shoonya_configured", lambda: False)
    sync_calls: list[None] = []
    monkeypatch.setattr(
        contract_sync_module, "sync_instrument_master", lambda *a, **kw: sync_calls.append(None)
    )

    contract_sync_module.run_contract_sync()

    assert sync_calls == []


def test_run_contract_sync_calls_sync_when_shoonya_connected(monkeypatch):
    from contextlib import contextmanager

    monkeypatch.setattr(contract_sync_module, "is_shoonya_configured", lambda: True)
    monkeypatch.setattr(contract_sync_module, "get_broker", lambda: object())

    class _FakeLog:
        status = "success"
        instruments_updated = 0
        contracts_added = 0
        contracts_expired = 0

    sync_calls: list[None] = []

    def _fake_sync(db, broker, exchanges):
        sync_calls.append(None)
        return _FakeLog()

    monkeypatch.setattr(contract_sync_module, "sync_instrument_master", _fake_sync)

    @contextmanager
    def _fake_session_scope():
        yield object()

    monkeypatch.setattr(contract_sync_module, "session_scope", _fake_session_scope)

    contract_sync_module.run_contract_sync()

    assert len(sync_calls) == 1
