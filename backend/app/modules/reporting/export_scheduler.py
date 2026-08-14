"""Ops-Hardening Phase 3. `TradeLogExportScheduler` — same background-thread
shape as `scheduler.health_check.HealthCheckScheduler` (daemon thread, a
`stop_event` for clean shutdown, `run_once()` exposed separately so tests can
drive it deterministically), triggering once daily at `EXPORT_TIME` (15:35
IST — chosen so intraday square-offs, which fire by `cutoff_time`/15:20 at
the latest, are fully settled in the DB before this reads `TradeOutcome`).

The "once daily" trigger is transition-detection, not a fixed short
interval — same shape `market_data_scheduler.MarketDataScheduler` already
uses for its own time-of-day boundaries: `_last_export_date` tracked
in-memory, compared each tick against `now_ist().date()`. This is
deliberately **not** persisted across restarts — a restart after 15:35
re-triggers on the very next tick, which is safe (not a duplicate-data bug)
purely because `exporter.export_trade_log_for_workspace`'s own append step
is idempotent (see that module's docstring). If `export_completed_trades_for_day`
raises, `_last_export_date` is deliberately left unset so the next tick
retries — a transient DB hiccup should mean "try again shortly," never
"silently skip today's export."
"""

from __future__ import annotations

import logging
import threading
from datetime import date, time

from app.core.clock import now_ist
from app.modules.reporting.exporter import export_completed_trades_for_day

logger = logging.getLogger("app.reporting.export_scheduler")

# How often the loop wakes up to check whether it's past EXPORT_TIME yet —
# a report export isn't latency-sensitive to the second the way market-data
# phase transitions are, so a coarser tick than MarketDataScheduler's 30s is
# fine; this still lands the export within a minute of 15:35.
DEFAULT_TICK_SECONDS = 60.0

EXPORT_TIME = time(15, 35)


class TradeLogExportScheduler:
    def __init__(self, tick_seconds: float = DEFAULT_TICK_SECONDS) -> None:
        self._tick_seconds = tick_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_export_date: date | None = None

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._tick_seconds + 5)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_once(self) -> None:
        now = now_ist()
        if now.time() < EXPORT_TIME or self._last_export_date == now.date():
            return
        export_completed_trades_for_day(now.date())
        self._last_export_date = now.date()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - a background loop must never die silently-crashed
                logger.exception("trade log export cycle failed")
            self._stop_event.wait(self._tick_seconds)


_scheduler: TradeLogExportScheduler | None = None


def ensure_trade_log_export_scheduler_running(
    tick_seconds: float = DEFAULT_TICK_SECONDS,
) -> TradeLogExportScheduler:
    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = TradeLogExportScheduler(tick_seconds=tick_seconds)
        _scheduler.start()
    return _scheduler


def stop_trade_log_export_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.stop()
        _scheduler = None
