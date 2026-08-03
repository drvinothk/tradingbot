"""The composition root every other module's docstrings already promise:
"A composition-root in `main.py` decides whether `mock` or `shoonya` gets
injected — no caller ever imports a concrete adapter directly." `get_broker`
is that decision point, lazily constructing one process-wide `BrokerPort`
instance.

This is only a meaningful pattern because `core.locking.LOCK_PROCESS_SINGLETON`
already guarantees exactly one backend process — the same reasoning
`api.v1.strategies`'s `_RUNNERS` in-memory dict already relies on for
tracking live `StrategyRunner` threads, extended here to a second
piece of required in-process state (the paper Execution Service and every
`PositionManager` thread need to share the *same* adapter instance, since
`MockBrokerAdapter`'s internal order/position book only means anything if
there's one of it).

The lazy default resolves to `MockBrokerAdapter`, seeded with a real
synthetic instrument universe (`build_mock_universe`) — found missing
during Phase 4's manual QC: `get_option_chain()` against a bare
`MockBrokerAdapter()` always returned empty (ticks/orders/positions all
work regardless, hash-seeded by symbol string, independent of
`self._instruments`), so no strategy could ever rank a real contract
against the live singleton, only against tests' own explicitly seeded
adapters. `app.main`'s startup also calls `sync_instrument_master` against
this same instance so `instruments`/`option_contracts` DB rows match what
it actually quotes.

Phase 5 adds the other branch: once `api.v1.shoonya.oauth_callback` has
completed a real OAuth login, it calls `set_broker` with a constructed
`ShoonyaBrokerAdapter` — from that point on, every module's `get_broker()`
call (market data ingestion, instrument sync, option-chain snapshots) talks
to the real broker with zero code changes, exactly the point of this
composition-root pattern. Nothing in this module imports
`ShoonyaBrokerAdapter` at module scope — it's imported lazily inside
`api.v1.shoonya` instead, so a process that never touches Shoonya (every
test, and local dev before real credentials exist) never even imports
`httpx`/`websockets`-touching code.

`get_broker()` is deliberately *not* what paper execution uses to decide
which broker places orders — see `get_execution_broker` below. An audit
found that every execution call site (`dispatch_trade_intent`,
`close_position`, `PositionManager`, manual square-off/reconcile) used to
fall back to this same `get_broker()`, which meant connecting Shoonya for
real market data (Phase 5's actual intent) silently turned every "paper"
trade into a real order against Shoonya's live `PlaceOrder` endpoint the
next time a strategy fired — nothing anywhere checked `TradingSession.mode`
first. `get_execution_broker` closes that gap by keeping a persistent mock
instance execution always uses today, entirely independent of whatever
`set_broker()` has installed in `_broker`.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from app.domain.market.mock_universe import build_mock_universe
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.mock.adapter import MockBrokerAdapter

if TYPE_CHECKING:
    from app.domain.session.models import TradingSession

_broker: BrokerPort | None = None
_execution_mock: MockBrokerAdapter | None = None


def _next_weekly_expiry() -> date:
    """Nearest Thursday on/after today — matches real NIFTY/BANKNIFTY weekly
    expiry cadence closely enough for a synthetic universe; exact day doesn't
    matter since nothing here is priced against a real listing."""
    today = date.today()
    return today + timedelta(days=(3 - today.weekday()) % 7)


def _get_or_create_execution_mock() -> MockBrokerAdapter:
    """The one persistent `MockBrokerAdapter` instance execution ever uses,
    never replaced by `set_broker()`. Kept separate from `_broker` so that
    `get_broker()`'s default (before anything is connected) and
    `get_execution_broker()` return the *same* instance — required so
    `PositionManager`'s quote reads and reconciliation's position reads
    never diverge from what `dispatch_trade_intent`/`close_position` wrote,
    for the common case today where nothing real is connected yet.
    """
    global _execution_mock
    if _execution_mock is None:
        _execution_mock = MockBrokerAdapter(instruments=build_mock_universe(_next_weekly_expiry()))
    return _execution_mock


def get_broker() -> BrokerPort:
    global _broker
    if _broker is None:
        _broker = _get_or_create_execution_mock()
    return _broker


def get_execution_broker(trading_session: TradingSession) -> BrokerPort:
    """Broker resolution for anything that places or reads orders/positions
    for a specific session — `dispatch_trade_intent`, `close_position`,
    `PositionManager`, manual square-off/reconcile, and startup recovery.
    Deliberately separate from `get_broker()`, which stays for market-data
    call sites (instrument sync, option-chain snapshots, ingestion) that
    legitimately want "whichever real broker is connected" with no session
    context available.

    Always returns the persistent mock today, regardless of what
    `get_broker()` currently resolves to or what `trading_session.mode` is
    — there is no live-order path anywhere in this codebase yet (Phase 6:
    guarded-live execution). `trading_session` is accepted now so this
    function's signature doesn't need to change when Phase 6 adds real
    per-strategy graduation gating; until then, deliberately not
    half-building that gate here without Phase 6's surrounding safeguards
    (one-lot enforcement, sign-off checklist) to back it.
    """
    del trading_session  # unused until Phase 6's graduation gating exists
    return _get_or_create_execution_mock()


def set_broker(broker: BrokerPort | None) -> None:
    """Test/composition-root hook — lets tests inject a seeded
    `MockBrokerAdapter` (or a fake) instead of the lazily-constructed
    default, lets `main.py` reset state between process lifespans in tests
    that exercise startup/shutdown more than once, and is what
    `api.v1.shoonya.oauth_callback` calls with a real `ShoonyaBrokerAdapter`
    once OAuth login completes. Only ever affects `get_broker()`'s data
    slot — `get_execution_broker()` is untouched by this call, deliberately:
    logging out of a real broker (or a test resetting `_broker` alone)
    shouldn't wipe whatever paper positions the execution mock is tracking.
    """
    global _broker
    _broker = broker


def reset_for_tests() -> None:
    """Full reset of both slots — unlike `set_broker(None)` alone, this also
    clears `_execution_mock`, so a test that relies on `get_execution_broker`
    /`get_broker`'s lazy default gets a genuinely fresh `MockBrokerAdapter`
    with no leftover orders/positions from a previous test, matching what
    `set_broker(None)` alone already guaranteed before `get_execution_broker`
    existed. Test-only — production code never needs to forget the
    execution mock's state.
    """
    global _broker, _execution_mock
    _broker = None
    _execution_mock = None


def is_shoonya_configured() -> bool:
    """Whether `get_broker()` currently resolves to a real Shoonya
    connection rather than the mock — the frontend needs this to decide
    whether to show "Connect Shoonya" or "Connected"/"Reconnect Shoonya".
    """
    from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter

    return isinstance(_broker, ShoonyaBrokerAdapter)
