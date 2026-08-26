"""`MarketDataScheduler`: acts on the phase transitions `market_data.
market_hours.current_phase` defines — session start at 08:30 IST, a hard
stop at 16:00 IST (23:30 IST when `Settings.market_data.is_replay_mode` is
set — see `market_hours`'s own 2026-08-10 docstring section; this class
needs no changes of its own for that, since it calls `current_phase()` with
no override and inherits whichever cutoff is currently configured), and a
low-frequency pre-market health check in between.
Same background-thread shape as `scheduler.health_check.HealthCheckScheduler`
(daemon thread, a `stop_event` for clean shutdown, `run_once()` exposed
separately so tests can drive it deterministically) — a single process-wide
instance, not one per session, for the same reason: there is exactly one
live market-data provider per process (`provider_composition`'s own
singleton), not one per trading_session.

Operates on whichever provider `provider_composition.get_market_data_provider()`
currently resolves to — already wrapped in `MarketHoursGatedProvider` for
any real provider (`"mock"` never reaches this scheduler at all, see
`ensure_market_data_scheduler_running`'s own guard) — so `connect()`/
`disconnect()` calls issued here naturally pass the gate at the exact
moment they're issued (the phase has already flipped by the time a
transition is detected) without this class needing to know anything about
the gate itself.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from app.modules.market_data.market_hours import (
    ENV_METRIC_SYMBOLS,
    TRADABLE_UNDERLYINGS,
    MarketPhase,
    current_phase,
)
from app.modules.market_data.provider_composition import (
    get_market_data_provider,
    is_market_data_ready,
)

logger = logging.getLogger("app.market_data.scheduler")

# How often the loop wakes up to check for a phase transition or a
# pre-market health-check tick — short enough that the 08:30/09:00/16:00
# boundaries are noticed within a few seconds, cheap enough (a few time
# comparisons) to not matter at this cadence.
DEFAULT_TICK_SECONDS = 30.0

# Pre-market's own low-frequency health check, per the explicit 2026-08-06
# spec: confirm the session is still alive well before the market opens,
# without treating pre-market as "start ingesting real data" (nothing
# subscribes to symbols until a strategy actually starts scanning — this
# is a liveness check, not ingestion).
PRE_MARKET_HEALTH_CHECK_SECONDS = 300.0


class MarketDataScheduler:
    def __init__(self, tick_seconds: float = DEFAULT_TICK_SECONDS) -> None:
        self._tick_seconds = tick_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_phase: MarketPhase | None = None
        self._seconds_since_health_check = 0.0

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._tick_seconds + 5)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_once(self) -> None:
        phase = current_phase()
        if phase != self._last_phase:
            self._handle_transition(self._last_phase, phase)
            self._last_phase = phase
            self._seconds_since_health_check = 0.0

        if phase in (MarketPhase.PRE_MARKET, MarketPhase.ACTIVE_MARKET):
            # 2026-08-26: broadened from PRE_MARKET-only -- a symbol that
            # failed to subscribe (see `_subscribe_one`) previously had no
            # real retry path once the day reached ACTIVE_MARKET (the next
            # phase transition wouldn't happen again until CLOSED tomorrow).
            # This tick now also gives it a real, low-frequency retry
            # during active market hours -- see `_pre_market_health_check`.
            self._seconds_since_health_check += self._tick_seconds
            if self._seconds_since_health_check >= PRE_MARKET_HEALTH_CHECK_SECONDS:
                self._seconds_since_health_check = 0.0
                self._pre_market_health_check()

    def _handle_transition(self, from_phase: MarketPhase | None, to_phase: MarketPhase) -> None:
        logger.warning(
            "Market-data phase transition: %s -> %s",
            from_phase.value if from_phase is not None else "startup",
            to_phase.value,
        )
        provider = get_market_data_provider()
        if to_phase is MarketPhase.PRE_MARKET:
            # 2026-08-19: invalidate registry._subscribed_symbols' "already
            # subscribed" bookkeeping *before* tearing down the connection
            # it was tracking -- otherwise a symbol subscribed yesterday
            # reads as "already handled" against today's brand-new
            # connection below, and the real subscribe request never gets
            # re-sent. See `market_data.registry
            # .reset_subscriptions_for_new_day`'s own docstring for the
            # live incident this fixes.
            self._reset_subscriptions_for_new_day()
            # Fresh session for the day -- disconnect first so any state
            # left over from yesterday (a stale token, an open WS) is
            # actually torn down, not just reused.
            provider.disconnect()
            provider.connect()
            self._subscribe_known_underlyings_if_ready()
            self._reset_daily_indicators()
        elif to_phase is MarketPhase.ACTIVE_MARKET and from_phase is not MarketPhase.PRE_MARKET:
            # Any path into ACTIVE_MARKET that did NOT come through
            # PRE_MARKET's own connect+subscribe: a mid-day process
            # (re)start (from_phase is None -- this branch's original,
            # still-handled case), or a scheduler whose PRE_MARKET handling
            # never got recorded (from_phase is still CLOSED) -- confirmed
            # live 2026-08-26: a per-symbol subscribe failure during
            # PRE_MARKET used to leave `_last_phase` stuck at CLOSED (see
            # `_subscribe_known_underlyings`'s per-symbol try/except below,
            # which now prevents that), so the 09:00 IST transition landed
            # here with `from_phase=CLOSED` and matched no branch at all --
            # neither NIFTY nor BANKNIFTY got subscribed for the rest of
            # that day. The normal daily pre_market -> active_market
            # transition (from_phase is already PRE_MARKET) stays a genuine
            # no-op, unchanged.
            if from_phase is MarketPhase.CLOSED:
                # A real day boundary was skipped, not just a same-day
                # process restart -- run the same once-per-day resets
                # PRE_MARKET's own entry would have.
                self._reset_subscriptions_for_new_day()
                self._reset_daily_indicators()
            provider.connect()
            self._subscribe_known_underlyings_if_ready()
        elif to_phase is MarketPhase.CLOSED:
            provider.disconnect()

    def _reset_subscriptions_for_new_day(self) -> None:
        # Local import, same convention as _subscribe_known_underlyings/
        # _reset_daily_indicators below.
        from app.modules.market_data.registry import reset_subscriptions_for_new_day

        reset_subscriptions_for_new_day()

    def _subscribe_known_underlyings_if_ready(self) -> None:
        """2026-08-14: real live incident -- this used to call `ensure_
        ingestion_running` unconditionally, same bug class as
        `strategy_engine.recovery.resume_strategy_runners` (see that
        function's own docstring for the full mechanism). This scheduler
        runs on its own timer, independent of `resume_strategy_runners` and
        reconnect -- a restart during market hours reaches `ACTIVE_MARKET`/
        `from_phase is None` within one tick, `ensure_ingestion_running`
        starts a real background thread writing fabricated prices to
        `price_bars`/`quote_ticks`, and this kept happening every tick
        until a human reconnected, even *after* `resume_strategy_runners`'s
        own guard was fixed -- confirmed live: two garbage bars land in the
        minute between a restart and reconnect. `market_data.registry
        .reset_for_reconnect`'s `resume_ingestion_for_active_runs` call
        still closes the loop once a human reconnects, same as before.
        """
        if not is_market_data_ready():
            logger.warning(
                "Shoonya not connected yet — deferring market-data subscription for "
                "known underlyings until reconnect (see market_data.registry"
                ".reset_for_reconnect)."
            )
            return
        self._subscribe_known_underlyings()

    def _subscribe_known_underlyings(self) -> None:
        # Local import matches this codebase's existing convention for this
        # exact call (api/v1/strategies.py, main.py both import it locally)
        # -- market-data ingestion for NIFTY/BANKNIFTY should start the
        # moment the exchange session begins, independent of any strategy
        # being manually started. Idempotent per symbol
        # (registry._subscribed_symbols), so safe to call again if this
        # transition shape is ever observed twice.
        from app.modules.market_data.registry import ensure_ingestion_running

        for symbol in TRADABLE_UNDERLYINGS:
            self._subscribe_one(ensure_ingestion_running, symbol)
        # VIX/PCR environment-metrics feed (2026-08-19) -- same
        # provider-agnostic subscription path as the tradable underlyings
        # above, just a separate loop so this symbol never touches
        # TRADABLE_UNDERLYINGS itself (see ENV_METRIC_SYMBOLS's own
        # docstring for why that separation matters).
        for symbol in ENV_METRIC_SYMBOLS:
            self._subscribe_one(ensure_ingestion_running, symbol)

    @staticmethod
    def _subscribe_one(ensure_ingestion_running: Callable[[str], object], symbol: str) -> None:
        # 2026-08-26: real live incident -- this loop used to have no
        # per-symbol isolation at all. A single symbol's broker error (a
        # not-yet-cached token, an expired session) raised straight out of
        # this whole method, which propagated through `_handle_transition`
        # and left `run_once`'s `self._last_phase` update unreached (see
        # that method's own comment) -- so one bad symbol (confirmed live:
        # NIFTY during a session-expiry window, and separately INDIA VIX
        # after a restart) silently blocked every *other* symbol in both
        # loops from ever being subscribed, not just itself. Isolating each
        # symbol here means the rest of the day's known underlyings still
        # get a real subscribe attempt even if one is broken; the broken
        # one gets a fresh retry from the next phase transition or the
        # periodic health-check tick (see `_pre_market_health_check`).
        try:
            ensure_ingestion_running(symbol)
        except Exception:  # noqa: BLE001 - one symbol's failure must never block the rest
            logger.exception(
                "Market-data subscribe failed for %s -- continuing with the remaining "
                "known underlyings; this one retries at the next health-check tick or "
                "phase transition",
                symbol,
            )

    def _reset_daily_indicators(self) -> None:
        # Local import, same convention as _subscribe_known_underlyings
        # above -- VWAP is session-cumulative and must restart from zero
        # each trading day (see IndicatorEngine.reset_session's own
        # docstring); EMA is untouched by this, deliberately (trend
        # continuity across sessions is the whole point of an exponential
        # average).
        from app.modules.market_data.registry import reset_daily_indicators

        reset_daily_indicators()

    def _pre_market_health_check(self) -> None:
        """Fires every `PRE_MARKET_HEALTH_CHECK_SECONDS` during PRE_MARKET
        *and* ACTIVE_MARKET (see `run_once`) -- kept this name since it
        still runs on the same interval/mechanism, just with a wider
        trigger window. Also re-attempts `_subscribe_known_underlyings_if_
        ready` (not just `provider.connect()`), so a symbol that failed to
        subscribe earlier (see `_subscribe_one`'s own comment) gets a real
        retry every few minutes without waiting for the next phase
        transition or a full disconnect/reconnect.
        """
        logger.warning("Health check: confirming provider connection is alive")
        provider = get_market_data_provider()
        try:
            provider.connect()  # idempotent -- a no-op if already connected
        except Exception:  # noqa: BLE001 - a background loop must never die silently-crashed
            logger.exception("Health check: provider connect failed")
        self._subscribe_known_underlyings_if_ready()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - a background loop must never die silently-crashed
                logger.exception("market-data scheduler cycle failed")
            self._stop_event.wait(self._tick_seconds)


_scheduler: MarketDataScheduler | None = None


def ensure_market_data_scheduler_running(
    tick_seconds: float = DEFAULT_TICK_SECONDS,
) -> MarketDataScheduler | None:
    """`None`, not started, when `MARKET_DATA_PROVIDER` is `"mock"` — same
    "mock has no real session for a schedule to protect" reasoning as
    `provider_composition.get_market_data_provider`'s own gate exclusion.
    """
    from app.config.settings import get_settings

    if get_settings().market_data.provider == "mock":
        return None

    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = MarketDataScheduler(tick_seconds=tick_seconds)
        _scheduler.start()
    return _scheduler


def stop_market_data_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.stop()
        _scheduler = None
