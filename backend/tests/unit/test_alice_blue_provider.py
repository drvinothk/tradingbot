"""`AliceBlueMarketDataProvider.is_ready` -- the one real override of
`BaseMarketDataProvider.is_ready`'s `True` default (see that method's own
docstring), added 2026-08-25 so `FailoverMarketDataProvider` can check
before attempting to subscribe Alice Blue as a backup leg.

2026-09-01: upgraded from a bare "is a session cached" check to the real
`alice_blue_connection_live()` liveness probe -- a cached-but-stale session
(the normal state every morning until a human logs in) used to read as
"ready", letting the failover wrapper construct a real `AliceBlueWSClient`
against a dead token every day; see that function's own docstring and
`alice_blue_ws_client.py`'s `_SESSION_NOT_READY_BACKOFF_SECONDS` for the
full incident. `probe_ws_session` (the real `createWsSess` REST call
`alice_blue_connection_live` makes) is monkeypatched throughout this file so
these tests stay deterministic and offline.
"""

from __future__ import annotations

import pytest

from app.config.settings import AliceBlueSettings
from app.modules.market_data.providers import alice_blue_auth, alice_blue_session
from app.modules.market_data.providers.alice_blue import AliceBlueMarketDataProvider
from app.modules.market_data.providers.alice_blue_auth import AliceBlueSession
from app.modules.market_data.providers.alice_blue_scrip_master import AliceBlueScripMasterService


@pytest.fixture(autouse=True)
def _reset_alice_blue_session(tmp_path, monkeypatch):
    """Same reasoning as test_api_market_data.py's own fixture -- without
    this, a real session cached on disk from this machine's own past login
    would leak into these tests, which must be deterministic regardless of
    local dev state. `_CACHE_PATH` is also redirected to a throwaway path
    (same pattern as test_shoonya_session_cache.py's own
    `_isolated_cache_path`) -- `set_alice_blue_session` below writes through
    to disk for real, and this suite must never overwrite whatever real
    session this machine has cached.
    """
    monkeypatch.setattr(
        alice_blue_session, "_CACHE_PATH", tmp_path / ".alice_blue_session_cache.json"
    )
    alice_blue_session.reset_for_tests()
    yield
    alice_blue_session.reset_for_tests()


def _provider() -> AliceBlueMarketDataProvider:
    return AliceBlueMarketDataProvider(AliceBlueSettings(), AliceBlueScripMasterService())


def test_is_ready_is_false_with_no_session():
    assert _provider().is_ready() is False


def test_is_ready_is_true_once_a_session_exists_and_probe_says_alive(monkeypatch):
    monkeypatch.setattr(alice_blue_auth, "probe_ws_session", lambda *a, **k: "alive")
    alice_blue_session.set_alice_blue_session(
        AliceBlueSession(client_id="AB1234", user_session="fake-session-token")
    )

    assert _provider().is_ready() is True


def test_is_ready_is_false_when_session_exists_but_probe_says_dead(monkeypatch):
    """The actual bug this upgrade closes: a *cached* session (yesterday's,
    still on disk) is not the same as a *live* one — a dead probe result
    must not let `FailoverMarketDataProvider` treat this leg as ready.
    """
    monkeypatch.setattr(alice_blue_auth, "probe_ws_session", lambda *a, **k: "dead")
    alice_blue_session.set_alice_blue_session(
        AliceBlueSession(client_id="AB1234", user_session="fake-session-token")
    )

    assert _provider().is_ready() is False
