"""Ops-Hardening Phase 7. The DB-backed instrument firewall -- see
`InstrumentFirewallConfig`'s own docstring (`app.domain.ops.models`) and
`broker_adapter.composition.get_execution_broker`'s own Phase 7 section for
how this is actually enforced at dispatch time. Gated behind `risk.override`
(not `session.start`, which Phase 4's market-data endpoints use) since this
directly controls which instruments real money can flow to -- a
risk-governance action, not a connectivity toggle.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.core.security.rbac import require_permission
from app.domain.audit.models import ActorType, EventCategory
from app.domain.identity.models import User
from app.domain.ops.models import DEFAULT_ACTIVE_LIVE_INSTRUMENTS, InstrumentFirewallConfig
from app.modules.audit_service.service import record_event

router = APIRouter(prefix="/system-settings", tags=["system-settings"])

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
