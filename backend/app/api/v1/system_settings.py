"""Ops-Hardening Phase 7. The DB-backed instrument firewall -- see
`InstrumentFirewallConfig`'s own docstring (`app.domain.ops.models`) and
`broker_adapter.composition.get_execution_broker`'s own Phase 7 section for
how this is actually enforced at dispatch time. Gated behind `risk.override`
(not `session.start`, which Phase 4's market-data endpoints use) since this
directly controls which instruments real money can flow to -- a
risk-governance action, not a connectivity toggle.

2026-08-20: `GET`/`PATCH /system-settings/daily-limits` added, same file,
same router, same `risk.override` gate -- the global "total daily budget"/
"total lots per day" settings surface from the UI dashboard plan. See
`GlobalDailyLimitsConfig`'s own docstring (`app.domain.ops.models`) for why
it's a new table rather than a rename of an existing risk-engine concept.
"""

from __future__ import annotations

import platform
import subprocess
import threading
import time as time_module
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.core.security.rbac import require_permission
from app.domain.audit.models import ActorType, EventCategory
from app.domain.execution.models import Order, OrderMode, Position, PositionStatus
from app.domain.identity.models import User
from app.domain.market.models import OptionContract
from app.domain.ops.models import (
    DEFAULT_ACTIVE_LIVE_INSTRUMENTS,
    DEFAULT_DAILY_BUDGET_AMOUNT,
    DEFAULT_DAILY_MAX_LOTS,
    GlobalDailyLimitsConfig,
    InstrumentFirewallConfig,
)
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

# Generated once per process, at import time -- i.e. once per real backend
# boot. `POST /restart-backend` echoes this back so the frontend can remember
# "the boot this restart was requested from"; `GET /boot-status` (unauthenticated
# poll target, see its own docstring) returns whatever the *current* process's
# value is. A restart genuinely landing is "the value returned by /boot-status
# is no longer the one /restart-backend returned" -- 2026-08-24 addition, closing
# the gap where the UI's "Restart scheduled in ~3s" message never updated to
# confirm the new process actually came back up (previously the frontend just
# stored the scheduling response's message and never polled anything further).
_BOOT_ID = str(uuid.uuid4())

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


class DailyLimitsOut(BaseModel):
    daily_budget_amount: float
    daily_max_lots: int


class SetDailyLimitsRequest(BaseModel):
    daily_budget_amount: float = Field(gt=0)
    daily_max_lots: int = Field(gt=0)


@router.get("/daily-limits", response_model=DailyLimitsOut)
def get_daily_limits(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("risk.override")),
) -> DailyLimitsOut:
    """2026-08-20 addition -- global "total daily budget" / "total lots per
    day" settings surface, same GET-returns-row-or-documented-default shape
    as get_instrument_firewall above. See GlobalDailyLimitsConfig's own
    docstring (app.domain.ops.models) for why this is a new table, not a
    rename of RiskLimitConfig.per_trade_lot_cap or
    TradingSession.budget_amount -- and why it's a settings surface only,
    not yet wired into any pre-trade enforcement path.
    """
    config = (
        db.query(GlobalDailyLimitsConfig)
        .filter(GlobalDailyLimitsConfig.workspace_id == user.workspace_id)
        .one_or_none()
    )
    if config is None:
        return DailyLimitsOut(
            daily_budget_amount=DEFAULT_DAILY_BUDGET_AMOUNT,
            daily_max_lots=DEFAULT_DAILY_MAX_LOTS,
        )
    return DailyLimitsOut(
        daily_budget_amount=float(config.daily_budget_amount),
        daily_max_lots=config.daily_max_lots,
    )


def _upsert_daily_limits(
    db: Session,
    workspace_id: uuid.UUID,
    daily_budget_amount: float,
    daily_max_lots: int,
) -> tuple[GlobalDailyLimitsConfig, dict]:
    """select-then-insert, guarded against the real DB unique constraint on
    `GlobalDailyLimitsConfig.workspace_id`. Two concurrent `PATCH` calls
    landing on the same not-yet-seeded workspace can both run their own
    `SELECT` and see `config is None` before either commits -- the loser's
    `INSERT` then hits a genuine `IntegrityError` instead of silently
    succeeding (previously unguarded: this would have surfaced as an
    unhandled 500). Recovered by treating that specific conflict as "someone
    else just created this row -- update it instead", the same effective
    outcome a real upsert would produce. No existing IntegrityError-recovery
    pattern was found elsewhere in this codebase to reuse (`InstrumentFirewallConfig`'s
    own near-identical select-then-insert in `set_instrument_firewall` above
    has the same latent race, out of scope for this fix) -- this is a new,
    narrowly-scoped one.
    """
    config = (
        db.query(GlobalDailyLimitsConfig)
        .filter(GlobalDailyLimitsConfig.workspace_id == workspace_id)
        .one_or_none()
    )
    previous = (
        {
            "daily_budget_amount": float(config.daily_budget_amount),
            "daily_max_lots": config.daily_max_lots,
        }
        if config is not None
        else {
            "daily_budget_amount": DEFAULT_DAILY_BUDGET_AMOUNT,
            "daily_max_lots": DEFAULT_DAILY_MAX_LOTS,
        }
    )
    if config is None:
        config = GlobalDailyLimitsConfig(
            workspace_id=workspace_id,
            daily_budget_amount=daily_budget_amount,
            daily_max_lots=daily_max_lots,
        )
        db.add(config)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            config = (
                db.query(GlobalDailyLimitsConfig)
                .filter(GlobalDailyLimitsConfig.workspace_id == workspace_id)
                .one()
            )
            config.daily_budget_amount = daily_budget_amount
            config.daily_max_lots = daily_max_lots
            db.flush()
    else:
        config.daily_budget_amount = daily_budget_amount
        config.daily_max_lots = daily_max_lots
        db.flush()
    return config, previous


@router.patch("/daily-limits", response_model=DailyLimitsOut)
def set_daily_limits(
    body: SetDailyLimitsRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("risk.override")),
) -> DailyLimitsOut:
    config, previous = _upsert_daily_limits(
        db, user.workspace_id, body.daily_budget_amount, body.daily_max_lots
    )

    record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        event_category=EventCategory.RISK_DECISION,
        event_type="global_daily_limits.updated",
        entity_type="global_daily_limits_config",
        entity_id=config.id,
        payload={
            "previous": previous,
            "new": {
                "daily_budget_amount": body.daily_budget_amount,
                "daily_max_lots": body.daily_max_lots,
            },
        },
    )
    db.commit()
    db.refresh(config)
    return DailyLimitsOut(
        daily_budget_amount=float(config.daily_budget_amount),
        daily_max_lots=config.daily_max_lots,
    )


class RestartBackendRequest(BaseModel):
    reason: str
    force: bool = False


class BootStatusOut(BaseModel):
    boot_id: str


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


@router.get("/boot-status", response_model=BootStatusOut)
def boot_status() -> BootStatusOut:
    """Deliberately unauthenticated -- the frontend polls this in a tight
    loop right through the middle of a restart to detect the new process
    coming up (see `_BOOT_ID`'s own docstring), and a lapsed/invalidated
    session cookie during that exact window must never be mistaken for "the
    backend is still down." The value returned carries no sensitive
    information (an opaque per-process UUID), same posture as the existing
    unauthenticated `/health` endpoint in `app.main`.
    """
    return BootStatusOut(boot_id=_BOOT_ID)


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
    return {
        "ok": True,
        "message": f"Restart scheduled in ~{_RESTART_DELAY_SECONDS:.0f}s.",
        "boot_id": _BOOT_ID,
    }
