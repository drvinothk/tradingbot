"""Ops-Hardening Phase 4: session.bootstrapper.DailyBootstrapScheduler --
pure trigger/throttle logic, driven with a monkeypatched clock and a
counting fake in place of the real bootstrap function (session-lifecycle
behavior itself is covered by tests/integration/test_daily_bootstrapper.py).
"""

from __future__ import annotations

from datetime import datetime

import pytest

import app.modules.scheduler.base as scheduler_base_module
import app.modules.session.bootstrapper as bootstrapper_module
from app.core.clock import IST
from app.modules.session.bootstrapper import BOOTSTRAP_TIME, DailyBootstrapScheduler


@pytest.fixture
def calls(monkeypatch) -> list[None]:
    recorded: list[None] = []
    monkeypatch.setattr(bootstrapper_module, "run_daily_bootstrap", lambda: recorded.append(None))
    return recorded


def _at(hour: int, minute: int, day: int = 18) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=IST)


def test_does_not_trigger_before_bootstrap_time(monkeypatch, calls):
    monkeypatch.setattr(scheduler_base_module, "now_ist", lambda: _at(8, 59))
    scheduler = DailyBootstrapScheduler()

    scheduler.run_once()

    assert calls == []


def test_triggers_on_first_tick_at_or_after_bootstrap_time(monkeypatch, calls):
    monkeypatch.setattr(scheduler_base_module, "now_ist", lambda: _at(9, 0))
    scheduler = DailyBootstrapScheduler()

    scheduler.run_once()

    assert len(calls) == 1


def test_does_not_retrigger_later_the_same_day(monkeypatch, calls):
    monkeypatch.setattr(scheduler_base_module, "now_ist", lambda: _at(9, 0))
    scheduler = DailyBootstrapScheduler()
    scheduler.run_once()

    monkeypatch.setattr(scheduler_base_module, "now_ist", lambda: _at(12, 0))
    scheduler.run_once()

    assert len(calls) == 1


def test_triggers_again_the_next_day(monkeypatch, calls):
    monkeypatch.setattr(scheduler_base_module, "now_ist", lambda: _at(9, 0, day=18))
    scheduler = DailyBootstrapScheduler()
    scheduler.run_once()

    monkeypatch.setattr(scheduler_base_module, "now_ist", lambda: _at(9, 0, day=19))
    scheduler.run_once()

    assert len(calls) == 2


def test_a_restart_at_09_05_catches_up_immediately(monkeypatch, calls):
    # The exact scenario the original spec names -- a restart right after
    # 09:00 must still bootstrap today on its first tick, safe because
    # run_daily_bootstrap's own session-creation is idempotent.
    monkeypatch.setattr(scheduler_base_module, "now_ist", lambda: _at(9, 5))
    scheduler = DailyBootstrapScheduler()

    scheduler.run_once()

    assert len(calls) == 1


def test_does_not_mark_the_day_done_when_bootstrap_raises(monkeypatch):
    def _raise():
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(bootstrapper_module, "run_daily_bootstrap", _raise)
    monkeypatch.setattr(scheduler_base_module, "now_ist", lambda: _at(9, 0))
    scheduler = DailyBootstrapScheduler()

    with pytest.raises(RuntimeError):
        scheduler.run_once()

    assert scheduler._last_run_date is None


def test_bootstrap_time_constant_is_09_00():
    assert BOOTSTRAP_TIME.hour == 9
    assert BOOTSTRAP_TIME.minute == 0
