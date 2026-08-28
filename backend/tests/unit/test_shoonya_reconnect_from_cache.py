"""`app.main._attempt_shoonya_reconnect_from_cache` — the startup step that
restores a disk-cached Shoonya session (`shoonya/session_cache.py`) instead
of forcing a fresh manual browser OAuth login on every backend restart.

Every external dependency (`get_settings`, the session cache, the real
`ShoonyaBrokerAdapter`, `composition.set_broker`) is faked/monkeypatched —
this test never touches a real file, network call, or the live broker
singleton.
"""

from __future__ import annotations

from pydantic import SecretStr

import app.main as main_module
from app.config.settings import ShoonyaSettings
from app.modules.broker_adapter.base.contracts import AuthResult
from app.modules.broker_adapter.shoonya import adapter as shoonya_adapter_module
from app.modules.broker_adapter.shoonya import session_cache as session_cache_module

CONFIGURED_SETTINGS = ShoonyaSettings(
    client_id="X",
    secret_code=SecretStr("Y"),
    user_id="FA1",
    api_host="https://api.shoonya.test/NorenWClientAPI",
    ws_host="wss://api.shoonya.test/NorenWSAPI/",
)
# Explicit client_id="" (not a bare `ShoonyaSettings()`) so this stays
# "unconfigured" even on a machine with a real `shoonya.env` on disk —
# `ShoonyaSettings` loads from that file by default, same reasoning
# `test_api_shoonya.py`'s own `test_login_url_returns_409_...` test already
# applies.
UNCONFIGURED_SETTINGS = ShoonyaSettings(client_id="")
AUTH_RESULT = AuthResult(session_token="tok", account_id="FA1")


class _FakeSettingsBundle:
    def __init__(self, shoonya: ShoonyaSettings) -> None:
        self.shoonya = shoonya


class _FakeAdapter:
    def __init__(self, settings, auth_result, *, margin_error: Exception | None = None) -> None:
        self.settings = settings
        self.auth_result = auth_result
        self._margin_error = margin_error
        self.closed = False

    def get_margin(self):
        if self._margin_error is not None:
            raise self._margin_error
        return object()

    def close(self) -> None:
        self.closed = True


def _patch_common(monkeypatch, *, settings: ShoonyaSettings):
    monkeypatch.setattr(
        "app.config.settings.get_settings", lambda: _FakeSettingsBundle(settings)
    )


def test_noop_when_shoonya_credentials_are_not_configured(monkeypatch):
    _patch_common(monkeypatch, settings=UNCONFIGURED_SETTINGS)
    get_cached_calls = []
    monkeypatch.setattr(
        session_cache_module,
        "get_cached_shoonya_session",
        lambda: get_cached_calls.append(1) or None,
    )
    set_broker_calls = []
    monkeypatch.setattr(
        "app.modules.broker_adapter.composition.set_broker",
        lambda broker: set_broker_calls.append(broker),
    )

    main_module._attempt_shoonya_reconnect_from_cache()

    assert get_cached_calls == []
    assert set_broker_calls == []


def test_noop_when_nothing_is_cached(monkeypatch):
    _patch_common(monkeypatch, settings=CONFIGURED_SETTINGS)
    monkeypatch.setattr(session_cache_module, "get_cached_shoonya_session", lambda: None)
    set_broker_calls = []
    monkeypatch.setattr(
        "app.modules.broker_adapter.composition.set_broker",
        lambda broker: set_broker_calls.append(broker),
    )

    main_module._attempt_shoonya_reconnect_from_cache()

    assert set_broker_calls == []


def test_valid_cached_session_reconnects(monkeypatch):
    _patch_common(monkeypatch, settings=CONFIGURED_SETTINGS)
    monkeypatch.setattr(session_cache_module, "get_cached_shoonya_session", lambda: AUTH_RESULT)
    set_cached_calls = []
    monkeypatch.setattr(
        session_cache_module,
        "set_cached_shoonya_session",
        lambda session: set_cached_calls.append(session),
    )

    fake_adapters: list[_FakeAdapter] = []

    def _fake_adapter_ctor(settings, auth_result):
        fake = _FakeAdapter(settings, auth_result)
        fake_adapters.append(fake)
        return fake

    monkeypatch.setattr(shoonya_adapter_module, "ShoonyaBrokerAdapter", _fake_adapter_ctor)

    set_broker_calls = []
    monkeypatch.setattr(
        "app.modules.broker_adapter.composition.set_broker",
        lambda broker: set_broker_calls.append(broker),
    )

    main_module._attempt_shoonya_reconnect_from_cache()

    assert len(fake_adapters) == 1
    assert set_broker_calls == [fake_adapters[0]]
    assert not fake_adapters[0].closed
    # A valid cache must not be rewritten/cleared on a successful reconnect.
    assert set_cached_calls == []


def test_stale_cached_session_is_discarded_not_installed(monkeypatch):
    from app.modules.broker_adapter.shoonya.rest_client import ShoonyaSessionExpiredError

    _patch_common(monkeypatch, settings=CONFIGURED_SETTINGS)
    monkeypatch.setattr(session_cache_module, "get_cached_shoonya_session", lambda: AUTH_RESULT)
    set_cached_calls = []
    monkeypatch.setattr(
        session_cache_module,
        "set_cached_shoonya_session",
        lambda session: set_cached_calls.append(session),
    )

    fake_adapters: list[_FakeAdapter] = []

    def _fake_adapter_ctor(settings, auth_result):
        fake = _FakeAdapter(
            settings,
            auth_result,
            margin_error=ShoonyaSessionExpiredError("get_margin", "Session Expired"),
        )
        fake_adapters.append(fake)
        return fake

    monkeypatch.setattr(shoonya_adapter_module, "ShoonyaBrokerAdapter", _fake_adapter_ctor)

    set_broker_calls = []
    monkeypatch.setattr(
        "app.modules.broker_adapter.composition.set_broker",
        lambda broker: set_broker_calls.append(broker),
    )

    main_module._attempt_shoonya_reconnect_from_cache()

    assert set_broker_calls == []
    assert fake_adapters[0].closed
    assert set_cached_calls == [None]


def test_transient_validation_failure_installs_anyway_and_keeps_the_cache(monkeypatch):
    """A `BrokerConnectivityError` (or anything else that isn't a
    `BrokerAuthError`) means the check call itself couldn't complete right
    now -- likely a transient network blip during boot -- not that the
    token is dead. Unlike a genuine `ShoonyaSessionExpiredError`, this must
    not discard a perfectly good cached session; the adapter is installed
    optimistically and `_AuthAwareBroker` catches a real auth failure later
    if the token truly is dead.
    """
    from app.modules.broker_adapter.base.errors import BrokerConnectivityError

    _patch_common(monkeypatch, settings=CONFIGURED_SETTINGS)
    monkeypatch.setattr(session_cache_module, "get_cached_shoonya_session", lambda: AUTH_RESULT)
    set_cached_calls = []
    monkeypatch.setattr(
        session_cache_module,
        "set_cached_shoonya_session",
        lambda session: set_cached_calls.append(session),
    )

    fake_adapters: list[_FakeAdapter] = []

    def _fake_adapter_ctor(settings, auth_result):
        fake = _FakeAdapter(
            settings, auth_result, margin_error=BrokerConnectivityError("timed out")
        )
        fake_adapters.append(fake)
        return fake

    monkeypatch.setattr(shoonya_adapter_module, "ShoonyaBrokerAdapter", _fake_adapter_ctor)

    set_broker_calls = []
    monkeypatch.setattr(
        "app.modules.broker_adapter.composition.set_broker",
        lambda broker: set_broker_calls.append(broker),
    )

    main_module._attempt_shoonya_reconnect_from_cache()

    assert set_broker_calls == [fake_adapters[0]]
    assert not fake_adapters[0].closed
    assert set_cached_calls == []


# --- _warm_shoonya_token_cache_from_db (restart token-cache warm-up) ----------

import contextlib  # noqa: E402

from app.modules.broker_adapter import composition as composition_module  # noqa: E402


class _FakeShoonyaAdapter:
    def __init__(self) -> None:
        self.warm_calls: list[list[tuple[str, str]]] = []

    def warm_token_cache(self, pairs: list[tuple[str, str]]) -> None:
        self.warm_calls.append(pairs)


def _patch_warm_common(monkeypatch, *, configured=True, inner=None):
    monkeypatch.setattr(composition_module, "is_shoonya_configured", lambda: configured)
    monkeypatch.setattr(composition_module, "get_broker", lambda: object())
    monkeypatch.setattr(composition_module, "unwrap_broker", lambda _b: inner)
    monkeypatch.setattr(shoonya_adapter_module, "ShoonyaBrokerAdapter", _FakeShoonyaAdapter)
    monkeypatch.setattr(
        "app.core.db.session.session_scope", lambda: contextlib.nullcontext(None)
    )


def test_warm_up_noop_when_not_shoonya_configured(monkeypatch):
    called = []
    monkeypatch.setattr(
        main_module, "_persisted_shoonya_option_tokens", lambda db: called.append(1) or []
    )
    _patch_warm_common(monkeypatch, configured=False, inner=_FakeShoonyaAdapter())

    main_module._warm_shoonya_token_cache_from_db()

    assert called == []


def test_warm_up_noop_when_broker_is_not_a_shoonya_adapter(monkeypatch):
    called = []
    monkeypatch.setattr(
        main_module, "_persisted_shoonya_option_tokens", lambda db: called.append(1) or []
    )
    _patch_warm_common(monkeypatch, inner=object())  # not a _FakeShoonyaAdapter

    main_module._warm_shoonya_token_cache_from_db()

    assert called == []


def test_warm_up_passes_persisted_pairs_to_the_adapter(monkeypatch):
    inner = _FakeShoonyaAdapter()
    pairs = [("NIFTY28AUG25C24000", "111"), ("BANKNIFTY28AUG25P52000", "222")]
    monkeypatch.setattr(main_module, "_persisted_shoonya_option_tokens", lambda db: pairs)
    _patch_warm_common(monkeypatch, inner=inner)

    main_module._warm_shoonya_token_cache_from_db()

    assert inner.warm_calls == [pairs]


def test_warm_up_is_non_fatal_when_query_raises(monkeypatch):
    inner = _FakeShoonyaAdapter()

    def _boom(db):
        raise RuntimeError("db down")

    monkeypatch.setattr(main_module, "_persisted_shoonya_option_tokens", _boom)
    _patch_warm_common(monkeypatch, inner=inner)

    main_module._warm_shoonya_token_cache_from_db()  # must not raise

    assert inner.warm_calls == []


def test_warm_up_is_non_fatal_when_adapter_call_raises(monkeypatch):
    class _Raiser(_FakeShoonyaAdapter):
        def warm_token_cache(self, pairs):
            raise RuntimeError("boom")

    inner = _Raiser()
    monkeypatch.setattr(main_module, "_persisted_shoonya_option_tokens", lambda db: [])
    _patch_warm_common(monkeypatch, inner=inner)

    main_module._warm_shoonya_token_cache_from_db()  # must not raise
