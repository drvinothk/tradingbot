"""Shared background-thread scheduler shape. `start`/`stop`/`is_alive`/
`run_once`/`_loop` were identical, copy-pasted independently into every
scheduler in this codebase that runs on a fixed interval
(`HealthCheckScheduler`, `ReconciliationLockRecoveryScheduler`) — this is
that shared shape, factored out 2026-08-26. Subclasses implement
`_run_cycle(db)` (the actual per-cycle work) and set `_cycle_failed_log_message`
to keep their own existing log text on an uncaught exception.

The "daily-at-time" schedulers (`ContractSyncScheduler`,
`TradeLogExportScheduler`, `DailyBootstrapScheduler`) have a genuinely
different `run_once` shape (a once-per-day guard before the real work, not
a plain fixed interval) and are not covered by this base class.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, time

from sqlalchemy.orm import Session

from app.core.clock import now_ist
from app.core.db.session import SessionFactory, session_scope


class IntervalScheduler:
    _cycle_failed_log_message: str = "scheduler cycle failed"

    def __init__(
        self,
        logger: logging.Logger,
        interval_seconds: float,
        session_factory: SessionFactory = session_scope,
    ) -> None:
        self._logger = logger
        self._interval_seconds = interval_seconds
        self._session_factory = session_factory
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_seconds + 5)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_once(self) -> None:
        with self._session_factory() as db:
            self._run_cycle(db)

    def _run_cycle(self, db: Session) -> None:
        raise NotImplementedError

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - a background loop must never die silently-crashed
                self._logger.exception(self._cycle_failed_log_message)
            self._stop_event.wait(self._interval_seconds)


class DailyAtTimeScheduler:
    """Shared shape for a daemon-thread scheduler that does its real work
    once per IST calendar day, at or after a fixed time-of-day —
    `start`/`stop`/`is_alive`/`run_once`'s own "already ran today, or not
    time yet" guard/`_loop` are identical across every such scheduler in
    this codebase (`ContractSyncScheduler`, `TradeLogExportScheduler`,
    `DailyBootstrapScheduler` — previously copy-pasted into each
    independently, differing only in the trigger time, the log message, and
    the actual once-daily work). Subclasses implement `_do_run()`.
    """

    _cycle_failed_log_message: str = "scheduler cycle failed"

    def __init__(self, logger: logging.Logger, run_time: time, tick_seconds: float = 60.0) -> None:
        self._logger = logger
        self._run_time = run_time
        self._tick_seconds = tick_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_run_date: date | None = None

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
        if now.time() < self._run_time or self._last_run_date == now.date():
            return
        self._do_run()
        self._last_run_date = now.date()

    def _do_run(self) -> None:
        raise NotImplementedError

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - a background loop must never die silently-crashed
                self._logger.exception(self._cycle_failed_log_message)
            self._stop_event.wait(self._tick_seconds)
