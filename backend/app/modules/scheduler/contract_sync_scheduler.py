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
from datetime import time

from app.core.db.session import session_scope
from app.domain.market.models import SyncStatus
from app.modules.broker_adapter.composition import get_broker, is_execution_broker_connected
from app.modules.ops import weekend_rest
from app.modules.scheduler.base import DailyAtTimeScheduler
from app.modules.scheduler.instrument_sync import sync_instrument_master

logger = logging.getLogger("app.scheduler.contract_sync_scheduler")

CONTRACT_SYNC_TIME = time(8, 30)


def run_contract_sync() -> None:
    # Weekend rest mode: no point syncing the contract master on a dormant
    # weekend. No-op Mon-Fri.
    if not weekend_rest.is_system_awake():
        logger.info("Contract sync: skipped -- weekend rest mode (no signed-in user).")
        return

    if not is_execution_broker_connected():
        logger.info(
            "Contract sync: Shoonya not connected -- skipping, existing local "
            "option_contracts data used as-is until the next reconnect/sync."
        )
        return

    with session_scope() as db:
        log = sync_instrument_master(db, get_broker(), ["NFO"])
        # .info for a normal success -- routine daily status, not something
        # worth interrupting a WARNING+-only view of the logs for; a real
        # PARTIAL/FAILED sync still logs at .warning so it stays visible.
        log_fn = logger.info if log.status == SyncStatus.SUCCESS else logger.warning
        log_fn(
            "Contract sync: status=%s instruments_updated=%d contracts_added=%d "
            "contracts_expired=%d",
            log.status,
            log.instruments_updated,
            log.contracts_added,
            log.contracts_expired,
        )


class ContractSyncScheduler(DailyAtTimeScheduler):
    _cycle_failed_log_message = "contract sync cycle failed"

    def __init__(self, tick_seconds: float = 60.0) -> None:
        super().__init__(logger, CONTRACT_SYNC_TIME, tick_seconds=tick_seconds)

    def _do_run(self) -> None:
        run_contract_sync()


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
