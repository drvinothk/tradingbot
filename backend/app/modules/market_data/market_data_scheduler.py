"""`MarketDataScheduler`: acts on the phase transitions `market_data.
market_hours.current_phase` defines — session start at 08:30 IST, a hard
stop at 16:00 IST, and a low-frequency pre-market health check in between.
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

from app.modules.market_data.market_hours import MarketPhase, current_phase
from app.modules.market_data.provider_composition import get_market_data_provider

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

        if phase is MarketPhase.PRE_MARKET:
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
            # Fresh session for the day -- disconnect first so any state
            # left over from yesterday (a stale token, an open WS) is
            # actually torn down, not just reused.
            provider.disconnect()
            provider.connect()
        elif to_phase is MarketPhase.CLOSED:
            provider.disconnect()

    def _pre_market_health_check(self) -> None:
        logger.warning("Pre-market health check: confirming provider connection is alive")
        provider = get_market_data_provider()
        try:
            provider.connect()  # idempotent -- a no-op if already connected
        except Exception:  # noqa: BLE001 - a background loop must never die silently-crashed
            logger.exception("Pre-market health check failed")

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
