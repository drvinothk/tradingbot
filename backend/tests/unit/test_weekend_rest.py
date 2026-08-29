"""`app.modules.ops.weekend_rest` -- the in-memory weekday/idle predicate that
drives weekend rest mode. Pure logic, no DB; `now` overrides keep every case
deterministic regardless of the real day of week CI runs on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config.settings import get_settings
from app.modules.ops import weekend_rest

# 2026-08-28 Fri, 08-29 Sat, 08-30 Sun, 08-31 Mon (verified).
FRIDAY = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
SUNDAY = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
MONDAY = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_marker():
    weekend_rest.reset_for_tests()
    yield
    weekend_rest.reset_for_tests()


@pytest.mark.parametrize(
    ("day", "expected"),
    [(FRIDAY, False), (SATURDAY, True), (SUNDAY, True), (MONDAY, False)],
)
def test_is_weekend_ist(day: datetime, expected: bool) -> None:
    assert weekend_rest.is_weekend_ist(now=day) is expected


def test_weekday_is_always_awake_even_with_no_activity() -> None:
    # No touch() called -- still awake, because it's a weekday.
    assert weekend_rest.is_system_awake(now_ist_val=FRIDAY) is True
    assert weekend_rest.is_dormant(now_ist_val=MONDAY) is False


def test_weekend_is_dormant_with_no_activity() -> None:
    assert weekend_rest.is_system_awake(now_ist_val=SATURDAY) is False
    assert weekend_rest.is_dormant(now_ist_val=SATURDAY) is True


def test_weekend_touch_wakes_within_idle_window() -> None:
    t0 = SATURDAY
    weekend_rest.touch(now=t0)

    idle_min = get_settings().app.weekend_rest_idle_minutes
    just_inside = t0 + timedelta(minutes=idle_min) - timedelta(seconds=1)
    just_outside = t0 + timedelta(minutes=idle_min) + timedelta(seconds=1)

    assert weekend_rest.is_system_awake(now_ist_val=SATURDAY, now_utc_val=just_inside) is True
    assert weekend_rest.is_system_awake(now_ist_val=SATURDAY, now_utc_val=just_outside) is False


def test_weekend_sleep_now_sleeps_immediately() -> None:
    weekend_rest.touch(now=SATURDAY)
    assert weekend_rest.is_system_awake(now_ist_val=SATURDAY, now_utc_val=SATURDAY) is True

    weekend_rest.sleep_now()
    assert weekend_rest.is_system_awake(now_ist_val=SATURDAY, now_utc_val=SATURDAY) is False


def test_disabled_flag_forces_always_awake_on_a_weekend(monkeypatch) -> None:
    monkeypatch.setattr(get_settings().app, "weekend_rest_enabled", False)
    # Sunday, no activity marker at all -- still awake because the feature is off.
    assert weekend_rest.is_system_awake(now_ist_val=SUNDAY) is True
    assert weekend_rest.is_dormant(now_ist_val=SUNDAY) is False
