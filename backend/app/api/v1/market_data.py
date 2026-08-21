"""Market-data provider preference — Ops-Hardening Phase 4. A manual
override on top of `FailoverMarketDataProvider`'s own automatic health-based
switching, not a replacement for it — see `MarketDataProviderPreference`'s
own docstring (`app.domain.ops.models`) and `FailoverMarketDataProvider
.set_manual_override`'s own docstring for the full design.

Gated behind `session.start`, matching `api.v1.shoonya`'s own broker-
connectivity endpoints (login/status/connect) — this is the same "who
manages which feed is live" concern, not a risk-governance action.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db.session import get_db
from app.core.security.rbac import require_permission
from app.domain.audit.models import ActorType, EventCategory
from app.domain.identity.models import User
from app.domain.ops.models import MarketDataProviderPreference
from app.modules.audit_service.service import record_event
from app.modules.market_data import diagnostic_session
from app.modules.market_data.provider_composition import get_market_data_provider
from app.modules.market_data.providers.failover import FailoverMarketDataProvider

logger = logging.getLogger("app.api.market_data")

router = APIRouter(prefix="/market-data", tags=["market-data"])

# Matches provider_composition._RECOGNIZED_FAILOVER_BACKUPS's own scope --
# TrueData isn't a supported failover leg yet, "mock" is never a real
# provider to force, and a provider can't be its own alternative.
# "angel_one" archived 2026-08-21 (kept aside pending an IP/proxy fix, see
# CLAUDE.md's Angel One section) -- deliberately not removed from
# provider_composition._RECOGNIZED_FAILOVER_BACKUPS or AngelOneSettings,
# only from this UI-driven override list, so reactivating it later is a
# one-line env-var change (MARKET_DATA_FAILOVER_BACKUP_PROVIDER=angel_one),
# not a code change -- same "explicit config, not a casual dropdown click"
# reasoning as MARKET_DATA_ALLOW_OFFHOURS_TESTING.
RECOGNIZED_OVERRIDE_PROVIDERS = ("shoonya",)


class ProviderPreferenceOut(BaseModel):
    active_provider: str | None
    # What FailoverMarketDataProvider is actually doing right now, if a
    # live failover-wrapped singleton exists -- distinct from
    # active_provider (the persisted preference) since the two can
    # legitimately disagree (e.g. failover isn't currently active, or the
    # preference was just saved but not yet applied to a running process).
    live_active_leg: str | None


class SetProviderPreferenceRequest(BaseModel):
    active_provider: str | None  # None clears the override


def _find_failover_provider() -> FailoverMarketDataProvider | None:
    provider = get_market_data_provider()
    inner = getattr(provider, "_inner", provider)
    return inner if isinstance(inner, FailoverMarketDataProvider) else None


@router.get("/provider-preference", response_model=ProviderPreferenceOut)
def get_provider_preference(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.start")),
) -> ProviderPreferenceOut:
    pref = (
        db.query(MarketDataProviderPreference)
        .filter(MarketDataProviderPreference.workspace_id == user.workspace_id)
        .one_or_none()
    )
    failover = _find_failover_provider()
    return ProviderPreferenceOut(
        active_provider=pref.active_provider if pref is not None else None,
        live_active_leg=failover.active_provider_name if failover is not None else None,
    )


@router.patch("/provider-preference", response_model=ProviderPreferenceOut)
def set_provider_preference(
    body: SetProviderPreferenceRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.start")),
) -> ProviderPreferenceOut:
    """Persists the preference first, then applies it live if a
    failover-wrapped singleton currently exists -- if not (failover
    disabled, or provider is "mock"), the preference is saved and will only
    take effect the next time failover *is* active; that distinction is
    logged clearly rather than silently no-op'd, so it isn't mistaken for
    the toggle simply not having worked.
    """
    if (
        body.active_provider is not None
        and body.active_provider not in RECOGNIZED_OVERRIDE_PROVIDERS
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"active_provider must be one of {RECOGNIZED_OVERRIDE_PROVIDERS} or null "
            "to clear the override",
        )

    pref = (
        db.query(MarketDataProviderPreference)
        .filter(MarketDataProviderPreference.workspace_id == user.workspace_id)
        .one_or_none()
    )
    previous = pref.active_provider if pref is not None else None
    if pref is None:
        pref = MarketDataProviderPreference(
            workspace_id=user.workspace_id, active_provider=body.active_provider
        )
        db.add(pref)
    else:
        pref.active_provider = body.active_provider
    db.flush()

    record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        event_category=EventCategory.MARKET_DATA_CONNECTIVITY,
        event_type="market_data_provider_preference.updated",
        entity_type="market_data_provider_preference",
        entity_id=pref.id,
        payload={"previous": previous, "new": body.active_provider},
    )

    failover = _find_failover_provider()
    live_active_leg: str | None = None
    if failover is not None:
        try:
            failover.set_manual_override(body.active_provider)
            live_active_leg = failover.active_provider_name
        except (ValueError, RuntimeError) as exc:
            db.commit()  # the preference itself is still valid and saved
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    else:
        logger.warning(
            "Market-data provider preference saved (%r) but no live failover-wrapped "
            "provider exists right now (failover disabled, or provider is mock) -- "
            "will take effect next time failover is active, not immediately.",
            body.active_provider,
        )

    db.commit()
    db.refresh(pref)
    return ProviderPreferenceOut(
        active_provider=pref.active_provider, live_active_leg=live_active_leg
    )


# ---------- WS quality diagnostic (Market Terminal "Test Default"/"Test
# Failback"/"Both") -- see diagnostic_session.py's own module docstring for
# the full design, especially why "default"/"failback" never name a broker.

_DIAGNOSTIC_ROLES = ("default", "failback")


class DiagnosticModeRequest(BaseModel):
    mode: str  # "default" | "failback" | "both"


def _roles_for_mode(mode: str) -> list[str]:
    if mode == "both":
        return list(_DIAGNOSTIC_ROLES)
    if mode in _DIAGNOSTIC_ROLES:
        return [mode]
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST, "mode must be one of ('default', 'failback', 'both')"
    )


@router.post("/diagnostic/start")
def start_diagnostic(
    body: DiagnosticModeRequest,
    user: User = Depends(require_permission("session.start")),
) -> dict:
    """`start_many` validates every requested role before starting any of
    them — see its own docstring for why "both" mode must be atomic (an
    error response must mean nothing started, not "half of it did").
    """
    try:
        return diagnostic_session.start_many(_roles_for_mode(body.mode), user.workspace_id)
    except (RuntimeError, diagnostic_session.UnsupportedFailbackProviderError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/diagnostic/stop")
def stop_diagnostic(
    body: DiagnosticModeRequest,
    user: User = Depends(require_permission("session.start")),
) -> dict:
    return {role: diagnostic_session.stop(role) for role in _roles_for_mode(body.mode)}


@router.get("/diagnostic/status")
def get_diagnostic_status(user: User = Depends(require_permission("session.start"))) -> dict:
    return diagnostic_session.status()
