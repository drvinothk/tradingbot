"""Weekend rest mode. On Saturday/Sunday IST the whole system goes dormant --
no broker connect/subscribe churn, no daily strategy bootstrap, no Telegram
pushes -- *unless* a user is actively logged in. Any authenticated request
(a login, a page load, the dashboard's own ~4s poll) marks the user active
via `touch()`; the system stays awake for `APP_WEEKEND_REST_IDLE_MINUTES`
(default 10) after the last such request, then goes back to sleep. An
explicit logout calls `sleep_now()` and sleeps it immediately.

**Monday-Friday IST this module is inert**: `is_system_awake()` returns
`True` before any other logic runs, so there is exactly zero behavior
change on a trading day. `APP_WEEKEND_REST_ENABLED=false` is an instant
kill switch (same "dangerous-ish toggle, documented, off-switch via
`systemctl set-environment` with no redeploy" shape as
`AppSettings.allow_real_money_dispatch`).

Why time-of-day gates weren't enough: every existing gate in this codebase
(`market_data.market_hours` 08:30-16:00, `alerting.manager`'s 09:00-15:30
Telegram window, `core.clock`'s trade window) checks the *hour* but never
the *day of week*, so a weekend behaves exactly like a trading day --
`MarketDataScheduler` connects the broker, gets no ticks, and
`HealthCheckScheduler` then fires a CRITICAL `market_data_stale` alert to
Telegram every 5 minutes.

Deliberately in-memory only (a module-level `datetime` behind a lock, same
"resets on restart, and that's acceptable" reasoning as
`alerting.manager`'s own Telegram dedup dict): no DB query, no migration,
no new write path. A restart during a weekend the user is actively using
re-wakes within one poll cycle (~a few seconds) once the browser's next
request lands.

Market holidays that fall on a weekday are **not** handled here -- weekends
only. Adding an NSE holiday calendar is a separate, maintenance-heavy
change; on a weekday holiday the user can just log in to wake the system,
same as any weekend.

Callers reference this as `weekend_rest.is_system_awake()` (module-attribute
access, not `from ... import is_system_awake`) so a single
`monkeypatch.setattr` in a test covers every call site at once.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from app.config.settings import get_settings
from app.core.clock import now_ist

_lock = threading.Lock()
# None => "no recent activity" (asleep on a weekend). Set to a UTC-aware
# datetime by touch(); cleared by sleep_now().
_last_activity_at: datetime | None = None


def touch(now: datetime | None = None) -> None:
    """Record authenticated user activity now. Called from the auth
    chokepoint (`core.security.deps.get_current_user`) and the login
    endpoint, so any request the frontend makes while a user is signed in
    keeps the system awake through the weekend idle window.
    """
    global _last_activity_at
    with _lock:
        _last_activity_at = now if now is not None else datetime.now(UTC)


def sleep_now() -> None:
    """Immediately mark the system idle (an explicit logout). The next
    `is_system_awake()` on a weekend returns `False`."""
    global _last_activity_at
    with _lock:
        _last_activity_at = None


def is_weekend_ist(now: datetime | None = None) -> bool:
    """`True` on Saturday/Sunday IST. `now` override is for deterministic
    tests, mirroring `market_data.market_hours.current_phase`'s own
    `now: time | None` parameter."""
    n = now if now is not None else now_ist()
    return n.weekday() >= 5  # Mon=0 .. Sat=5, Sun=6


def is_system_awake(
    now_ist_val: datetime | None = None, now_utc_val: datetime | None = None
) -> bool:
    """The one predicate every gate point consults.

    Order matters: the kill switch and the weekday short-circuit both
    return before any activity/time bookkeeping, which is what guarantees
    zero behavior change Mon-Fri and when the feature is disabled.
    """
    settings = get_settings().app
    if not settings.weekend_rest_enabled:
        return True
    if not is_weekend_ist(now_ist_val):
        return True

    with _lock:
        last = _last_activity_at
    if last is None:
        return False
    now_utc = now_utc_val if now_utc_val is not None else datetime.now(UTC)
    return now_utc - last < timedelta(minutes=settings.weekend_rest_idle_minutes)


def is_dormant(
    now_ist_val: datetime | None = None, now_utc_val: datetime | None = None
) -> bool:
    return not is_system_awake(now_ist_val, now_utc_val)


def reset_for_tests() -> None:
    """Clear the in-memory activity marker. Tests that exercise dormancy
    call this (or `sleep_now()`) explicitly; the autouse
    `_weekend_rest_awake` fixture in `conftest.py` calls it on teardown."""
    sleep_now()
