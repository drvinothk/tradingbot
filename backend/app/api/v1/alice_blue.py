"""Alice Blue OAuth login: `GET /login-url` gives the frontend the URL to
send the user's browser to; `GET /callback` is what Alice Blue's own site
redirects that browser back to afterward, per `ALICEBLUE_REDIRECT_URL`. Same
shape as `api.v1.shoonya`'s identical pair — see that module's own
docstring for why the callback still requires an already-logged-in-to-this-
app session (a plain browser navigation carries the session cookie fine
under `SameSite=Lax`).

Market-data only — no order/account endpoints exist here, and none ever
will (see `AliceBlueMarketDataProvider`'s own docstring for the structural
reason: `BaseMarketDataProvider`'s interface has no order methods at all).
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.db.session import get_db
from app.core.security.rbac import require_permission
from app.domain.audit.models import ActorType, EventCategory
from app.domain.identity.models import User
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.base.contracts import Tick
from app.modules.market_data.providers.alice_blue_auth import (
    AliceBlueAuthError,
    build_authorize_url,
    exchange_for_session,
)
from app.modules.market_data.providers.alice_blue_session import set_alice_blue_session

logger = logging.getLogger("app.api.alice_blue")

router = APIRouter(prefix="/aliceblue", tags=["alice-blue"])


@router.get("/login-url")
def get_login_url(user: User = Depends(require_permission("session.start"))) -> dict:
    settings = get_settings().alice_blue
    missing = settings.missing_required_fields()
    if missing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Alice Blue credentials are not configured — missing "
            f"{', '.join(missing)} in backend/app/config/credentials/alice_blue.env",
        )
    return {"authorize_url": build_authorize_url(settings)}


@router.get("/status")
def get_status(user: User = Depends(require_permission("session.start"))) -> dict:
    from app.modules.market_data.providers.alice_blue_session import get_alice_blue_session

    return {"connected": get_alice_blue_session() is not None}


@router.get("/callback", response_class=HTMLResponse)
def oauth_callback(
    auth_code: str = Query(alias="authCode"),
    user_id: str = Query(alias="userId"),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.start")),
) -> str:
    """Not exercised end-to-end without a real Alice Blue account completing
    the browser login — see `alice_blue_auth.py`'s own docstring for exactly
    what's confirmed live vs. still a documented-but-unverified detail
    (the `Authorization` header shape for later REST calls). Query param
    names (`authCode`/`userId`) are Alice Blue's own casing, confirmed from
    their docs — the alias, not the Python parameter name, is what matters
    on the wire.
    """
    settings = get_settings().alice_blue
    try:
        session = exchange_for_session(settings, auth_code, user_id)
    except AliceBlueAuthError as exc:
        record_event(
            db,
            workspace_id=user.workspace_id,
            actor_type=ActorType.USER,
            actor_id=user.id,
            event_category=EventCategory.CREDENTIAL_CONFIG_CHANGE,
            event_type="alice_blue.oauth_login_failed",
            entity_type="broker_account",
            entity_id=None,
            payload={"error": str(exc)},
        )
        db.commit()
        return (
            "<h1>Alice Blue login failed</h1>"
            f"<p>{exc}</p><p>Close this tab and try again from the app.</p>"
        )

    set_alice_blue_session(session)

    record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        event_category=EventCategory.CREDENTIAL_CONFIG_CHANGE,
        event_type="alice_blue.oauth_login_succeeded",
        entity_type="broker_account",
        entity_id=None,
        payload={"client_id": session.client_id},
    )
    db.commit()
    return "<h1>Alice Blue connected</h1><p>You can close this tab and return to the app.</p>"


@router.get("/ws-tick-diagnostic")
def ws_tick_diagnostic(
    duration: float = 15.0,
    exchange: str = "NSE",
    token: str = "26000",
    label: str = "NIFTY 50",
    user: User = Depends(require_permission("session.start")),
) -> dict:
    """Isolated connectivity+parsing proof, deliberately independent of the
    full `AliceBlueMarketDataProvider`/scrip-master/DB pipeline (no writes
    anywhere) — mirrors `api.v1.shoonya.ws_tick_diagnostic`'s own role.
    Defaults to NIFTY 50's real, confirmed-live index token
    (`NSE|26000` — see `alice_blue_scrip_master.py`'s own docstring for how
    this was confirmed) so the common case needs no query params at all.
    `duration` clamped to 1-30s, same reasoning as Shoonya's own diagnostic:
    a typo'd query param must not tie up a worker thread indefinitely.
    """
    from app.modules.market_data.providers.alice_blue_session import get_alice_blue_session
    from app.modules.market_data.providers.alice_blue_ws_client import AliceBlueWSClient

    session = get_alice_blue_session()
    if session is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No live Alice Blue session — connect via /aliceblue/login-url first.",
        )

    settings = get_settings().alice_blue
    bounded_duration = min(max(duration, 1.0), 30.0)
    ticks: list[dict] = []

    def _on_tick(tick: Tick) -> None:
        ticks.append(
            {"contract_symbol": tick.contract_symbol, "ltp": tick.ltp, "ts": tick.ts.isoformat()}
        )

    from app.modules.market_data.providers.alice_blue_auth import create_ws_session

    client = AliceBlueWSClient(
        settings.ws_host,
        uid=f"{session.client_id}_API",
        actid=f"{session.client_id}_API",
        user_session=session.user_session,
        on_tick=_on_tick,
        ensure_ws_session=lambda: create_ws_session(settings, session),
    )
    client.start()
    connected = client._connected.is_set()  # noqa: SLF001 - deliberate diagnostic-only reach, same pattern as shoonya's export-ws-session-for-diagnostic
    client.subscribe([(label, exchange, token)])

    time.sleep(bounded_duration)
    client.stop()

    return {
        "connected": connected,
        "subscribed": f"{exchange}|{token}",
        "ticks_received": len(ticks),
        "sample_ticks": ticks[:5],
    }
