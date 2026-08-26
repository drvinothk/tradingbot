"""Ops-Hardening Phase 3. `TradeLogExportScheduler` — same background-thread
shape as `scheduler.health_check.HealthCheckScheduler` (daemon thread, a
`stop_event` for clean shutdown, `run_once()` exposed separately so tests can
drive it deterministically), triggering once daily at `EXPORT_TIME` (15:35
IST — chosen so intraday square-offs, which fire by `cutoff_time`/15:09 at
the latest, are fully settled in the DB before this reads `TradeOutcome`).

The "once daily" trigger is transition-detection, not a fixed short
interval — same shape `market_data_scheduler.MarketDataScheduler` already
uses for its own time-of-day boundaries: `DailyAtTimeScheduler`'s shared
`_last_run_date` tracked in-memory, compared each tick against
`now_ist().date()`. This is deliberately **not** persisted across
restarts — a restart after 15:35 re-triggers on the very next tick, which
is safe (not a duplicate-data bug) purely because
`exporter.export_trade_log_for_workspace`'s own append step is idempotent
(see that module's docstring). If `export_completed_trades_for_day` raises,
`_last_run_date` is deliberately left unset so the next tick retries — a
transient DB hiccup should mean "try again shortly," never "silently skip
today's export."
"""

from __future__ import annotations

import logging
from datetime import time

from app.core.clock import now_ist
from app.modules.reporting.exporter import export_completed_trades_for_day
from app.modules.scheduler.base import DailyAtTimeScheduler

logger = logging.getLogger("app.reporting.export_scheduler")

# How often the loop wakes up to check whether it's past EXPORT_TIME yet —
# a report export isn't latency-sensitive to the second the way market-data
# phase transitions are, so a coarser tick than MarketDataScheduler's 30s is
# fine; this still lands the export within a minute of 15:35.
DEFAULT_TICK_SECONDS = 60.0

EXPORT_TIME = time(15, 35)


class TradeLogExportScheduler(DailyAtTimeScheduler):
    _cycle_failed_log_message = "trade log export cycle failed"

    def __init__(self, tick_seconds: float = DEFAULT_TICK_SECONDS) -> None:
        super().__init__(logger, EXPORT_TIME, tick_seconds=tick_seconds)

    def _do_run(self) -> None:
        export_completed_trades_for_day(now_ist().date())


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
