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

import logging
import threading
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.db.session import SessionFactory, get_db, session_scope
from app.core.security.rbac import require_permission
from app.domain.audit.models import ActorType, EventCategory
from app.domain.identity.models import User
from app.domain.market.models import Instrument, OptionContract
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.composition import (
    get_broker,
    is_shoonya_configured,
    set_broker,
    unwrap_broker,
)
from app.modules.broker_adapter.shoonya.auth import build_authorize_url, exchange_code_for_token
from app.modules.broker_adapter.shoonya.session_cache import set_cached_shoonya_session
from app.modules.scheduler.instrument_sync import sync_instrument_master

logger = logging.getLogger("app.api.shoonya")

router = APIRouter(prefix="/shoonya", tags=["shoonya"])

# This system only ever trades these two underlyings — see
# `broker_adapter/shoonya/adapter.py`'s own `KNOWN_UNDERLYINGS` for the
# identical scoping decision on the sync side.
_KNOWN_UNDERLYINGS = ("NIFTY", "BANKNIFTY")


def _seed_option_anchors(db: Session, adapter: object) -> None:
    """Pre-warms `ShoonyaBrokerAdapter._resolve_option_anchor_tsym`'s cache
    from this system's own already-synced `option_contracts`, for every
    expiry `sync_instrument_master` (just called before this) confirmed
    active — see `ShoonyaBrokerAdapter.seed_option_anchor`'s own docstring
    for why: a live `SearchScrip` call for something already known correct
    is pure unreliability with no correctness upside. `adapter` is typed
    `object` (not `ShoonyaBrokerAdapter`) so this function's signature
    doesn't force an eager import of `broker_adapter.shoonya` at module
    scope — see this module's own top-level docstring for why that's
    deliberately avoided; `seed_option_anchor` is called via `getattr` so
    a non-Shoonya broker (never expected here, but safe regardless) is
    simply skipped rather than crashing OAuth login over a warm-up step.
    """
    seed = getattr(adapter, "seed_option_anchor", None)
    if seed is None:
        return
    for symbol in _KNOWN_UNDERLYINGS:
        instrument = db.query(Instrument).filter(Instrument.symbol == symbol).one_or_none()
        if instrument is None:
            continue
        rows = (
            db.query(OptionContract.expiry_date, OptionContract.symbol)
            .filter(
                OptionContract.instrument_id == instrument.id,
                OptionContract.is_active.is_(True),
            )
            .order_by(OptionContract.expiry_date)
            .distinct(OptionContract.expiry_date)
            .all()
        )
        for expiry_date, tsym in rows:
            seed(symbol, expiry_date, tsym)


_post_login_background_lock = threading.Lock()


def _run_post_login_background_work(
    adapter: BrokerPort,
    *,
    market_data_provider: str,
    session_factory: SessionFactory = session_scope,
) -> None:
    """The slow, non-auth-critical half of a successful Shoonya login —
    instrument-master sync, option-anchor seeding, market-data registry
    reset, daily-bootstrap retry — split out of `oauth_callback` and run in
    a background thread (see `_spawn_post_login_background_work` below).

    2026-08-26: this used to run inline in the request. With 10 strategy
    configs now enabled (up from 5 when `run_daily_bootstrap` was added
    here), the daily-bootstrap step alone can trigger hundreds of
    serialized `GetQuotes` calls (see `core/rate_limiter.py`'s own
    docstring) — confirmed live: nginx's `proxy_read_timeout` (already
    raised once, to 90s) still wasn't enough, producing a real 504 to the
    browser twice in one day even though the backend itself completed
    successfully every time. Backgrounding this means the browser gets a
    response in well under a second regardless of how long this takes.

    Uses its own DB session (`session_factory`, real production
    `session_scope` by default) — never the request-scoped session
    `oauth_callback` was given, which FastAPI tears down the moment the
    HTTP response returns, before this thread's work even starts.

    Each step keeps its own try/except, same per-step granularity
    `oauth_callback` already had for these calls when they were inline —
    but now load-bearing in a new way: an uncaught exception in a
    background thread has no request left to fail loudly, so every step
    must guard itself explicitly rather than relying on "the request will
    500 if this fails" the way the old synchronous code implicitly could
    for `sync_instrument_master`/`_seed_option_anchors`.
    """
    try:
        with session_factory() as db:
            sync_instrument_master(db, adapter, ["NFO"])
            _seed_option_anchors(db, adapter)
            db.commit()
    except Exception:
        logger.exception(
            "post-login background instrument-master sync / option-anchor seed failed "
            "-- continuing with market-data reset and daily bootstrap regardless"
        )

    if market_data_provider == "shoonya":
        from app.modules.market_data.registry import reset_for_reconnect

        try:
            reset_for_reconnect()
        except Exception:
            logger.exception("post-login background reset_for_reconnect failed")
    else:
        from app.modules.market_data.provider_composition import reset_shoonya_backup_leg

        try:
            reset_shoonya_backup_leg()
        except Exception:
            logger.exception("post-login background reset_shoonya_backup_leg failed")

    from app.modules.session.bootstrapper import run_daily_bootstrap

    try:
        run_daily_bootstrap()
    except Exception:
        logger.exception(
            "post-login background run_daily_bootstrap failed -- any strategy that "
            "previously failed to auto-spawn will retry at the next 09:00 IST cycle "
            "or app login instead"
        )


def _spawn_post_login_background_work(adapter: BrokerPort, *, market_data_provider: str) -> None:
    """Non-blocking: if a previous reconnect's background work is still
    running, logs and skips spawning a duplicate rather than racing two
    concurrent `sync_instrument_master`/`run_daily_bootstrap` passes against
    each other (the in-flight pass already covers essentially the same
    work — the next login, or tomorrow's 09:00 IST scheduler tick,
    reconciles anything this one would have refreshed). Split out from
    `_run_post_login_background_work` so tests can monkeypatch just this
    one call to run synchronously instead of a real thread firing under
    pytest — same precedent `system_settings._schedule_restart` already
    established for this exact "kick off background work from a request
    handler" shape in this codebase.
    """
    if not _post_login_background_lock.acquire(blocking=False):
        logger.warning(
            "shoonya.oauth_callback: post-login background work already in progress "
            "from a previous reconnect -- skipping a duplicate run"
        )
        return

    def _run() -> None:
        try:
            _run_post_login_background_work(adapter, market_data_provider=market_data_provider)
        finally:
            _post_login_background_lock.release()

    threading.Thread(target=_run, daemon=True).start()


@router.get("/status")
def get_status(user: User = Depends(require_permission("session.start"))) -> dict:
    return {"connected": is_shoonya_configured()}


@router.get("/ws-diagnostic")
def ws_diagnostic(user: User = Depends(require_permission("session.start"))) -> dict:
    """2026-08-11 diagnostic — verifies the new WS auth payload Shoonya
    support specified (see `ws_client.py`'s own docstring). Requires an
    already-live Shoonya session (reuses whatever `get_broker()` currently
    holds; needs no separate credentials of its own) — hit
    `/shoonya/login-url` first if this 409s.
    """
    from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter

    broker = get_broker()
    # composition.py wraps every real adapter in `_AuthAwareBroker` — unwrap
    # it, since `isinstance(broker, ShoonyaBrokerAdapter)` alone is always
    # False against the wrapper.
    inner = unwrap_broker(broker)
    if not isinstance(inner, ShoonyaBrokerAdapter):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No live Shoonya session — connect via /shoonya/login-url first.",
        )
    return inner.diagnose_ws_auth()


@router.get("/export-ws-session-for-diagnostic")
def export_ws_session_for_diagnostic(
    user: User = Depends(require_permission("session.start")),
) -> dict:
    """2026-08-19 TEMP diagnostic — lets a standalone, out-of-process WS
    quality-monitoring script (run in isolation, deliberately outside this
    app's own request/response cycle so it can watch for hours without
    tying up an HTTP connection) reuse this app's already-live Shoonya
    session instead of logging in separately. A second, independent login
    risked silently invalidating this app's own live session mid-paper-
    trading if Shoonya's session model only permits one active token per
    account (never confirmed either way, not worth risking to find out).

    Writes `{uid, actid, access_token, ws_host, api_host}` to a local file
    only this box's own user can read (`chmod 600`) -- deliberately never
    returned in the HTTP response body, so the access token never lands in
    a browser network tab, an nginx access log, or anywhere else an HTTP
    response might be captured. Strip this endpoint once the diagnostic run
    is done.
    """
    import json
    import os

    from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter

    broker = get_broker()
    inner = unwrap_broker(broker)
    if not isinstance(inner, ShoonyaBrokerAdapter):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No live Shoonya session — connect via /shoonya/login-url first.",
        )
    payload = {
        "uid": inner._uid,  # noqa: SLF001 - deliberate diagnostic-only reach into adapter internals
        "actid": inner._actid,  # noqa: SLF001
        "access_token": inner._auth_result.session_token,  # noqa: SLF001
        "ws_host": inner._settings.ws_host,  # noqa: SLF001
        "api_host": inner._settings.api_host,  # noqa: SLF001
    }
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".ws_diagnostic_session.json")
    path = os.path.abspath(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    return {"exported": True}


@router.get("/ws-tick-diagnostic")
def ws_tick_diagnostic(
    symbols: str,
    warm_underlying: str = "",
    warm_expiry: str = "",
    duration: float = 15.0,
    user: User = Depends(require_permission("session.start")),
) -> dict:
    """2026-08-12, Phase 0 of verifying Shoonya WS end-to-end (see
    `ShoonyaBrokerAdapter.diagnose_ws_ticks`'s own docstring) — unlike
    `/ws-diagnostic` (auth handshake only), this exercises the real
    `subscribe_quotes` path and reports whatever ticks actually arrive.
    `symbols` is a comma-separated list of real contract symbols — Shoonya's
    real trading symbol format is `DDMMMYY` + `C`/`P` + strike, e.g.
    `NIFTY18AUG26C24400` (**not** a `CE`/`PE` suffix — see `normalizer.py`'s
    own module docstring for why that distinction matters and how it was
    confirmed). Check `option_contracts` for a currently-active one. Pass
    `warm_underlying`/`warm_expiry` (`YYYY-MM-DD`) — e.g. `NIFTY`/
    `2026-08-18` — to have this call
    `get_option_chain` first and populate this adapter instance's token
    cache, same as real strategies always do before subscribing; omit
    them and a symbol whose token isn't already cached in this process
    will fail with a clear `resolve_token` error instead. `duration` is
    clamped to 1-30s so a typo'd query param can't tie up a worker thread
    indefinitely.
    """
    from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter

    broker = get_broker()
    inner = unwrap_broker(broker)
    if not isinstance(inner, ShoonyaBrokerAdapter):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No live Shoonya session — connect via /shoonya/login-url first.",
        )
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not symbol_list:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No symbols given")
    bounded_duration = min(max(duration, 1.0), 30.0)
    try:
        parsed_expiry = date.fromisoformat(warm_expiry) if warm_expiry else None
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"warm_expiry must be YYYY-MM-DD, got {warm_expiry!r}",
        ) from exc
    return inner.diagnose_ws_ticks(
        symbol_list,
        duration_seconds=bounded_duration,
        warm_underlying=warm_underlying or None,
        warm_expiry=parsed_expiry,
    )


@router.get("/search-scrip")
def search_scrip_diagnostic(
    exchange: str,
    text: str,
    user: User = Depends(require_permission("session.start")),
) -> dict:
    """2026-08-14: read-only lookup for discovering a symbol's real Shoonya
    `tsym`/token before subscribing to it — e.g. India VIX, which (like
    NIFTY/BANKNIFTY before it — see `_UNDERLYING_INDEX_TSYM`'s own
    docstring) isn't a bare recognizable name on Shoonya's own search; a
    naive text search can return several unrelated decoys, so this
    deliberately returns every raw candidate for a human to pick from,
    rather than auto-selecting the first match. No side effects — doesn't
    subscribe, doesn't cache anything.
    """
    from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter

    broker = get_broker()
    inner = unwrap_broker(broker)
    if not isinstance(inner, ShoonyaBrokerAdapter):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No live Shoonya session — connect via /shoonya/login-url first.",
        )
    rows = inner._rest.search_scrip(inner._uid, exchange, text)  # noqa: SLF001
    return {"count": len(rows), "results": rows}


@router.get("/subscribe-diagnostic")
def subscribe_diagnostic(
    symbols: str,
    manual_symbol: str = "",
    manual_exchange: str = "",
    manual_token: str = "",
    duration: float = 20.0,
    user: User = Depends(require_permission("session.start")),
) -> dict:
    """2026-08-14: unlike `/ws-tick-diagnostic` (throwaway callback, never
    actually invoked once real ingestion already owns the shared
    connection's fixed construction-time callback -- a known, accepted
    limitation, not fixed here), this subscribes through the *real*
    production path -- `market_data.provider_composition
    .get_market_data_provider()`, the same cached provider `market_data
    .registry.ensure_ingestion_running` uses -- whose per-symbol callback
    dict is updatable live with no reconstruction needed, so this doesn't
    disturb any other symbol's existing routing (verified against
    `FailoverMarketDataProvider`/`MarketHoursGatedProvider`'s own
    subscribe_ticks/unsubscribe_ticks, both per-symbol/additive).

    `manual_symbol`/`manual_exchange`/`manual_token` (all three or none)
    seed the broker's token cache before subscribing -- for a symbol with
    no `Instrument`/`OptionContract` row and no `KNOWN_UNDERLYINGS` entry
    (e.g. a VIX row discovered via `/search-scrip`), which has no other way
    to resolve a token. Already-cached option-contract symbols (anything a
    real strategy has already ranked via get_option_chain) need no manual
    token -- `_resolve_token` already has it.

    **Caller must independently confirm no symbol passed here has a
    currently-OPEN Position** -- subscribe_ticks/unsubscribe_ticks are
    per-symbol but not per-caller, so sharing a symbol with
    PositionManager's own live subscription would silently steal that
    position's real pricing callback for this call's duration and not
    restore it afterward (unsubscribe_ticks just pops the entry). Not
    checked here since this endpoint has no session/position context of
    its own -- it's the caller's responsibility, same as
    `/ws-tick-diagnostic` already leaves `warm_underlying`/`warm_expiry`
    correctness to the caller.
    """
    import threading
    import time as time_module

    from app.modules.broker_adapter.base.contracts import Tick
    from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter
    from app.modules.market_data.provider_composition import get_market_data_provider

    broker = get_broker()
    inner = unwrap_broker(broker)
    if not isinstance(inner, ShoonyaBrokerAdapter):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No live Shoonya session — connect via /shoonya/login-url first.",
        )
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not symbol_list:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No symbols given")
    bounded_duration = min(max(duration, 1.0), 45.0)

    if manual_symbol and manual_exchange and manual_token:
        inner._remember_token(manual_symbol, manual_exchange, manual_token)  # noqa: SLF001

    received: list[dict] = []
    lock = threading.Lock()

    def _collect(tick: Tick) -> None:
        with lock:
            received.append(
                {
                    "contract_symbol": tick.contract_symbol,
                    "ltp": tick.ltp,
                    "volume": tick.volume,
                    "oi": tick.oi,
                    "ts": tick.ts.isoformat(),
                }
            )

    provider = get_market_data_provider()
    try:
        provider.subscribe_ticks(symbol_list, on_tick=_collect)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "ticks_received": 0, "sample": []}

    try:
        time_module.sleep(bounded_duration)
    finally:
        try:
            provider.unsubscribe_ticks(symbol_list)
        except Exception:
            logger.exception("Failed to unsubscribe after subscribe-diagnostic")

    with lock:
        return {"ticks_received": len(received), "sample": received[:20]}


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
    # 2026-08-25: disk-cache this session so a later backend restart can
    # reconnect automatically without a fresh browser login — see
    # `session_cache.py`'s own docstring and `main._attempt_shoonya_reconnect
    # _from_cache`, which validates it with a real API call before trusting
    # it on the next startup.
    set_cached_shoonya_session(session.auth_result)

    # 2026-08-21: bracket-order research Phase A — read-only, no order
    # placed/modified/cancelled anywhere in this block. Logs this account's
    # actual enabled exchange/product list (exarr/prarr) from both possible
    # sources (the GenAcsTok login response itself, and a separate
    # UserDetails probe) so a human can decide whether NFO bracket/cover
    # orders (prd='B'/'H') are even reachable on this account before any
    # further BO work — see docs' bracket-order research memo. Exception-
    # safe, same pattern as the reconnect calls below: a diagnostic failing
    # must never fail a login that otherwise succeeded.
    if session.raw_login_capabilities is not None:
        logger.info(
            "shoonya.product_capabilities (from GenAcsTok login response): exarr=%r prarr=%r",
            session.raw_login_capabilities.get("exarr"),
            session.raw_login_capabilities.get("prarr"),
        )
    else:
        logger.info(
            "shoonya.product_capabilities: GenAcsTok login response had no exarr/prarr fields"
        )
    try:
        capabilities = adapter.get_product_capabilities()
        logger.info("shoonya.product_capabilities (from UserDetails): %r", capabilities)
    except Exception:
        logger.exception("shoonya.product_capabilities: UserDetails diagnostic failed")

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

    # 2026-08-26: the rest of this function -- instrument-master sync,
    # option-anchor seeding, market-data registry reset, and the
    # auto-spawn-retry daily bootstrap (see `_run_post_login_background_
    # work`'s own docstring for the full history and why this moved off
    # the request path) -- now runs in a background thread instead of
    # inline, so the browser gets this response back immediately instead
    # of risking an nginx 504 on a slow reconnect.
    _spawn_post_login_background_work(
        adapter, market_data_provider=get_settings().market_data.provider
    )

    return (
        "<h1>Shoonya connected</h1>"
        "<p>You can close this tab and return to the app. Finishing option-data sync "
        "and today's strategy setup in the background — this can take a minute.</p>"
    )
