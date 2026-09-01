"""No real socket involved — `_run`/`_receive_loop` (the actual connect/
reconnect loop) need a live server to exercise meaningfully, which this
phase has no account to test against anyway. These tests cover the pure
logic instead: subscription bookkeeping and inbound-message dispatch,
which is exactly what a field-name mismatch (this whole package's running
caveat) would break.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

import app.modules.broker_adapter.shoonya.ws_client as ws_client_module
from app.modules.broker_adapter.shoonya.ws_client import ShoonyaWSClient, _SubscriptionEntry


def _client(on_depth=None, **kwargs) -> tuple[ShoonyaWSClient, list, list]:
    ticks: list = []
    depths: list = []
    client = ShoonyaWSClient(
        "wss://example.test/ws",
        uid="FA1",
        actid="FA1",
        access_token="tok",
        on_tick=lambda t: ticks.append(t),
        on_depth=(lambda d: depths.append(d)) if on_depth is None else on_depth,
        **kwargs,
    )
    return client, ticks, depths


class _FakeConnection:
    """Stands in for `websockets.sync.connection.Connection` — `_authenticate`
    only ever calls `.send`/`.recv` on it.
    """

    def __init__(self, ack: dict):
        self.sent: list[str] = []
        self._ack = json.dumps(ack)

    def send(self, message: str) -> None:
        self.sent.append(message)

    def recv(self, timeout: float | None = None) -> str:
        return self._ack


def test_subscription_entry_key_is_exchange_pipe_token():
    entry = _SubscriptionEntry(contract_symbol="NIFTY30JUL26C24000", exchange="NFO", token="12345")
    assert entry.key == "NFO|12345"


def test_subscribe_populates_entries_by_key():
    client, _, _ = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])
    assert "NFO|12345" in client._entries_by_key
    assert client._entries_by_key["NFO|12345"].contract_symbol == "NIFTY30JUL26C24000"


def test_unsubscribe_removes_matching_entries():
    client, _, _ = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])
    client.unsubscribe(["NIFTY30JUL26C24000"])
    assert client._entries_by_key == {}


def test_has_subscriptions_reflects_remaining_entries():
    """This is what a caller that only owns *some* of a shared connection's
    subscriptions (e.g. a diagnostic call) checks before deciding to
    `stop()` the whole thing — must not falsely report empty while another
    caller's subscription is still live.
    """
    client, _, _ = _client()
    assert client.has_subscriptions() is False

    client.subscribe(
        [("NIFTY30JUL26C24000", "NFO", "12345"), ("BANKNIFTY30JUL26C50000", "NFO", "67890")]
    )
    assert client.has_subscriptions() is True

    client.unsubscribe(["NIFTY30JUL26C24000"])
    assert client.has_subscriptions() is True, "one entry remains, must not report empty"

    client.unsubscribe(["BANKNIFTY30JUL26C50000"])
    assert client.has_subscriptions() is False


def test_send_is_a_noop_without_a_live_connection():
    client, _, _ = _client()
    # Must not raise even though no `connect()` has ever happened.
    client._send({"t": "t", "k": "NFO|12345"})


def test_subscribe_sends_immediately_on_an_already_live_connection():
    """2026-08-12 regression test for the real bug found via a live
    market-hours diagnostic: `subscribe()` called on an already-connected
    client (any thread other than `_run`'s own) used to update
    `_entries_by_key` only, never actually writing to the socket, because
    `_send` required an explicit `ws=` that only `_run` ever passed. This
    is what `ticks_received: 0` traced back to — the subscribe request
    never left the process. `_live_ws` (set by `_run` once connected) is
    what `_send` now falls back to for exactly this case.
    """
    client, _, _ = _client()
    ws = _FakeConnection({"t": "ak", "s": "OK"})
    client._live_ws = ws

    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])

    sent = [json.loads(raw) for raw in ws.sent]
    assert {"t": "t", "k": "NFO|12345"} in sent


def test_unsubscribe_sends_immediately_on_an_already_live_connection():
    client, _, _ = _client()
    ws = _FakeConnection({"t": "ak", "s": "OK"})
    client._live_ws = ws
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])

    client.unsubscribe(["NIFTY30JUL26C24000"])

    assert json.loads(ws.sent[-1]) == {"t": "u", "k": "NFO|12345"}


def test_handle_message_dispatches_tick_to_on_tick():
    client, ticks, _ = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])

    message = json.dumps(
        {"t": "tk", "e": "NFO", "tk": "12345", "lp": "123.45", "bp1": "123.0", "sp1": "124.0"}
    )
    client._handle_message(message)

    assert len(ticks) == 1
    assert ticks[0].contract_symbol == "NIFTY30JUL26C24000"
    assert ticks[0].ltp == 123.45


def test_set_callbacks_repoints_dispatch_on_a_running_client():
    """`ShoonyaBrokerAdapter.subscribe_quotes` calls this when a Shoonya
    reconnect rebuilds the shared `BrokerPortMarketDataAdapter` — the live
    receive loop must forward to the new callback from the next frame on,
    without a reconnect.
    """
    client, original_ticks, _ = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])

    rebound_ticks: list = []
    client.set_callbacks(on_tick=lambda t: rebound_ticks.append(t))

    client._handle_message(
        json.dumps({"t": "tk", "e": "NFO", "tk": "12345", "lp": "123.45"})
    )

    assert original_ticks == []
    assert len(rebound_ticks) == 1
    assert rebound_ticks[0].ltp == 123.45


def test_handle_message_dispatches_depth_to_on_depth():
    client, _, depths = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])

    raw = {"t": "dk", "e": "NFO", "tk": "12345"}
    raw.update({f"bp{i}": str(100 - i) for i in range(1, 6)})
    raw.update({f"bq{i}": "10" for i in range(1, 6)})
    raw.update({f"sp{i}": str(100 + i) for i in range(1, 6)})
    raw.update({f"sq{i}": "10" for i in range(1, 6)})
    client._handle_message(json.dumps(raw))

    assert len(depths) == 1
    assert depths[0].contract_symbol == "NIFTY30JUL26C24000"


def test_handle_message_merges_a_partial_touchline_update_onto_the_last_snapshot():
    """2026-08-12 regression, live-observed on a real account during market
    hours: a real "tf" (touchline feed) update carries only the fields
    that actually changed since the last push -- e.g. only OI, never
    repeating 'lp' -- and must still parse successfully by inheriting the
    last known 'lp' from the prior "tk" snapshot, not get dropped as a
    NormalizationError. This is why the live diagnostic reported
    ticks_received: 0 despite real ticks genuinely arriving on the wire.
    """
    client, ticks, _ = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])

    full_snapshot = json.dumps(
        {"t": "tk", "e": "NFO", "tk": "12345", "lp": "123.45", "bp1": "123.0", "sp1": "124.0"}
    )
    client._handle_message(full_snapshot)

    partial_update = json.dumps({"t": "tf", "e": "NFO", "tk": "12345", "oi": "500"})
    client._handle_message(partial_update)

    assert len(ticks) == 2
    assert ticks[1].ltp == 123.45, "must inherit 'lp' from the last full snapshot"
    assert ticks[1].oi == 500


def test_handle_message_partial_update_before_any_snapshot_still_fails_to_parse():
    """No prior snapshot to merge onto means there's genuinely nothing to
    report yet -- this must still fail exactly as before the coalescing
    fix, not be papered over with a fabricated price.
    """
    client, ticks, _ = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])

    partial_update = json.dumps({"t": "tf", "e": "NFO", "tk": "12345", "oi": "500"})
    client._handle_message(partial_update)

    assert ticks == []


def test_unsubscribe_clears_the_cached_snapshot_for_that_key():
    client, _, _ = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])
    client._handle_message(json.dumps({"t": "tk", "e": "NFO", "tk": "12345", "lp": "123.45"}))
    assert client._last_known_by_key != {}

    client.unsubscribe(["NIFTY30JUL26C24000"])

    assert client._last_known_by_key == {}


def test_handle_message_ignores_unknown_token():
    client, ticks, _ = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])

    message = json.dumps({"t": "tk", "e": "NFO", "tk": "99999", "lp": "1.0"})
    client._handle_message(message)

    assert ticks == []


def test_handle_message_ignores_malformed_json():
    client, ticks, _ = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])
    client._handle_message("not json")  # must not raise
    assert ticks == []


def test_handle_message_swallows_normalization_error():
    client, ticks, _ = _client()
    client.subscribe([("NIFTY30JUL26C24000", "NFO", "12345")])
    # Missing 'lp' -> parse_tick raises NormalizationError, caught and logged.
    message = json.dumps({"t": "tk", "e": "NFO", "tk": "12345"})
    client._handle_message(message)
    assert ticks == []


def test_handle_message_dispatches_order_update_to_on_order_update():
    updates: list = []
    client, _, _ = _client(on_order_update=lambda msg: updates.append(msg))

    message = json.dumps({"t": "om", "norenordno": "26082000267157", "status": "COMPLETE"})
    client._handle_message(message)

    assert updates == [{"t": "om", "norenordno": "26082000267157", "status": "COMPLETE"}]


def test_handle_message_order_update_does_not_require_a_matching_subscription():
    """Order-update messages are account-level events, not tied to any
    subscribed instrument token — must not be silently dropped by the
    tick/depth subscription-key lookup, unlike a stray tick for an unknown
    token (see test_handle_message_ignores_unknown_token).
    """
    updates: list = []
    client, ticks, _ = _client(on_order_update=lambda msg: updates.append(msg))
    # Deliberately no subscribe() call at all.

    client._handle_message(json.dumps({"t": "om", "norenordno": "1", "status": "OPEN"}))

    assert len(updates) == 1
    assert ticks == []


def test_handle_message_swallows_order_update_callback_exception():
    def _boom(_msg: dict) -> None:
        raise RuntimeError("boom")

    client, ticks, _ = _client(on_order_update=_boom)
    # Must not raise even though the callback itself blows up.
    client._handle_message(json.dumps({"t": "om", "norenordno": "1", "status": "OPEN"}))
    assert ticks == []


def test_handle_message_ignores_order_update_when_no_callback_registered():
    client, ticks, _ = _client()  # on_order_update defaults to None
    client._handle_message(json.dumps({"t": "om", "norenordno": "1", "status": "OPEN"}))
    assert ticks == []


def test_authenticate_defaults_source_to_api():
    """Payload shape per Shoonya support's own 2026-08-11 reply — "t": "a"
    and "accesstoken", replacing the old "t": "c"/"susertoken" pair that
    never once got past NOT_OK against the previous OAuth-migration-era
    payload.
    """
    client, _, _ = _client()
    ws = _FakeConnection({"t": "ak", "s": "OK"})
    client._authenticate(ws)
    sent = json.loads(ws.sent[0])
    assert sent == {
        "t": "a",
        "uid": "FA1",
        "actid": "FA1",
        "accesstoken": "tok",
        "source": "API",
    }


def test_authenticate_sends_configured_source():
    client, _, _ = _client(source="WEB")
    ws = _FakeConnection({"t": "ak", "s": "OK"})
    client._authenticate(ws)
    sent = json.loads(ws.sent[0])
    assert sent["source"] == "WEB"


def test_authenticate_raises_on_not_ok_ack():
    client, _, _ = _client()
    ws = _FakeConnection({"t": "ak", "s": "NOT_OK"})
    with pytest.raises(ConnectionError):
        client._authenticate(ws)




# -- volume proxy (NSE cash-index has no `v`; front-month future supplies it) --


def _idx_tick(**over):
    msg = {"t": "tf", "e": "NSE", "tk": "26000", "lp": "24100.0"}
    msg.update(over)
    return json.dumps(msg)


def _fut_tick(v):
    return json.dumps({"t": "tf", "e": "NFO", "tk": "68407", "lp": "24300.0", "v": str(v)})


def test_volume_proxy_splices_future_volume_increment_into_index_ticks():
    client, ticks, _ = _client()
    client.subscribe([("NIFTY", "NSE", "26000")])
    client.set_volume_proxy(("NIFTY", "NSE", "26000"), ("NIFTY29SEP26F", "NFO", "68407"))

    client._handle_message(_fut_tick(1_000_000))   # source baseline
    client._handle_message(_idx_tick())            # first index tick -> increment 0
    client._handle_message(_fut_tick(1_002_500))   # +2500 cumulative
    client._handle_message(_idx_tick(lp="24110.0"))

    assert [t.contract_symbol for t in ticks] == ["NIFTY", "NIFTY"]
    assert ticks[0].volume == 0
    assert ticks[0].ltp == 24100.0          # price stays the index price
    assert ticks[1].volume == 2500
    assert ticks[1].ltp == 24110.0


def test_volume_proxy_source_frames_are_never_forwarded():
    client, ticks, _ = _client()
    client.subscribe([("NIFTY", "NSE", "26000")])
    client.set_volume_proxy(("NIFTY", "NSE", "26000"), ("NIFTY29SEP26F", "NFO", "68407"))

    client._handle_message(_fut_tick(1_000_000))
    client._handle_message(_fut_tick(1_000_500))

    assert ticks == []


def test_volume_proxy_clamps_to_zero_when_cumulative_volume_resets():
    client, ticks, _ = _client()
    client.subscribe([("NIFTY", "NSE", "26000")])
    client.set_volume_proxy(("NIFTY", "NSE", "26000"), ("NIFTY29SEP26F", "NFO", "68407"))

    client._handle_message(_fut_tick(9_000_000))
    client._handle_message(_idx_tick())            # baseline established, 0
    client._handle_message(_fut_tick(1_200))       # new day: cum < last
    client._handle_message(_idx_tick())            # must not go negative / huge
    client._handle_message(_fut_tick(3_700))
    client._handle_message(_idx_tick())

    assert [t.volume for t in ticks] == [0, 0, 2500]


def test_volume_proxy_index_tick_before_any_future_frame_reports_zero_volume():
    client, ticks, _ = _client()
    client.subscribe([("NIFTY", "NSE", "26000")])
    client.set_volume_proxy(("NIFTY", "NSE", "26000"), ("NIFTY29SEP26F", "NFO", "68407"))

    client._handle_message(_idx_tick())

    assert ticks[0].volume == 0


def test_set_volume_proxy_supersedes_old_source_on_rollover():
    client, _, _ = _client()
    client.subscribe([("NIFTY", "NSE", "26000")])
    client.set_volume_proxy(("NIFTY", "NSE", "26000"), ("NIFTY29SEP26F", "NFO", "68407"))
    client.set_volume_proxy(("NIFTY", "NSE", "26000"), ("NIFTY27OCT26F", "NFO", "70001"))

    assert "NFO|68407" not in client._entries_by_key
    assert "NFO|70001" in client._entries_by_key
    assert client._volume_proxy["NSE|26000"] == "NFO|70001"


def test_unsubscribe_target_also_drops_its_volume_proxy_source():
    client, _, _ = _client()
    client.subscribe([("NIFTY", "NSE", "26000")])
    client.set_volume_proxy(("NIFTY", "NSE", "26000"), ("NIFTY29SEP26F", "NFO", "68407"))
    client.unsubscribe(["NIFTY"])

    assert client._entries_by_key == {}
    assert client._volume_proxy == {}


# -- `_run`'s session_is_live gate (2026-09-01) -------------------------------
#
# `_run`/`_receive_loop` still need a live server to test meaningfully in
# general (this file's own module docstring) — but the gate itself is pure
# control flow around whether `connect()` gets called at all, testable with
# `connect` monkeypatched to a fake and the backoff tuples shrunk to make the
# test fast, same spirit as every other test here staying deterministic and
# socket-free.


class _RunLoopFakeConnection:
    """Gives `_authenticate` a real ack on the first `recv`, then makes every
    later `recv` (the ones `_receive_loop` issues) behave like Shoonya's own
    real 1s poll timeout — never any further messages, so `_receive_loop`
    just idles until `_stop` is set, exactly like a genuinely idle connection.
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

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _patch_fast_backoff_and_connect(monkeypatch) -> list[str]:
    """Shrinks both backoff tuples to ~10ms so these tests don't spend real
    wall-clock seconds waiting out `_RECONNECT_BACKOFF_SECONDS`/
    `_SESSION_NOT_READY_BACKOFF_SECONDS`, and replaces `connect` with a fake
    that records each attempted URL. Returns that list.
    """
    connect_calls: list[str] = []

    def _fake_connect(url: str, open_timeout: float = 10) -> _FakeConnectContext:
        connect_calls.append(url)
        return _FakeConnectContext(_RunLoopFakeConnection({"t": "ak", "s": "OK"}))

    monkeypatch.setattr(ws_client_module, "connect", _fake_connect)
    monkeypatch.setattr(ws_client_module, "_RECONNECT_BACKOFF_SECONDS", (0.01,))
    monkeypatch.setattr(ws_client_module, "_SESSION_NOT_READY_BACKOFF_SECONDS", (0.01, 0.01))
    return connect_calls


def _run_in_background(client: ShoonyaWSClient) -> threading.Thread:
    thread = threading.Thread(target=client._run, daemon=True)
    thread.start()
    return thread


def test_run_skips_connect_while_session_is_not_live(monkeypatch):
    connect_calls = _patch_fast_backoff_and_connect(monkeypatch)
    client, _, _ = _client(session_is_live=lambda: False)

    thread = _run_in_background(client)
    time.sleep(0.2)
    client.stop()
    thread.join(timeout=2)

    assert connect_calls == []
    assert client._connected.is_set() is False


def test_run_attempts_connect_once_session_becomes_live(monkeypatch):
    connect_calls = _patch_fast_backoff_and_connect(monkeypatch)
    live = {"value": False}
    client, _, _ = _client(session_is_live=lambda: live["value"])

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
    """`session_is_live=None` (the default, and every existing caller that
    predates this parameter) must behave exactly as before — no gating.
    """
    connect_calls = _patch_fast_backoff_and_connect(monkeypatch)
    client, _, _ = _client()  # no session_is_live passed

    thread = _run_in_background(client)
    for _ in range(50):
        if connect_calls:
            break
        time.sleep(0.02)
    client.stop()
    thread.join(timeout=2)

    assert len(connect_calls) >= 1


def test_run_treats_a_raising_session_is_live_as_not_live(monkeypatch):
    connect_calls = _patch_fast_backoff_and_connect(monkeypatch)

    def _raising() -> bool:
        raise RuntimeError("probe blew up")

    client, _, _ = _client(session_is_live=_raising)

    thread = _run_in_background(client)
    time.sleep(0.2)
    client.stop()
    thread.join(timeout=2)

    assert connect_calls == []
    assert client._proxy_last_cum_v == {}
