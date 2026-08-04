"""Shoonya OAuth login: `GET /login-url` gives the frontend the URL to send
the user's browser to; `GET /callback` is what Shoonya's own site redirects
that browser back to afterward, per `SHOONYA_REDIRECT_URL`. Both require an
already-logged-in-to-this-app session (the callback is a plain browser
navigation, not a `fetch`, but the session cookie still travels — GET
navigations aren't blocked by `SameSite=Lax`), so an event can be recorded
against a real `user`/`workspace_id`.

Deliberately does not import anything from `broker_adapter.shoonya` at
module scope — see `composition.py`'s own docstring for why: a process that
never actually connects to Shoonya (every test, and local dev before real
credentials exist) shouldn't pay for importing `httpx`/`websockets`-touching
code it never uses.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.db.session import get_db
from app.core.security.rbac import require_permission
from app.domain.audit.models import ActorType, EventCategory
from app.domain.identity.models import User
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.composition import is_shoonya_configured, set_broker
from app.modules.broker_adapter.shoonya.auth import build_authorize_url, exchange_code_for_token
from app.modules.scheduler.instrument_sync import sync_instrument_master

router = APIRouter(prefix="/shoonya", tags=["shoonya"])


@router.get("/status")
def get_status(user: User = Depends(require_permission("session.start"))) -> dict:
    return {"connected": is_shoonya_configured()}


@router.get("/login-url")
def get_login_url(user: User = Depends(require_permission("session.start"))) -> dict:
    settings = get_settings().shoonya
    missing = settings.missing_required_fields()
    if missing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Shoonya credentials are not configured — missing "
            f"{', '.join(missing)} in backend/app/config/credentials/shoonya.env",
        )
    return {"authorize_url": build_authorize_url(settings)}


@router.get("/callback", response_class=HTMLResponse)
def oauth_callback(
    code: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.start")),
) -> str:
    """Not exercised end-to-end without a real Shoonya account completing
    the browser login — see `broker_adapter/shoonya/auth.py`'s own
    docstring for the "researched, not live-verified" caveat on the
    `GenAcsTok` response shape this depends on.
    """
    from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter
    from app.modules.broker_adapter.shoonya.auth import ShoonyaAuthError

    settings = get_settings().shoonya
    try:
        session = exchange_code_for_token(settings, code)
    except ShoonyaAuthError as exc:
        record_event(
            db,
            workspace_id=user.workspace_id,
            actor_type=ActorType.USER,
            actor_id=user.id,
            event_category=EventCategory.CREDENTIAL_CONFIG_CHANGE,
            event_type="shoonya.oauth_login_failed",
            entity_type="broker_account",
            entity_id=None,
            payload={"error": str(exc)},
        )
        db.commit()
        return (
            "<h1>Shoonya login failed</h1>"
            f"<p>{exc}</p><p>Close this tab and try again from the app.</p>"
        )

    adapter = ShoonyaBrokerAdapter(settings, session.auth_result)
    set_broker(adapter)

    # Live-found bug: `composition.py`'s own docstring claims "once Phase 5's
    # real Shoonya adapter is configured, that adapter syncs its own
    # instrument master from the exchange" — it never actually did.
    # `_sync_mock_instrument_universe` (app.main's startup hook) only runs
    # once, at process startup, and only against the mock adapter (Shoonya
    # isn't connected yet at that point — login is a live, later, in-browser
    # action). With nothing re-syncing after a real login, `instruments`/
    # `option_contracts` stayed permanently stuck on the mock adapter's
    # synthetic seed data (a fixed "nearest Thursday" expiry, identical for
    # every underlying) — which is exactly what the frontend's expiry picker
    # was showing, and exactly why every real start_strategy call was
    # requesting an expiry that doesn't actually exist. Syncing here, right
    # after a real login, is what actually makes real expiries reach the
    # picker. `sync_instrument_master` never raises (failures are recorded
    # in its own log row, not thrown) — safe to call unconditionally.
    sync_instrument_master(db, adapter, ["NFO"])

    record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        event_category=EventCategory.CREDENTIAL_CONFIG_CHANGE,
        event_type="shoonya.oauth_login_succeeded",
        entity_type="broker_account",
        entity_id=None,
        payload={"account_id": session.auth_result.account_id},
    )
    db.commit()
    return "<h1>Shoonya connected</h1><p>You can close this tab and return to the app.</p>"
