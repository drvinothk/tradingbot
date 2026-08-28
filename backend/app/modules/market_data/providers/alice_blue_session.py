"""Process-wide holder for the most recent Alice Blue OAuth session —
market-data's own mirror of `broker_adapter/composition.py`'s
`get_broker()`/`set_broker()` pattern, deliberately a *separate* singleton
(see `provider_composition.py`'s own docstring for why market data and
execution never share one broker-resolution slot).

Alice Blue's login can't be triggered from inside `AliceBlueMarketDataProvider
.connect()` the way Angel One's can (direct password+TOTP) — it's a
browser-redirect flow that only ever completes via `api.v1.alice_blue
.oauth_callback`. So `connect()` just reads whatever `get_alice_blue_session`
currently holds; `None` until a human completes the browser login at least
once.

**Disk-cached, unlike every other in-memory broker session in this
codebase** (Shoonya's `get_broker()` singleton included, which stays purely
in-memory and needs a fresh reconnect on every restart) — added 2026-08-21
after landing a genuine session, then immediately needing two more backend
restarts (each to deploy more Alice Blue code that didn't exist yet) each
silently discarding it and forcing another human browser-login click.
`userSession` is a bearer token, not a password — caching it to
`config/credentials/.alice_blue_session_cache.json` (gitignored via the
same `backend/app/config/credentials/*` rule as every real secret file in
that directory, `chmod 600`) is the same risk class as Shoonya's own
`export-ws-session-for-diagnostic` writing a live access token to a local
file, just automatic rather than a manual diagnostic step. A stale/expired
cached token is harmless — the next real API call against it simply fails
and a fresh login is needed exactly as it would be with no cache at all;
this only ever saves a re-login when the token is still genuinely live.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from app.config.settings import CREDENTIALS_DIR
from app.modules.market_data.providers.alice_blue_auth import AliceBlueSession

logger = logging.getLogger("app.market_data.alice_blue_session")

_CACHE_PATH = CREDENTIALS_DIR / ".alice_blue_session_cache.json"

_lock = threading.Lock()
_session: AliceBlueSession | None = None
_loaded_from_disk = False

# Short-lived cache for `alice_blue_connection_live()` so the Market Terminal's
# on-mount + on-focus polling of `/aliceblue/status` doesn't fire a
# `createWsSess` probe every time. Holds the last *definitive* result only.
_PROBE_TTL_SECONDS = 30.0
_probe_lock = threading.Lock()
_probe_cache: tuple[float, bool] | None = None  # (monotonic_deadline, connected)


def _load_from_disk() -> AliceBlueSession | None:
    try:
        with open(_CACHE_PATH) as f:
            data = json.load(f)
        return AliceBlueSession(client_id=data["client_id"], user_session=data["user_session"])
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, KeyError, OSError):
        logger.warning("Could not read cached Alice Blue session at %s", _CACHE_PATH)
        return None


def _write_to_disk(session: AliceBlueSession | None) -> None:
    if session is None:
        try:
            os.remove(_CACHE_PATH)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Could not remove cached Alice Blue session at %s", _CACHE_PATH)
        return
    payload = {"client_id": session.client_id, "user_session": session.user_session}
    try:
        fd = os.open(_CACHE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
    except OSError:
        logger.warning("Could not cache Alice Blue session to %s", _CACHE_PATH)


def get_alice_blue_session() -> AliceBlueSession | None:
    global _session, _loaded_from_disk
    with _lock:
        if _session is None and not _loaded_from_disk:
            _loaded_from_disk = True
            _session = _load_from_disk()
        return _session


def set_alice_blue_session(session: AliceBlueSession | None) -> None:
    global _session, _loaded_from_disk
    with _lock:
        _session = session
        _loaded_from_disk = True
    _write_to_disk(session)


def alice_blue_connection_live() -> bool:
    """What `GET /aliceblue/status` reports: is the cached Alice Blue token
    actually usable *right now*, not just "a cache file exists".

    `None` session → `False` (no probe). Otherwise a `createWsSess` liveness
    probe (`alice_blue_auth.probe_ws_session`), behind a 30s TTL:
    `"alive"` → `True`, `"dead"` → `False`, `"unknown"` (transient) → the last
    definitive result if we have one, else `True` (optimistic — a network
    blip must not read as "not connected"). Read-only: never clears the
    session, never touches the WS reconnect loop.
    """
    global _probe_cache

    from app.config.settings import get_settings
    from app.modules.market_data.providers.alice_blue_auth import probe_ws_session

    session = get_alice_blue_session()
    if session is None:
        return False

    now = time.monotonic()
    with _probe_lock:
        if _probe_cache is not None and now < _probe_cache[0]:
            return _probe_cache[1]
        last_known = _probe_cache[1] if _probe_cache is not None else None

    result = probe_ws_session(get_settings().alice_blue, session)
    if result == "unknown":
        return last_known if last_known is not None else True

    connected = result == "alive"
    with _probe_lock:
        _probe_cache = (time.monotonic() + _PROBE_TTL_SECONDS, connected)
    return connected


def reset_for_tests() -> None:
    """Resets only the in-memory singleton — deliberately does **not** call
    `set_alice_blue_session(None)`, which would delete the real on-disk
    cache file. `_loaded_from_disk = True` prevents the next
    `get_alice_blue_session()` call from re-loading a real cached session
    into a test process.
    """
    global _session, _loaded_from_disk, _probe_cache
    with _lock:
        _session = None
        _loaded_from_disk = True
    with _probe_lock:
        _probe_cache = None
