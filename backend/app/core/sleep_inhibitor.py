"""Windows sleep inhibitor. Activity-based, not time-based, per the user's
own guidance: normal sleep is fine when idle, but the machine must not sleep
while a trading_session is actively scanning, and must definitely not sleep
while a position is open. Callers acquire/release around those state changes
(Scheduler owns this in later phases); this module only wraps the Win32 call.

Uses ctypes directly rather than pywin32 for this one call — it's a single
well-documented kernel32 function and ctypes avoids a pywin32 dependency for
something this small.
"""

from __future__ import annotations

import ctypes
import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040


def inhibit_sleep() -> bool:
    """Tells Windows the system should stay awake until release_sleep() is
    called. Idempotent — calling this again while already inhibited just
    re-asserts the same flags. Returns False (no-op) on non-Windows, so this
    is safe to import/call in tests on any platform.
    """
    if sys.platform != "win32":
        return False
    result = ctypes.windll.kernel32.SetThreadExecutionState(  # type: ignore[attr-defined]
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    )
    return result != 0


def release_sleep() -> bool:
    """Releases the inhibition, returning the system to its normal power plan."""
    if sys.platform != "win32":
        return False
    result = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)  # type: ignore[attr-defined]
    return result != 0


class SleepInhibitor:
    """Reference-counted so overlapping reasons to stay awake (an active scan
    AND an open position) don't release prematurely when only one clears."""

    def __init__(self) -> None:
        self._active_reasons: set[str] = set()

    def acquire(self, reason: str) -> None:
        was_empty = not self._active_reasons
        self._active_reasons.add(reason)
        if was_empty:
            inhibit_sleep()

    def release(self, reason: str) -> None:
        self._active_reasons.discard(reason)
        if not self._active_reasons:
            release_sleep()

    @property
    def is_inhibited(self) -> bool:
        return bool(self._active_reasons)


_inhibitor: SleepInhibitor | None = None


def get_sleep_inhibitor() -> SleepInhibitor:
    """Process-wide singleton, same shape as `broker_adapter.composition.
    get_broker()` — one `SleepInhibitor` for the whole process, since the
    reference-counting only means anything if every acquire/release call
    site shares the same instance. Callers: `api.v1.strategies.start_strategy`
    /`stop_strategy` acquire/release around "actively scanning"
    (`f"strategy_run:{run.id}"`), `execution_engine.paper.service`
    `_open_position_from_fill`/`close_position` acquire/release around "has
    an open position" (`f"position:{position.id}"`) — the two overlapping
    lifecycles this module's own docstring describes.
    """
    global _inhibitor
    if _inhibitor is None:
        _inhibitor = SleepInhibitor()
    return _inhibitor
