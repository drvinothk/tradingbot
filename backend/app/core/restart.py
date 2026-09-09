"""Schedule a graceful restart of this backend's own systemd unit.

Extracted 2026-09-09 from `api.v1.system_settings._schedule_restart` so more
than one caller can share it: the `/restart-backend` endpoint (which keeps its
own open-live-position 409 pre-check), the Kill Switch endpoint (which has just
flattened every live position, so there is nothing left to protect), and the
broker OAuth callbacks' post-login background work (a manual reconnect
deliberately wants a guaranteed-clean process).

Platform-guarded: the deployment target is a single Linux box whose `ubuntu`
account already has unrestricted passwordless sudo (confirmed live 2026-08-20).
Off Linux -- a Windows dev machine, or a CI runner where an autouse fixture
should be stubbing this anyway -- it logs and no-ops rather than shelling out
to a `systemctl` that isn't there.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import threading
import time as _time

logger = logging.getLogger("app.core.restart")

# The unit this box's app process runs under. Hardcoded for the same reason
# `api.v1.system_settings` hardcodes it -- one real deployment target, and the
# platform guard below trips long before this string matters anywhere else.
RESTART_SERVICE_NAME = "trading-bot.service"
RESTART_DELAY_SECONDS = 3.0


def schedule_backend_restart(reason: str = "") -> bool:
    """Fire `sudo systemctl restart trading-bot.service` after a short delay,
    on a daemon thread, so the caller's HTTP response (or background work) can
    finish first. Goes through real `systemctl restart` -- not a raw
    `os._exit` -- so the app's graceful-shutdown path runs (singleton lock
    released, schedulers stopped).

    Returns `True` if a restart was actually scheduled, `False` if it was a
    no-op (not Linux). Never raises.
    """
    if platform.system() != "Linux":
        logger.info(
            "schedule_backend_restart(%r) is a no-op off Linux -- not restarting.", reason
        )
        return False

    logger.warning(
        "Backend restart scheduled in ~%.0fs (reason: %s)", RESTART_DELAY_SECONDS, reason
    )

    def _run() -> None:
        _time.sleep(RESTART_DELAY_SECONDS)
        subprocess.run(  # noqa: S603, S607 - deliberate, fixed argv, no shell
            ["sudo", "systemctl", "restart", RESTART_SERVICE_NAME], check=False
        )

    threading.Thread(target=_run, daemon=True).start()
    return True
