"""Ops-Hardening Phase 7. `ContractSyncScheduler` -- same `threading.Thread`
+ `stop_event` + once-per-day-guard shape as `session.bootstrapper
.DailyBootstrapScheduler`, triggering once daily at `CONTRACT_SYNC_TIME`
(08:30 IST, ahead of the Phase 4 bootstrap's 09:00 and Phase 6's spawn that
follows it). Closes a real gap: `sync_instrument_master` (`scheduler
.instrument_sync`) was previously only ever called once at process startup
or once per manual Shoonya OAuth login -- there was no daily refresh at all,
so `option_contracts` could go stale for days between a human's logins,
which the Phase 6 auto-spawner's `resolve_nearest_expiry` depends on being
fresh every trading morning.

No-ops (logs, doesn't fail) when Shoonya isn't connected yet at 08:30 --
this system's "Hard ON" switch is the human's own daily manual broker
login, per explicit design; a sync against the mock broker's synthetic
universe would be pointless (already covered once at startup by
`app.main._sync_mock_instrument_universe`) and misleading to log as a real
refresh.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, time

from app.core.clock import now_ist
from app.core.db.session import session_scope
from app.modules.broker_adapter.composition import get_broker, is_shoonya_configured
from app.modules.scheduler.instrument_sync import sync_instrument_master

logger = logging.getLogger("app.scheduler.contract_sync_scheduler")

CONTRACT_SYNC_TIME = time(8, 30)


def run_contract_sync() -> None:
    if not is_shoonya_configured():
        logger.info(
            "Contract sync: Shoonya not connected -- skipping, existing local "
            "option_contracts data used as-is until the next reconnect/sync."
        )
        return

    with session_scope() as db:
        log = sync_instrument_master(db, get_broker(), ["NFO"])
        logger.warning(
            "Contract sync: status=%s instruments_updated=%d contracts_added=%d "
            "contracts_expired=%d",
            log.status,
            log.instruments_updated,
            log.contracts_added,
            log.contracts_expired,
        )


class ContractSyncScheduler:
    def __init__(self, tick_seconds: float = 60.0) -> None:
        self._tick_seconds = tick_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sync_date: date | None = None

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
        if now.time() < CONTRACT_SYNC_TIME or self._last_sync_date == now.date():
            return
        run_contract_sync()
        self._last_sync_date = now.date()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - a background loop must never die silently-crashed
                logger.exception("contract sync cycle failed")
            self._stop_event.wait(self._tick_seconds)


_scheduler: ContractSyncScheduler | None = None


def ensure_contract_sync_scheduler_running(tick_seconds: float = 60.0) -> ContractSyncScheduler:
    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = ContractSyncScheduler(tick_seconds=tick_seconds)
        _scheduler.start()
    return _scheduler


def stop_contract_sync_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.stop()
        _scheduler = None
