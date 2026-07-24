"""Windows Service wrapper for the backend process, via pywin32.

Why a Windows Service rather than a scheduled task or a manually-launched
console app: the engine must survive logoff, must start automatically on
boot, and must be trivially inspectable via `services.msc` / `sc query` —
all standard Windows Service behavior, and the most "traditional app"-shaped
way to keep a background process alive on this platform. Installing/starting
uvicorn itself inside the service process (rather than shelling out to a
separate uvicorn process) means log output flows through the standard
Windows Service event log path too.

Install (as Administrator):
    python ops/windows_service/service.py install
    python ops/windows_service/service.py start

Remove:
    python ops/windows_service/service.py stop
    python ops/windows_service/service.py remove

Debug (run in foreground, no service manager involved):
    python ops/windows_service/service.py debug
"""

from __future__ import annotations

import sys
from pathlib import Path

import servicemanager
import win32service
import win32serviceutil

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))


class TradingBotService(win32serviceutil.ServiceFramework):
    _svc_name_ = "TradingBotEngine"
    _svc_display_name_ = "Trading Bot Engine"
    _svc_description_ = (
        "Broker-agnostic trading platform backend (API + workers). "
        "Runs independently of any browser/UI session — closing the "
        "dashboard must never affect a live position."
    )

    def __init__(self, args) -> None:
        super().__init__(args)
        self._uvicorn_server = None

    def SvcStop(self) -> None:
        # Shutdown is entirely driven by uvicorn's own should_exit flag —
        # SvcDoRun blocks inside server.run() until it sees this, at which
        # point that call returns and the service reports itself stopped.
        # No separate wait-event is needed on top of that.
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True

    def SvcDoRun(self) -> None:
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        self._run_server()

    def _run_server(self) -> None:
        import uvicorn

        from app.main import app  # noqa: E402  (path inserted above)

        config = uvicorn.Config(app, host="127.0.0.1", port=5000, log_level="info")
        server = uvicorn.Server(config)
        self._uvicorn_server = server
        server.run()


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(TradingBotService)
