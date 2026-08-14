"""Ops-Hardening Phase 3: reporting.export_scheduler.TradeLogExportScheduler
-- pure trigger/throttle logic, driven with a monkeypatched clock and a
counting fake in place of the real export function (no DB/file I/O needed
here; those are covered by test_trade_log_exporter.py and
test_trade_log_export_query.py).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

import app.modules.reporting.export_scheduler as export_scheduler_module
from app.core.clock import IST
from app.modules.reporting.export_scheduler import EXPORT_TIME, TradeLogExportScheduler


@pytest.fixture
def calls(monkeypatch) -> list[date]:
    recorded: list[date] = []

    def _fake_export(target_date=None, **kwargs):
        recorded.append(target_date)

    monkeypatch.setattr(export_scheduler_module, "export_completed_trades_for_day", _fake_export)
    return recorded


def _at(hour: int, minute: int, day: int = 18) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=IST)


def test_does_not_trigger_before_export_time(monkeypatch, calls):
    monkeypatch.setattr(export_scheduler_module, "now_ist", lambda: _at(15, 34))
    scheduler = TradeLogExportScheduler()

    scheduler.run_once()

    assert calls == []


def test_triggers_on_first_tick_at_or_after_export_time(monkeypatch, calls):
    monkeypatch.setattr(export_scheduler_module, "now_ist", lambda: _at(15, 35))
    scheduler = TradeLogExportScheduler()

    scheduler.run_once()

    assert calls == [date(2026, 8, 18)]


def test_does_not_retrigger_later_the_same_day(monkeypatch, calls):
    monkeypatch.setattr(export_scheduler_module, "now_ist", lambda: _at(15, 35))
    scheduler = TradeLogExportScheduler()
    scheduler.run_once()

    monkeypatch.setattr(export_scheduler_module, "now_ist", lambda: _at(18, 0))
    scheduler.run_once()

    assert calls == [date(2026, 8, 18)]


def test_triggers_again_the_next_day(monkeypatch, calls):
    monkeypatch.setattr(export_scheduler_module, "now_ist", lambda: _at(15, 35, day=18))
    scheduler = TradeLogExportScheduler()
    scheduler.run_once()

    monkeypatch.setattr(export_scheduler_module, "now_ist", lambda: _at(15, 35, day=19))
    scheduler.run_once()

    assert calls == [date(2026, 8, 18), date(2026, 8, 19)]


def test_a_restart_after_export_time_catches_up_immediately(monkeypatch, calls):
    # A fresh scheduler instance (as after a process restart) with no
    # in-memory record of having already exported today must still trigger
    # on its very first tick if it's already past EXPORT_TIME -- safe
    # because the underlying export is idempotent (see exporter.py).
    monkeypatch.setattr(export_scheduler_module, "now_ist", lambda: _at(20, 0))
    scheduler = TradeLogExportScheduler()

    scheduler.run_once()

    assert calls == [date(2026, 8, 18)]


def test_does_not_mark_the_day_done_when_export_raises(monkeypatch):
    def _raise(target_date=None, **kwargs):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(export_scheduler_module, "export_completed_trades_for_day", _raise)
    monkeypatch.setattr(export_scheduler_module, "now_ist", lambda: _at(15, 35))
    scheduler = TradeLogExportScheduler()

    with pytest.raises(RuntimeError):
        scheduler.run_once()

    assert scheduler._last_export_date is None


def test_export_time_constant_is_15_35():
    assert EXPORT_TIME.hour == 15
    assert EXPORT_TIME.minute == 35
