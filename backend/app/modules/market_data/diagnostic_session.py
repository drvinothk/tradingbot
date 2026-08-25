"""Backs the Market Terminal "WS Quality Test" control (added 2026-08-22,
user-designed): a dropdown with three choices — "Test Default", "Test
Failback", "Both" — that deliberately never names a broker. "Default"
always means whatever `Settings.market_data.provider` currently resolves
to; "Failback" always means whatever `Settings.market_data.
failover_backup_provider` currently is. As of 2026-08-25 that's Shoonya
(primary)/Alice Blue (backup) — see `provider_composition.py`'s own
`_RECOGNIZED_FAILOVER_BACKUPS` comment for the promotion writeup — but the
exact same "Test Failback" button would start testing whatever provider is
configured next with zero code change here, since Alice Blue's own name is
never hardcoded into the UI or this module, the same lesson learned from
the Angel One archiving bug (a hardcoded provider list drifting out of
sync with the real config).

**Two fundamentally different mechanisms per role, decided at start time**:

- **"default"**: read-only. `provider_composition.get_market_data_provider()`
  is *already* connected and subscribed to NIFTY/BANKNIFTY in production
  (`MarketDataIngestionService`'s own job) — this just polls its already-
  warm `get_latest_tick()` every 30s. No new connection, zero added load.
- **"failback"**: the backup provider is *not* necessarily subscribed to
  anything right now, so this has to actively test it. Two genuinely
  different cases:
  - `provider == "shoonya"`: Shoonya allows exactly **one** connection per
    account (see `BrokerPort.subscribe_quotes`'s own docstring) — this
    can't open an isolated second one, so it reuses the same shared
    `broker_adapter.composition.get_broker()` singleton every other Shoonya
    caller uses, via a throwaway `BrokerPortMarketDataAdapter` wrapper.
    Deliberately **never unsubscribes** when the run stops —
    `BrokerPortMarketDataAdapter.unsubscribe_ticks` calls
    `broker.unsubscribe_quotes` on the *shared* connection, which removes
    the wire-level subscription for that symbol/token regardless of which
    adapter instance asked, exactly the kind of cross-caller clobbering
    `broker_port_shim.py`'s own docstring already documents two live
    incidents of. Leaving the subscription running afterward is harmless
    (matches "subscribe once, keep forever" — every other subscriber in
    this codebase already behaves this way) and is strictly safer than
    risking silently killing a different caller's subscription to the same
    symbol.
  - `provider == "alice_blue"`: a genuinely isolated `AliceBlueWSClient`
    connection (Alice Blue has no such one-connection restriction) — same
    mechanics as the one-shot `/aliceblue/ws-tick-diagnostic`, just kept
    running and snapshotted instead of torn down after a bounded duration.
    This session's own connection is exclusively owned here, so stopping it
    cleanly on `stop()` is always safe.

Every ~30s while a role is running, one `MarketDataDiagnosticSnapshot` row
per symbol is written (not every raw tick — see that model's own docstring
for why) so a full trading day's worth of data survives a backend restart
and can be exported later via `GET /reports/ws-quality-export`.

**`_session_factory` is a swappable module global, not a bare `session_scope()`
call** — same reasoning as `market_data/registry.py`'s own `session_factory`
parameters and this project's own documented incident (CLAUDE.md's QC-pass
notes) where a background path defaulting straight to the production
`session_scope` silently wrote real rows into the dev DB from inside a test
run. This module is invoked from HTTP endpoints with no natural place to
thread a per-call `session_factory` through, so it follows the composition-
root pattern instead (`broker_adapter.composition.set_broker`,
`provider_composition.set_market_data_provider`): a module-level default,
swapped via `set_session_factory`/`reset_for_tests` for the whole test
session, not per call.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.db.session import session_scope
from app.domain.ops.models import MarketDataDiagnosticRun, MarketDataDiagnosticSnapshot
from app.modules.broker_adapter.composition import get_broker, is_shoonya_configured
from app.modules.market_data.provider_composition import get_market_data_provider
from app.modules.market_data.providers.alice_blue_auth import create_ws_session
from app.modules.market_data.providers.alice_blue_session import get_alice_blue_session
from app.modules.market_data.providers.alice_blue_ws_client import AliceBlueWSClient
from app.modules.market_data.providers.broker_port_shim import BrokerPortMarketDataAdapter

logger = logging.getLogger("app.market_data.diagnostic_session")

SessionFactory = Callable[[], AbstractContextManager[Session]]

_SNAPSHOT_INTERVAL_SECONDS = 30.0
_ROLES = ("default", "failback")
# (our own DB symbol, Alice Blue's own INDICES display name) -- same mapping
# as alice_blue_scrip_master._INDEX_DISPLAY_NAME, kept local to avoid a
# cross-module import just for two literals.
_ALICE_BLUE_SUBSCRIPTIONS = (
    ("NIFTY", "NIFTY 50", "NSE", "26000"),
    ("BANKNIFTY", "NIFTY BANK", "NSE", "26009"),
)
_SYMBOLS = ("NIFTY", "BANKNIFTY")

_session_factory: SessionFactory = session_scope


class UnsupportedFailbackProviderError(Exception):
    pass


@dataclass
class _RoleState:
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    run_id: uuid.UUID | None = None
    provider_name: str | None = None
    ws_client: AliceBlueWSClient | None = None


_lock = threading.Lock()
_states: dict[str, _RoleState] = {role: _RoleState() for role in _ROLES}


def set_session_factory(factory: SessionFactory) -> None:
    global _session_factory
    _session_factory = factory


def _resolve_provider_name(role: str) -> str:
    settings = get_settings().market_data
    return settings.provider if role == "default" else settings.failover_backup_provider


def is_running(role: str) -> bool:
    state = _states[role]
    return state.thread is not None and state.thread.is_alive()


def _validate_can_run(role: str, provider_name: str) -> None:
    """Checked synchronously, before the background thread ever starts —
    the thread-body functions (`_run_shoonya_failback_loop`/
    `_run_alice_blue_failback_loop`) re-check the same conditions
    defensively (a real race is possible: a session can be cleared between
    this check and the loop's first iteration), but the *primary* check
    lives here so `start()` fails loud and synchronous, and the API layer's
    `try/except RuntimeError` around it actually catches something —
    without this, every precondition failure would only ever surface as an
    async `MarketDataDiagnosticRun.status="error"` row a caller would have
    to separately poll for, not a clear rejected HTTP response.
    """
    if role == "default":
        return  # always safe -- a passive read of the already-warm active provider
    if provider_name == "shoonya":
        if not is_shoonya_configured():
            raise RuntimeError("Shoonya is not connected -- connect it first (Market Terminal).")
    elif provider_name == "alice_blue":
        if get_alice_blue_session() is None:
            raise RuntimeError(
                "No Alice Blue session -- connect it first (Market Terminal)."
            )
    else:
        raise UnsupportedFailbackProviderError(
            f"No isolated diagnostic exists for failback provider {provider_name!r}"
        )


def start(role: str, workspace_id: uuid.UUID) -> dict:
    if role not in _ROLES:
        raise ValueError(f"unknown role {role!r} -- must be one of {_ROLES}")

    with _lock:
        state = _states[role]
        if state.thread is not None and state.thread.is_alive():
            return {
                "already_running": True,
                "run_id": str(state.run_id),
                "provider": state.provider_name,
            }

        provider_name = _resolve_provider_name(role)
        _validate_can_run(role, provider_name)
        run_id = uuid.uuid4()
        now = datetime.now(UTC)
        with _session_factory() as db:
            db.add(
                MarketDataDiagnosticRun(
                    id=run_id,
                    workspace_id=workspace_id,
                    role=role,
                    provider=provider_name,
                    started_at=now,
                    status="running",
                )
            )

        state.run_id = run_id
        state.provider_name = provider_name
        state.stop_event = threading.Event()
        state.thread = threading.Thread(target=_run_role, args=(role, state), daemon=True)
        state.thread.start()
        return {"already_running": False, "run_id": str(run_id), "provider": provider_name}


def start_many(roles: list[str], workspace_id: uuid.UUID) -> dict:
    """"Both" mode's entry point — validates *every* requested role before
    starting *any* of them. Without this two-phase split, a request for
    `["default", "failback"]` where "default" is always safe but
    "failback" fails validation would start "default"'s real background
    thread + DB row, then raise on "failback" — the caller sees an error
    response with no `run_id`, but a role is now silently running with no
    way to know it started short of separately polling `status()`. Whether
    to fail the whole request atomically (nothing starts) or accept
    "default" started while surfacing "failback"'s error is a real design
    choice, not a slip either way — this project picked atomic: an error
    response should mean nothing changed, matching a real trading system's
    "never leave a caller unsure what actually happened" bar.
    """
    for role in roles:
        state = _states[role]
        if state.thread is not None and state.thread.is_alive():
            continue
        _validate_can_run(role, _resolve_provider_name(role))

    return {role: start(role, workspace_id) for role in roles}


def stop(role: str) -> dict:
    if role not in _ROLES:
        raise ValueError(f"unknown role {role!r} -- must be one of {_ROLES}")

    with _lock:
        state = _states[role]
        thread = state.thread
        if thread is None:
            return {"was_running": False}
        state.stop_event.set()

    thread.join(timeout=_SNAPSHOT_INTERVAL_SECONDS + 5)

    with _lock:
        run_id = state.run_id
        client = state.ws_client
        state.ws_client = None
        state.thread = None

    if client is not None:
        client.stop()

    if run_id is not None:
        with _session_factory() as db:
            run = db.get(MarketDataDiagnosticRun, run_id)
            if run is not None and run.status == "running":
                run.stopped_at = datetime.now(UTC)
                run.status = "stopped"

    return {"was_running": True}


def stop_all() -> None:
    """App-shutdown hygiene, mirroring every other scheduler's `stop_*`
    call in `app.main`'s lifespan — a diagnostic run left "running" across a
    restart is harmless (its already-written snapshots stay valid, it just
    never gets a `stopped_at`), but cleanly marking it stopped keeps the
    Reports export from showing an open-ended run that silently died.
    """
    for role in _ROLES:
        try:
            stop(role)
        except Exception:
            logger.exception("Failed to stop diagnostic role %r during shutdown", role)


def status() -> dict:
    with _lock:
        return {
            role: {
                "running": is_running(role),
                "provider": state.provider_name,
                "run_id": str(state.run_id) if state.run_id else None,
            }
            for role, state in _states.items()
        }


def _run_role(role: str, state: _RoleState) -> None:
    assert state.run_id is not None
    try:
        if role == "default":
            _run_default_loop(state)
        else:
            _run_failback_loop(state)
    except Exception as exc:
        logger.exception("Diagnostic role %r crashed", role)
        with _session_factory() as db:
            run = db.get(MarketDataDiagnosticRun, state.run_id)
            if run is not None:
                run.status = "error"
                run.detail = str(exc)[:500]
                run.stopped_at = datetime.now(UTC)


def _write_snapshot(
    run_id: uuid.UUID, symbol: str, connected: bool, ltp: float | None, tick_ts: datetime | None
) -> None:
    with _session_factory() as db:
        db.add(
            MarketDataDiagnosticSnapshot(
                id=uuid.uuid4(),
                run_id=run_id,
                recorded_at=datetime.now(UTC),
                symbol=symbol,
                connected=connected,
                ltp=ltp,
                tick_ts=tick_ts,
            )
        )


def _run_default_loop(state: _RoleState) -> None:
    assert state.run_id is not None
    provider = get_market_data_provider()
    while not state.stop_event.is_set():
        for symbol in _SYMBOLS:
            tick = provider.get_latest_tick(symbol)
            _write_snapshot(
                state.run_id,
                symbol,
                tick is not None,
                tick.ltp if tick is not None else None,
                tick.ts if tick is not None else None,
            )
        state.stop_event.wait(_SNAPSHOT_INTERVAL_SECONDS)


def _run_failback_loop(state: _RoleState) -> None:
    provider_name = state.provider_name
    if provider_name == "shoonya":
        _run_shoonya_failback_loop(state)
    elif provider_name == "alice_blue":
        _run_alice_blue_failback_loop(state)
    else:
        raise UnsupportedFailbackProviderError(
            f"No isolated diagnostic exists for failback provider {provider_name!r}"
        )


def _run_shoonya_failback_loop(state: _RoleState) -> None:
    assert state.run_id is not None
    if not is_shoonya_configured():
        raise RuntimeError("Shoonya is not connected -- connect it first (Market Terminal).")

    adapter = BrokerPortMarketDataAdapter(get_broker())
    latest: dict[str, object] = {}

    def on_tick(tick: object) -> None:
        latest[tick.contract_symbol] = tick  # type: ignore[attr-defined]

    adapter.subscribe_ticks(list(_SYMBOLS), on_tick)  # type: ignore[arg-type]

    # Deliberately no `finally: adapter.unsubscribe_ticks(...)` -- see this
    # module's own docstring for why that would risk killing a different
    # caller's subscription to the same shared Shoonya connection.
    while not state.stop_event.is_set():
        for symbol in _SYMBOLS:
            tick = latest.get(symbol)
            _write_snapshot(
                state.run_id,
                symbol,
                tick is not None,
                tick.ltp if tick is not None else None,  # type: ignore[attr-defined]
                tick.ts if tick is not None else None,  # type: ignore[attr-defined]
            )
        state.stop_event.wait(_SNAPSHOT_INTERVAL_SECONDS)


def _run_alice_blue_failback_loop(state: _RoleState) -> None:
    assert state.run_id is not None
    session = get_alice_blue_session()
    if session is None:
        raise RuntimeError(
            "No Alice Blue session -- connect it first (Market Terminal)."
        )

    settings = get_settings().alice_blue
    latest: dict[str, object] = {}

    def on_tick(tick: object) -> None:
        latest[tick.contract_symbol] = tick  # type: ignore[attr-defined]

    client = AliceBlueWSClient(
        settings.ws_host,
        uid=f"{session.client_id}_API",
        actid=f"{session.client_id}_API",
        user_session=session.user_session,
        on_tick=on_tick,  # type: ignore[arg-type]
        ensure_ws_session=lambda: create_ws_session(settings, session),
    )
    state.ws_client = client
    client.start()
    client.subscribe([(label, exch, token) for _, label, exch, token in _ALICE_BLUE_SUBSCRIPTIONS])

    while not state.stop_event.is_set():
        for our_symbol, label, _exch, _token in _ALICE_BLUE_SUBSCRIPTIONS:
            tick = latest.get(label)
            _write_snapshot(
                state.run_id,
                our_symbol,
                tick is not None,
                tick.ltp if tick is not None else None,  # type: ignore[attr-defined]
                tick.ts if tick is not None else None,  # type: ignore[attr-defined]
            )
        state.stop_event.wait(_SNAPSHOT_INTERVAL_SECONDS)


def reset_for_tests(session_factory: SessionFactory | None = None) -> None:
    global _states, _session_factory
    for role in _ROLES:
        state = _states[role]
        if state.thread is not None:
            state.stop_event.set()
    _states = {role: _RoleState() for role in _ROLES}
    _session_factory = session_factory if session_factory is not None else session_scope
