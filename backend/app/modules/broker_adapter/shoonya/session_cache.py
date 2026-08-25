"""Process-wide holder for the most recent Shoonya OAuth session — mirrors
`market_data.providers.alice_blue_session`'s own disk-cache pattern exactly
(same lock, same lazy-load-once singleton, same read/write/remove shape),
just holding a broker-agnostic `AuthResult` instead of an `AliceBlueSession`.

**Disk-cached, unlike this codebase's own Shoonya `get_broker()` singleton
used to be** — before this, that singleton stayed purely in-memory and
needed a fresh manual browser OAuth login (User ID + password + OTP/TOTP)
on every backend restart, even when the token issued that morning was still
genuinely valid for the rest of the day. Alice Blue hit and fixed this exact
gap first (see that module's own docstring for the incident that prompted
it — landing a real session, then needing two more restarts the same day
to deploy more code, each one silently discarding it).

`session_token` is a bearer token, not a password — caching it to
`config/credentials/.shoonya_session_cache.json` (`chmod 600`, gitignored
via the same `backend/app/config/credentials/*` rule as every real secret
file in that directory) is the same risk class Alice Blue's own cache
already accepted, and the same risk class this codebase's own
`export-ws-session-for-diagnostic` endpoint already accepts by writing a
live access token to a local file for a different purpose. A stale/expired
cached token is harmless on its own — `main._attempt_shoonya_reconnect_from_
cache` validates it with a real API call before trusting it, and clears the
cache immediately if that call fails, so a dead entry never lingers past the
next startup.
"""

from __future__ import annotations

import json
import logging
import os
import threading

from app.config.settings import CREDENTIALS_DIR
from app.modules.broker_adapter.base.contracts import AuthResult

logger = logging.getLogger("app.broker_adapter.shoonya.session_cache")

_CACHE_PATH = CREDENTIALS_DIR / ".shoonya_session_cache.json"

_lock = threading.Lock()
_session: AuthResult | None = None
_loaded_from_disk = False


def _load_from_disk() -> AuthResult | None:
    try:
        with open(_CACHE_PATH) as f:
            data = json.load(f)
        return AuthResult(session_token=data["session_token"], account_id=data["account_id"])
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, KeyError, OSError):
        logger.warning("Could not read cached Shoonya session at %s", _CACHE_PATH)
        return None


def _write_to_disk(session: AuthResult | None) -> None:
    if session is None:
        try:
            os.remove(_CACHE_PATH)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Could not remove cached Shoonya session at %s", _CACHE_PATH)
        return
    payload = {"session_token": session.session_token, "account_id": session.account_id}
    try:
        fd = os.open(_CACHE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
    except OSError:
        logger.warning("Could not cache Shoonya session to %s", _CACHE_PATH)


def get_cached_shoonya_session() -> AuthResult | None:
    global _session, _loaded_from_disk
    with _lock:
        if _session is None and not _loaded_from_disk:
            _loaded_from_disk = True
            _session = _load_from_disk()
        return _session


def set_cached_shoonya_session(session: AuthResult | None) -> None:
    global _session, _loaded_from_disk
    with _lock:
        _session = session
        _loaded_from_disk = True
    _write_to_disk(session)


def reset_for_tests() -> None:
    """Resets only the in-memory singleton — deliberately does **not** call
    `set_cached_shoonya_session(None)`, which would delete the real on-disk
    cache file. `_loaded_from_disk = True` prevents the next
    `get_cached_shoonya_session()` call from re-loading a real cached
    session into a test process.
    """
    global _session, _loaded_from_disk
    with _lock:
        _session = None
        _loaded_from_disk = True
