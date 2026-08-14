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

import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy.orm import object_session

from app.config.settings import get_settings
from app.domain.execution.models import Order as OrderRow
from app.domain.execution.models import OrderMode
from app.domain.execution.models import Position as PositionRow
from app.domain.market.mock_universe import build_mock_universe
from app.domain.session.models import SafeMode
from app.domain.strategy.models import StrategyConfig, StrategyRuntimeMode, StrategyStatus
from app.modules.broker_adapter.base.broker_port import BrokerPort, DepthCallback, TickCallback
from app.modules.broker_adapter.base.contracts import (
    AuthResult,
    DepthSnapshot,
    InstrumentInfo,
    MarginInfo,
    OptionChainSnapshot,
    OrderRequest,
    OrderResult,
    Position,
    PriceCandle,
    Tick,
)
from app.modules.broker_adapter.base.errors import BrokerAuthError, ConfigurationError
from app.modules.broker_adapter.mock.adapter import MockBrokerAdapter

if TYPE_CHECKING:
    from app.domain.session.models import TradingSession
    from app.domain.strategy.models import StrategyRun

logger = logging.getLogger("app.broker_adapter.composition")

_broker: BrokerPort | None = None
_execution_mock: MockBrokerAdapter | None = None
_shoonya_connected: bool = False


class _AuthAwareBroker(BrokerPort):
    """Wraps whatever real adapter `set_broker` installs so a `BrokerAuthError`
    raised by *any* `BrokerPort` call — market data ingestion, instrument
    sync, strategy ranking's option-chain refresh, a future live
    `get_margin` check, whichever call site happens to trip it — flips
    `is_shoonya_configured()`/`GET /shoonya/status` back to disconnected,
    not just whichever one call site happened to notice. Purely an observer:
    it doesn't retry, doesn't swallow the error (always re-raises), and
    doesn't close or replace the underlying connection — that stays
    `set_broker`'s job, since tearing down a WS/REST client mid-call from
    inside the call that's failing is its own hazard.
    """

    def __init__(self, inner: BrokerPort) -> None:
        self._inner = inner

    def _mark_disconnected(self, exc: BrokerAuthError) -> None:
        global _shoonya_connected
        if _shoonya_connected:
            logger.warning("Shoonya broker connection failed (%s); marking disconnected", exc)
        _shoonya_connected = False

    def authenticate(self) -> AuthResult:
        try:
            return self._inner.authenticate()
        except BrokerAuthError as exc:
            self._mark_disconnected(exc)
            raise

    def get_instrument_master(self, exchange: str) -> list[InstrumentInfo]:
        try:
            return self._inner.get_instrument_master(exchange)
        except BrokerAuthError as exc:
            self._mark_disconnected(exc)
            raise

    def get_option_chain(self, underlying: str, expiry: date) -> OptionChainSnapshot:
        try:
            return self._inner.get_option_chain(underlying, expiry)
        except BrokerAuthError as exc:
            self._mark_disconnected(exc)
            raise

    def get_price_history(
        self, underlying: str, start: datetime, end: datetime, timeframe_seconds: int = 60
    ) -> list[PriceCandle]:
        try:
            return self._inner.get_price_history(underlying, start, end, timeframe_seconds)
        except BrokerAuthError as exc:
            self._mark_disconnected(exc)
            raise

    def get_quote(self, contract_symbol: str) -> Tick:
        try:
            return self._inner.get_quote(contract_symbol)
        except BrokerAuthError as exc:
            self._mark_disconnected(exc)
            raise

    def get_depth(self, contract_symbol: str) -> DepthSnapshot:
        try:
            return self._inner.get_depth(contract_symbol)
        except BrokerAuthError as exc:
            self._mark_disconnected(exc)
            raise

    def subscribe_quotes(
        self,
        contract_symbols: list[str],
        on_tick: TickCallback,
        on_depth: DepthCallback | None = None,
    ) -> None:
        try:
            self._inner.subscribe_quotes(contract_symbols, on_tick, on_depth)
        except BrokerAuthError as exc:
            self._mark_disconnected(exc)
            raise

    def unsubscribe_quotes(self, contract_symbols: list[str]) -> None:
        try:
            self._inner.unsubscribe_quotes(contract_symbols)
        except BrokerAuthError as exc:
            self._mark_disconnected(exc)
            raise

    def place_order(self, request: OrderRequest) -> OrderResult:
        try:
            return self._inner.place_order(request)
        except BrokerAuthError as exc:
            self._mark_disconnected(exc)
            raise

    def modify_order(self, broker_order_id: str, **changes: object) -> OrderResult:
        try:
            return self._inner.modify_order(broker_order_id, **changes)
        except BrokerAuthError as exc:
            self._mark_disconnected(exc)
            raise

    def cancel_order(self, broker_order_id: str) -> OrderResult:
        try:
            return self._inner.cancel_order(broker_order_id)
        except BrokerAuthError as exc:
            self._mark_disconnected(exc)
            raise

    def get_order_status(self, broker_order_id: str) -> OrderResult:
        try:
            return self._inner.get_order_status(broker_order_id)
        except BrokerAuthError as exc:
            self._mark_disconnected(exc)
            raise

    def get_positions(self) -> list[Position]:
        try:
            return self._inner.get_positions()
        except BrokerAuthError as exc:
            self._mark_disconnected(exc)
            raise

    def get_margin(self) -> MarginInfo:
        try:
            return self._inner.get_margin()
        except BrokerAuthError as exc:
            self._mark_disconnected(exc)
            raise

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()


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


def _position_opened_live(position: PositionRow) -> bool:
    """True only if the position's own *opening* Order actually recorded
    `mode=LIVE` — never inferred from current session mode, which can
    change (kill_switch, degraded_mode) after the position was already
    opened for real.
    """
    db = object_session(position)
    if db is None:
        return False
    opening_order = db.get(OrderRow, position.opening_order_id)
    return opening_order is not None and opening_order.mode == OrderMode.LIVE


def _real_broker_or_raise(reason: str) -> BrokerPort:
    """The single, uniform gate every real-broker path in `get_execution_broker`
    routes through -- both checks apply regardless of *why* real execution
    was requested (a live-tier session mode, or closing a position that was
    genuinely opened live), so neither path can accidentally skip one.
    """
    if not get_settings().app.allow_real_money_dispatch:
        raise ConfigurationError(
            f"{reason}, but ALLOW_REAL_MONEY_DISPATCH is not set. Refusing to dispatch "
            "-- will not silently fall back to paper for a live-intent trade."
        )
    if not is_shoonya_configured():
        raise ConfigurationError(
            f"{reason}, but no real Shoonya broker is currently connected "
            "(is_shoonya_configured() is False)."
        )
    return get_broker()


def get_execution_broker(
    trading_session: TradingSession,
    strategy_run: StrategyRun | None = None,
    *,
    position: PositionRow | None = None,
) -> BrokerPort:
    """Broker resolution for anything that places or reads orders/positions
    for a specific session — `dispatch_trade_intent`, `close_position`,
    `PositionManager`, manual square-off/reconcile, and startup recovery.
    Deliberately separate from `get_broker()`, which stays for market-data
    call sites that legitimately want "whichever real broker is connected"
    with no session context.

    **Ops-Hardening Phase 5 routing, in priority order:**

    1. `position` given and it was actually opened LIVE (`Order.mode`, not
       current session mode) → always resolves the real broker, regardless
       of `trading_session.mode`. Closing genuinely-live risk is never
       blocked by `kill_switch`/`degraded_mode`/`reconciliation_lock` —
       those modes exist to stop *new* risk, not strand *existing* real
       positions with no path to close them. Still requires
       `allow_real_money_dispatch` (raises `ConfigurationError`, not a
       silent paper fallback, if it's off).
    2. `mode` in `(paper_only, degraded_mode, kill_switch,
       reconciliation_lock)` → mock, unconditionally, regardless of
       `strategy_run`.
    3. `mode == live_enabled`, or `mode == paper_plus_guarded_live` and
       `strategy_run`'s own `StrategyConfig.status == LIVE` (graduated) AND
       `runtime_mode != FORCE_PAPER` → real broker, gated on
       `allow_real_money_dispatch` (raises `ConfigurationError` rather than
       falling back to paper if it's off — a missing/false flag must never
       be silently read as "use paper instead," per explicit design intent).
       `runtime_mode.FORCE_PAPER` (Ops-Hardening Phase 1) is the tactical,
       same-day override this check completes -- Phase 1's own docstring
       named this exact gap ("a later phase's dispatch gating") and it had
       zero runtime effect anywhere until Phase 6 wired it in here.
    4. Otherwise (`paper_plus_guarded_live` with no `strategy_run`, or a
       not-yet-graduated one) → mock. Missing strategy-graduation
       information must never default *up* to real money.

    Real-adapter resolution never constructs a new `ShoonyaBrokerAdapter`
    here (this module never imports that class at all, by design — see
    the module's own docstring) — it reuses whatever `get_broker()`
    currently holds, install by `api.v1.shoonya.oauth_callback` after a
    real OAuth login, checked via `is_shoonya_configured()` for health.
    Lazy by construction: a paper-only session never even evaluates
    `is_shoonya_configured()`, let alone spins up a connection.
    """
    if position is not None and _position_opened_live(position):
        return _real_broker_or_raise(f"position {position.id} was opened live")

    mode = SafeMode(trading_session.mode)
    if mode in (
        SafeMode.PAPER_ONLY,
        SafeMode.DEGRADED_MODE,
        SafeMode.KILL_SWITCH,
        SafeMode.RECONCILIATION_LOCK,
    ):
        return _get_or_create_execution_mock()

    strategy_is_live = False
    if strategy_run is not None:
        db = object_session(strategy_run)
        if db is not None:
            config = db.get(StrategyConfig, strategy_run.strategy_config_id)
            strategy_is_live = (
                config is not None
                and config.status == StrategyStatus.LIVE
                and config.runtime_mode != StrategyRuntimeMode.FORCE_PAPER
            )

    if mode == SafeMode.LIVE_ENABLED or (
        mode == SafeMode.PAPER_PLUS_GUARDED_LIVE and strategy_is_live
    ):
        return _real_broker_or_raise(f"trading_session {trading_session.id} mode={mode.value}")

    return _get_or_create_execution_mock()


def is_execution_broker_live(broker: BrokerPort) -> bool:
    """Whether a broker `get_execution_broker` returned is the real
    adapter, not the persistent execution mock -- callers (`dispatch_trade
    _intent`, `close_position`) use this to tag `Order.mode` correctly
    rather than hardcoding `OrderMode.PAPER`. A plain `isinstance` against
    `MockBrokerAdapter` is enough: the mock is never wrapped in
    `_AuthAwareBroker` (only `set_broker` wraps real adapters), so this
    correctly reads real+wrapped as "live" with no unwrapping needed.
    """
    return not isinstance(broker, MockBrokerAdapter)


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

    Closes whatever was previously installed (duck-typed via `getattr(...,
    "close", None)`) before replacing it — a real `ShoonyaBrokerAdapter`
    holds a REST client and possibly a WS connection that would otherwise
    leak every time a user reconnects (or logs in from a second tab).
    `MockBrokerAdapter` has no `close()`, so this is always a no-op for the
    persistent execution-mock default. The new broker (if any) is wrapped in
    `_AuthAwareBroker` so a later auth failure through *any* call site
    updates `is_shoonya_configured()`, not just this call.
    """
    global _broker, _shoonya_connected
    previous = _broker
    if previous is not None:
        close = getattr(previous, "close", None)
        if callable(close):
            close()

    if broker is None:
        _broker = None
        _shoonya_connected = False
        return

    _broker = _AuthAwareBroker(broker)
    _shoonya_connected = True


def reset_for_tests() -> None:
    """Full reset of both slots — unlike `set_broker(None)` alone, this also
    clears `_execution_mock`, so a test that relies on `get_execution_broker`
    /`get_broker`'s lazy default gets a genuinely fresh `MockBrokerAdapter`
    with no leftover orders/positions from a previous test, matching what
    `set_broker(None)` alone already guaranteed before `get_execution_broker`
    existed. Test-only — production code never needs to forget the
    execution mock's state.
    """
    global _broker, _execution_mock, _shoonya_connected
    _broker = None
    _execution_mock = None
    _shoonya_connected = False


def is_shoonya_configured() -> bool:
    """Whether `get_broker()` currently resolves to a real, still-healthy
    Shoonya connection rather than the mock — the frontend needs this to
    decide whether to show "Connect Shoonya" or "Connected"/"Reconnect
    Shoonya". Backed by an explicit flag rather than `isinstance` so a
    `BrokerAuthError` caught by `_AuthAwareBroker` (from *any* call site,
    not just the OAuth callback) correctly flips this back to `False` —
    before this, the check only asked "was a real adapter ever installed,"
    which stayed `True` forever even after the session died.
    """
    return _shoonya_connected
