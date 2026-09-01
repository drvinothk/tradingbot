"""No real socket involved, same reasoning as `test_shoonya_ws_client.py`'s
own module docstring — `_run`/`_receive_loop` need a live server to exercise
meaningfully. These tests cover the `session_is_live` gate added 2026-09-01
(see `ws_client.py`'s `_SESSION_NOT_READY_BACKOFF_SECONDS` for the incident
this closes: a stale/expired Alice Blue session — the normal state every
morning until a human logs in — meant `_run` retried a doomed handshake,
*and* the `ensure_ws_session` REST call ahead of it, every 30s indefinitely;
live-confirmed ~190 failed attempts in one ~90 minute window, made worse
here than for Shoonya since this client is also the live failover backup).

No test file existed for `AliceBlueWSClient` at all before this one.
"""

from __future__ import annotations

import json
import threading
import time

import app.modules.market_data.providers.alice_blue_ws_client as alice_blue_ws_client_module
from app.modules.market_data.providers.alice_blue_ws_client import AliceBlueWSClient


def _client(**kwargs) -> tuple[AliceBlueWSClient, list]:
    ticks: list = []
    client = AliceBlueWSClient(
        "wss://example.test/ws",
        uid="FA1_API",
        actid="FA1_API",
        user_session="sess",
        on_tick=lambda t: ticks.append(t),
        **kwargs,
    )
    return client, ticks


class _RunLoopFakeConnection:
    """Real ack on the first `recv` (what `_authenticate` issues), then a
    real-1s-poll-timeout shape on every later `recv` (what `_receive_loop`
    issues) — an idle connection, not a hang.
    """

    def __init__(self, ack: dict):
        self._ack = json.dumps(ack)
        self._first = True
        self.sent: list[str] = []

    def send(self, message: str) -> None:
        self.sent.append(message)

    def recv(self, timeout: float | None = None) -> str:
        if self._first:
            self._first = False
            return self._ack
        raise TimeoutError


class _FakeConnectContext:
    def __init__(self, conn: _RunLoopFakeConnection):
        self._conn = conn

    def __enter__(self) -> _RunLoopFakeConnection:
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def _patch_fast_backoff_and_connect(monkeypatch) -> tuple[list[str], list[int]]:
    """Shrinks both backoff tuples to ~10ms and replaces `connect`/
    `create_ws_session` with fakes. Returns (connect_calls, ensure_session_calls).
    """
    connect_calls: list[str] = []

    def _fake_connect(url: str, open_timeout: float = 10) -> _FakeConnectContext:
        connect_calls.append(url)
        return _FakeConnectContext(_RunLoopFakeConnection({"t": "ck", "s": "OK"}))

    monkeypatch.setattr(alice_blue_ws_client_module, "connect", _fake_connect)
    monkeypatch.setattr(alice_blue_ws_client_module, "_RECONNECT_BACKOFF_SECONDS", (0.01,))
    monkeypatch.setattr(
        alice_blue_ws_client_module, "_SESSION_NOT_READY_BACKOFF_SECONDS", (0.01, 0.01)
    )
    return connect_calls, []


def _run_in_background(client: AliceBlueWSClient) -> threading.Thread:
    thread = threading.Thread(target=client._run, daemon=True)
    thread.start()
    return thread


def test_run_skips_connect_while_session_is_not_live(monkeypatch):
    connect_calls, _ = _patch_fast_backoff_and_connect(monkeypatch)
    client, _ = _client(session_is_live=lambda: False)

    thread = _run_in_background(client)
    time.sleep(0.2)
    client.stop()
    thread.join(timeout=2)

    assert connect_calls == []
    assert client._connected.is_set() is False


def test_run_also_skips_ensure_ws_session_while_session_is_not_live(monkeypatch):
    """`ensure_ws_session` (a real REST call, `POST .../createWsSess`) fires
    on every connect attempt today — the gate must short-circuit *before*
    that call too, not just before the WS handshake, since it's the more
    expensive of the two.
    """
    connect_calls, _ = _patch_fast_backoff_and_connect(monkeypatch)
    ensure_calls: list[int] = []
    client, _ = _client(
        session_is_live=lambda: False, ensure_ws_session=lambda: ensure_calls.append(1)
    )

    thread = _run_in_background(client)
    time.sleep(0.2)
    client.stop()
    thread.join(timeout=2)

    assert connect_calls == []
    assert ensure_calls == []


def test_run_attempts_connect_once_session_becomes_live(monkeypatch):
    connect_calls, _ = _patch_fast_backoff_and_connect(monkeypatch)
    live = {"value": False}
    client, _ = _client(session_is_live=lambda: live["value"])

    thread = _run_in_background(client)
    time.sleep(0.1)
    assert connect_calls == [], "must not attempt while session_is_live() is False"

    live["value"] = True
    connected = client._connected.wait(timeout=2.0)

    client.stop()
    thread.join(timeout=2)

    assert connected is True, "auth should complete once session_is_live() flips True"
    assert len(connect_calls) >= 1


def test_run_attempts_connect_immediately_when_session_is_live_is_unset(monkeypatch):
    """`session_is_live=None` (the default, and every caller that predates
    this parameter) must behave exactly as before — no gating.
    """
    connect_calls, _ = _patch_fast_backoff_and_connect(monkeypatch)
    client, _ = _client()  # no session_is_live passed

    thread = _run_in_background(client)
    connected = client._connected.wait(timeout=2.0)
    client.stop()
    thread.join(timeout=2)

    assert connected is True
    assert len(connect_calls) >= 1


def test_run_treats_a_raising_session_is_live_as_not_live(monkeypatch):
    connect_calls, _ = _patch_fast_backoff_and_connect(monkeypatch)

    def _raising() -> bool:
        raise RuntimeError("probe blew up")

    client, _ = _client(session_is_live=_raising)

    thread = _run_in_background(client)
    time.sleep(0.2)
    client.stop()
    thread.join(timeout=2)

    assert connect_calls == []
