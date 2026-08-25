"""`session_cache.py` mirrors `alice_blue_session.py`'s own disk-cache
pattern — these tests cover the same shape: write/read/remove roundtrip,
`reset_for_tests()` not touching a real on-disk file, and tolerance of a
corrupt cache file.
"""

from __future__ import annotations

import json

import pytest

from app.modules.broker_adapter.base.contracts import AuthResult
from app.modules.broker_adapter.shoonya import session_cache


@pytest.fixture(autouse=True)
def _isolated_cache_path(tmp_path, monkeypatch):
    """Redirects the module's on-disk cache to a throwaway path and resets
    its in-memory singleton before and after every test, so tests never
    read or clobber a real cached session on the machine running them.
    """
    cache_path = tmp_path / ".shoonya_session_cache.json"
    monkeypatch.setattr(session_cache, "_CACHE_PATH", cache_path)
    session_cache._session = None
    session_cache._loaded_from_disk = True
    yield cache_path
    session_cache._session = None
    session_cache._loaded_from_disk = True


def test_get_returns_none_when_nothing_cached():
    assert session_cache.get_cached_shoonya_session() is None


def test_set_then_get_roundtrips_in_memory():
    auth = AuthResult(session_token="tok", account_id="FA1")
    session_cache.set_cached_shoonya_session(auth)
    assert session_cache.get_cached_shoonya_session() == auth


def test_set_writes_to_disk_with_restricted_permissions(_isolated_cache_path):
    auth = AuthResult(session_token="tok", account_id="FA1")
    session_cache.set_cached_shoonya_session(auth)

    assert _isolated_cache_path.exists()
    payload = json.loads(_isolated_cache_path.read_text())
    assert payload == {"session_token": "tok", "account_id": "FA1"}


def test_a_fresh_process_reloads_the_cached_session_from_disk(_isolated_cache_path):
    auth = AuthResult(session_token="tok", account_id="FA1")
    session_cache.set_cached_shoonya_session(auth)

    # Simulate a fresh process: in-memory slot forgotten, disk untouched.
    session_cache._session = None
    session_cache._loaded_from_disk = False

    assert session_cache.get_cached_shoonya_session() == auth


def test_set_none_removes_the_cache_file(_isolated_cache_path):
    session_cache.set_cached_shoonya_session(AuthResult(session_token="tok", account_id="FA1"))
    assert _isolated_cache_path.exists()

    session_cache.set_cached_shoonya_session(None)

    assert not _isolated_cache_path.exists()
    assert session_cache.get_cached_shoonya_session() is None


def test_corrupt_cache_file_is_tolerated_not_raised(_isolated_cache_path):
    _isolated_cache_path.write_text("not valid json")
    session_cache._loaded_from_disk = False

    assert session_cache.get_cached_shoonya_session() is None


def test_reset_for_tests_clears_memory_but_not_a_real_disk_file(_isolated_cache_path):
    session_cache.set_cached_shoonya_session(AuthResult(session_token="tok", account_id="FA1"))

    session_cache.reset_for_tests()

    assert session_cache.get_cached_shoonya_session() is None
    # The disk file itself survives reset_for_tests() -- only a later
    # set_cached_shoonya_session(None) or set_cached_shoonya_session(other)
    # touches disk again.
    assert _isolated_cache_path.exists()
