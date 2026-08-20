"""Ops-Hardening Phase 7. The DB-backed instrument firewall -- see
`InstrumentFirewallConfig`'s own docstring (`app.domain.ops.models`) and
`broker_adapter.composition.get_execution_broker`'s own Phase 7 section for
how this is actually enforced at dispatch time. Gated behind `risk.override`
(not `session.start`, which Phase 4's market-data endpoints use) since this
directly controls which instruments real money can flow to -- a
risk-governance action, not a connectivity toggle.
"""

from __future__ import annotations

import platform
import subprocess
import threading
import time as time_module

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.core.security.rbac import require_permission
from app.domain.audit.models import ActorType, EventCategory
from app.domain.execution.models import Order, OrderMode, Position, PositionStatus
from app.domain.identity.models import User
from app.domain.market.models import OptionContract
from app.domain.ops.models import DEFAULT_ACTIVE_LIVE_INSTRUMENTS, InstrumentFirewallConfig
from app.modules.audit_service.service import record_event

router = APIRouter(prefix="/system-settings", tags=["system-settings"])

# The service unit this box's app process runs under -- the User=ubuntu
# account it runs as already has unrestricted passwordless sudo (confirmed
# live, 2026-08-20), so no new sudoers grant is needed for the app to
# restart its own unit. Hardcoded, not settings-driven -- this is genuinely
# the only deployment target today (see CLAUDE.md), and a Windows dev
# machine hits the platform guard below long before this string matters.
_RESTART_SERVICE_NAME = "trading-bot.service"
_RESTART_DELAY_SECONDS = 3.0

# This system only ever trades these two underlyings -- same scoping as
# api.v1.shoonya._KNOWN_UNDERLYINGS / broker_adapter/shoonya/adapter.py's
# KNOWN_UNDERLYINGS.
RECOGNIZED_FIREWALL_INSTRUMENTS = ("NIFTY", "BANKNIFTY")


class InstrumentFirewallOut(BaseModel):
    active_live_instruments: list[str]
    recognized_instruments: list[str]


class SetInstrumentFirewallRequest(BaseModel):
    active_live_instruments: list[str]


@router.get("/instrument-firewall", response_model=InstrumentFirewallOut)
def get_instrument_firewall(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("risk.override")),
) -> InstrumentFirewallOut:
    config = (
        db.query(InstrumentFirewallConfig)
        .filter(InstrumentFirewallConfig.workspace_id == user.workspace_id)
        .one_or_none()
    )
    active = config.active_live_instruments if config is not None else list(
        DEFAULT_ACTIVE_LIVE_INSTRUMENTS
    )
    return InstrumentFirewallOut(
        active_live_instruments=active,
        recognized_instruments=list(RECOGNIZED_FIREWALL_INSTRUMENTS),
    )


@router.patch("/instrument-firewall", response_model=InstrumentFirewallOut)
def set_instrument_firewall(
    body: SetInstrumentFirewallRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("risk.override")),
) -> InstrumentFirewallOut:
    unknown = [s for s in body.active_live_instruments if s not in RECOGNIZED_FIREWALL_INSTRUMENTS]
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unrecognized instrument(s) {unknown} -- must be a subset of "
            f"{RECOGNIZED_FIREWALL_INSTRUMENTS}",
        )

    config = (
        db.query(InstrumentFirewallConfig)
        .filter(InstrumentFirewallConfig.workspace_id == user.workspace_id)
        .one_or_none()
    )
    previous = config.active_live_instruments if config is not None else list(
        DEFAULT_ACTIVE_LIVE_INSTRUMENTS
    )
    if config is None:
        config = InstrumentFirewallConfig(
            workspace_id=user.workspace_id,
            active_live_instruments=body.active_live_instruments,
        )
        db.add(config)
    else:
        config.active_live_instruments = body.active_live_instruments
    db.flush()

    record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        event_category=EventCategory.RISK_DECISION,
        event_type="instrument_firewall.updated",
        entity_type="instrument_firewall_config",
        entity_id=config.id,
        payload={"previous": previous, "new": body.active_live_instruments},
    )
    db.commit()
    db.refresh(config)
    return InstrumentFirewallOut(
        active_live_instruments=config.active_live_instruments,
        recognized_instruments=list(RECOGNIZED_FIREWALL_INSTRUMENTS),
    )


class RestartBackendRequest(BaseModel):
    reason: str
    force: bool = False


class OpenLivePositionOut(BaseModel):
    trading_session_id: str
    contract_symbol: str
    qty: int


def _schedule_restart() -> None:
    """Runs `systemctl restart` on a delay, in a daemon thread, so the HTTP
    response for the request that triggered this actually reaches the
    client before the process goes down. Goes through real `systemctl
    restart` (not a raw self `os._exit`) so the app's own graceful-shutdown
    path runs first -- `KillSignal=SIGINT`/`TimeoutStopSec=15` in the unit
    file, the same clean "Process singleton lock released" shutdown this
    box has shown on every restart tonight -- rather than a hard kill.
    Split out as its own top-level function so tests can monkeypatch it
    instead of a real restart ever firing under pytest.
    """

    def _run() -> None:
        time_module.sleep(_RESTART_DELAY_SECONDS)
        subprocess.run(  # noqa: S603, S607 - deliberate, fixed argv, no shell
            ["sudo", "systemctl", "restart", _RESTART_SERVICE_NAME], check=False
        )

    threading.Thread(target=_run, daemon=True).start()


@router.post("/restart-backend")
def restart_backend(
    body: RestartBackendRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("risk.override")),
) -> dict:
    """2026-08-20 addition -- every backend restart before this required SSH
    + `sudo systemctl restart` by hand. A restart pauses `PositionManager`'s
    stop/target/trail checks for the few seconds the process is down, so
    this refuses by default when any session has a genuinely live open
    position (`Position.status == OPEN` whose *opening* `Order.mode ==
    LIVE` -- never inferred from current session mode, same reasoning
    `broker_adapter.composition._position_opened_live` already established)
    -- `force=true` is the deliberate "yes, I understand" override.

    Platform-guarded: this box's `ubuntu` account already has unrestricted
    passwordless sudo (confirmed live), so no new grant was needed for the
    app to restart its own systemd unit -- but a local Windows dev machine
    has no `systemctl` at all, so this refuses cleanly there rather than
    trying and failing inside the subprocess call.
    """
    if platform.system() != "Linux":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "restart-backend is only supported on the Linux deployment (no systemctl here).",
        )

    open_live = (
        db.query(Position, OptionContract.symbol)
        .join(Order, Order.id == Position.opening_order_id)
        .join(OptionContract, OptionContract.id == Position.option_contract_id)
        .filter(Position.status == PositionStatus.OPEN, Order.mode == OrderMode.LIVE)
        .all()
    )
    if open_live and not body.force:
        positions = [
            OpenLivePositionOut(
                trading_session_id=str(position.trading_session_id),
                contract_symbol=symbol,
                qty=position.qty,
            ).model_dump()
            for position, symbol in open_live
        ]
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "message": (
                    f"{len(positions)} open live position(s) would lose stop/target/trail "
                    "monitoring for the duration of the restart. Pass force=true to proceed "
                    "anyway."
                ),
                "open_live_positions": positions,
            },
        )

    record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        event_category=EventCategory.MANUAL_OVERRIDE,
        event_type="system.restart_requested",
        payload={"reason": body.reason, "force": body.force, "open_live_positions": len(open_live)},
    )
    db.commit()

    _schedule_restart()
    return {"ok": True, "message": f"Restart scheduled in ~{_RESTART_DELAY_SECONDS:.0f}s."}
