"""`ScripMasterRefreshScheduler` — same `threading.Thread`-with-`stop_event`
shape as `scheduler.health_check.HealthCheckScheduler`: checks hourly
whether the scrip master's own last successful sync is more than a day old
and re-runs it if so. A small dedicated timer rather than folding this into
the unrelated NTP/disk health-check loop, since the two concerns (system
clock/disk health vs. a market-data symbol mapping's freshness) share no
reasoning, only the shape of "a periodic background check."

No existing "recurring daily job" pattern exists elsewhere in this codebase
to hook into instead — `scheduler/instrument_sync.py`'s own docstring calls
itself "daily" but is actually only ever invoked once, at process startup
(confirmed while building this module); this scheduler is a real recurring
timer, not an aspirational docstring.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

from app.core.db.session import session_scope
from app.modules.market_data.scrip_master import ScripMasterService

logger = logging.getLogger("app.market_data.scrip_master_scheduler")

DEFAULT_CHECK_INTERVAL_SECONDS = 3600.0
DEFAULT_REFRESH_AFTER = timedelta(hours=24)


class ScripMasterRefreshScheduler:
    def __init__(
        self,
        scrip_master: ScripMasterService,
        check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        refresh_after: timedelta = DEFAULT_REFRESH_AFTER,
    ) -> None:
        self._scrip_master = scrip_master
        self._check_interval_seconds = check_interval_seconds
        self._refresh_after = refresh_after
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._check_interval_seconds + 5)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_once(self) -> None:
        last = self._scrip_master.last_synced_at
        if last is not None and datetime.now(UTC) - last < self._refresh_after:
            return
        self._scrip_master.fetch_and_parse()
        with session_scope() as db:
            log = self._scrip_master.sync_to_db(db)
            logger.info(
                "Scrip master refresh: status=%s rows_parsed=%d rows_mapped=%d",
                log.status,
                log.rows_parsed,
                log.rows_mapped,
            )

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - a background loop must never die silently-crashed
                logger.exception("Scrip master refresh cycle failed")
            self._stop_event.wait(self._check_interval_seconds)


_scheduler: ScripMasterRefreshScheduler | None = None


def ensure_scrip_master_refresh_scheduler_running(
    scrip_master: ScripMasterService,
    check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
) -> ScripMasterRefreshScheduler:
    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = ScripMasterRefreshScheduler(
            scrip_master, check_interval_seconds=check_interval_seconds
        )
        _scheduler.start()
    return _scheduler


def stop_scrip_master_refresh_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.stop()
        _scheduler = None
