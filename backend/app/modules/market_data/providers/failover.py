"""`FailoverMarketDataProvider` — wraps a primary and a backup
`BaseMarketDataProvider` and routes ticks/reads through whichever is
currently healthy. Composed at `provider_composition.py`'s composition
root, the same level `MarketHoursGatedProvider` already wraps at, and
following that class's exact "delegate every method to an inner provider,
add cross-cutting behavior" shape — this is not a rewrite of any concrete
provider (Shoonya, Angel One), just a third thing sitting between them and
`MarketDataIngestionService`/`PositionManager`.

An externally-drafted proposal for this feature assumed an asyncio-native
system (`asyncio.Queue`, `asyncio.create_task` watchdog) — rejected, since
this codebase's market-data/broker core is deliberately synchronous and
threading-based throughout (see `providers/base.py`'s own docstring). This
class uses a daemon `threading.Thread` running `run_once()` on a fixed poll
interval, matching `market_data_scheduler.MarketDataScheduler`'s and
`scheduler.health_check.HealthCheckScheduler`'s own shape, including
exposing `run_once()` separately so tests can drive it deterministically.
The one real difference from those: ticks arrive asynchronously from a
background WS thread, not synchronously with the poll loop, so unlike
`MarketDataScheduler`'s pure simulated-time accumulator this needs an actual
time source to correlate "when did the last primary tick land" against
"how long has it been since" — hence the injectable `clock` (real
`time.monotonic` in production, a controllable fake in tests) rather than
accumulating elapsed time purely from `run_once()` calls.

**Health is tracked as a single scalar, not per-symbol.** Per this
project's own documented invariant that a broker connection is one shared
stream, not one per instrument (`market_data/registry.py`'s own docstring),
a real primary-provider failure drops every subscribed symbol at once — so
"any tick from primary, on any symbol, within the last N seconds" is the
whole signal. This is a different concern from
`MarketDataIngestionService`'s own per-symbol WS-health-grace fallback
(which exists for "did the very first tick ever arrive for this one
symbol", not "did an established stream go silent").

**Dual-subscribe, lazy-backup.** Primary is subscribed immediately and
stays subscribed even while backup is active, so its recovery can be
observed continuously (required for anti-flap). Backup is subscribed only
on the first real failover trip — holding a live backup connection open at
all times would mean an unconditional login on every process start for a
feed this project has already flagged as fragile (Angel One's proxy
dependency, rate limits), for zero benefit while primary is healthy. Once
recovered, backup is explicitly unsubscribed again.

Only the active leg's ticks are ever forwarded to the caller's `on_tick` —
the inactive leg's ticks still update its own health timestamp (needed to
detect backup failing) but are dropped, since two feeds writing the same
`price_bars` bucket would violate `uq_price_bar_bucket`.

**Readiness-gated trip (2026-08-25).** `_ensure_backup_subscribed` checks
`backup.is_ready()` (`BaseMarketDataProvider.is_ready`'s own docstring)
*before* ever calling `subscribe_ticks()` — added for the Shoonya-primary/
Alice-Blue-backup configuration, where backup's auth is a one-time human
browser login with no backend-triggerable retry. Without this, every trip
attempt against a disconnected Alice Blue would burn a doomed
`subscribe_ticks()` call before failing the exact same way; with it, an
unready backup is never even attempted — primary simply stays the active
leg (still unhealthy, still being retried every watchdog cycle for its own
recovery) until a human connects the backup, rather than "tripping" to a
leg that can't actually take over.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime

from app.core.db.session import SessionFactory
from app.domain.ops.models import AlertSeverity
from app.domain.session.models import TradingSession, TradingSessionStatus
from app.modules.alerting.manager import send_alert
from app.modules.broker_adapter.base.contracts import DepthSnapshot, PriceCandle, Tick
from app.modules.market_data.providers.base import BaseMarketDataProvider

logger = logging.getLogger("app.market_data.failover")

TickCallback = Callable[[Tick], None]
DepthCallback = Callable[[DepthSnapshot], None]

DEFAULT_POLL_INTERVAL_SECONDS = 1.0


class FailoverMarketDataProvider(BaseMarketDataProvider):
    def __init__(
        self,
        primary: BaseMarketDataProvider,
        backup: BaseMarketDataProvider,
        *,
        primary_name: str,
        backup_name: str,
        failover_threshold_seconds: float,
        recovery_stabilization_seconds: float,
        backup_retry_seconds: float,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        alert_session_factory: SessionFactory | None = None,
    ) -> None:
        self._primary = primary
        self._backup = backup
        self._primary_name = primary_name
        self._backup_name = backup_name
        self._failover_threshold_seconds = failover_threshold_seconds
        self._recovery_stabilization_seconds = recovery_stabilization_seconds
        self._backup_retry_seconds = backup_retry_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock
        # 2026-08-25: opt-in only, defaulting to None -- this class has zero
        # DB/session context otherwise, and it's directly instantiated by
        # several existing tests with no session_factory of any kind. A
        # hardcoded default here would repeat the exact "background write
        # defaults to the production DB inside a test" trap CLAUDE.md
        # already documents for PositionManager/StrategyRunner (fixed the
        # same way there: constructor-injected, defaulting to None/a
        # no-DB-touching stub, never a bare `session_scope` default).
        # provider_composition.py's own production composition root is the
        # one real call site that passes the actual `session_scope`.
        self._alert_session_factory = alert_session_factory

        self._lock = threading.Lock()
        self._active: str = primary_name
        self._subscribed_at: float | None = None
        self._last_primary_tick_at: float | None = None
        self._recovery_started_at: float | None = None
        self._backup_subscribed = False
        self._next_backup_attempt_at: float | None = None
        # Ops-Hardening Phase 4: a manual override on top of everything
        # above, not a replacement for it -- see set_manual_override's own
        # docstring.
        self._manual_override: str | None = None

        self._symbols: set[str] = set()
        # Per-symbol, not a single shared slot -- both the primary and
        # backup tick handlers dispatch through this same dict regardless
        # of which leg is active, keyed by symbol rather than one shared
        # attribute. A single slot (overwritten on every subscribe_ticks
        # call) meant PositionManager subscribing an option contract on
        # this same shared provider would silently overwrite
        # MarketDataIngestionService's own underlying callback, breaking
        # quote_ticks/price_bars persistence for everything, system-wide,
        # the moment any position opened -- live-confirmed 2026-08-13, same
        # root cause and fix as broker_port_shim.py/angel_one.py.
        #
        # **2026-08-17: real gap in that fix, found and fixed.** The
        # 2026-08-13 writeup assumed "the two real callers subscribe
        # disjoint symbol sets" (ingestion on underlyings, PositionManager
        # on option contracts) -- true for options, false for the
        # underlying itself: PositionManager._ensure_symbol_subscribed also
        # subscribes the *underlying* (for its own live-price read on that
        # position, via a no-op callback), which collides on the exact same
        # symbol MarketDataIngestionService already registered its real
        # persistence callback for. Per-symbol keying alone doesn't stop a
        # second *caller* from clobbering the first caller's callback on a
        # symbol they both legitimately subscribe to. Live-confirmed via a
        # temporary raw-frame diagnostic (now removed): quote_ticks for
        # NIFTY stopped incrementing at the exact millisecond
        # PositionManager's own "SUBSCRIBE sent: keys='NSE|26000'" fired,
        # while the real WS frames kept arriving on the wire uninterrupted
        # (proving this was a client-side callback-registration bug, not a
        # broker-side subscription drop). Fixed by making registration
        # first-registrant-wins (`setdefault` instead of unconditional
        # assignment) -- safe because every real caller in this codebase
        # always subscribes a given symbol with the exact same callback on
        # every call, so "keep whichever callback got here first" never
        # actually discards a caller's *own* intended callback, only a
        # different caller's redundant later subscribe.
        self._on_tick_by_symbol: dict[str, TickCallback] = {}
        self._on_depth_by_symbol: dict[str, DepthCallback] = {}

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _alert(
        self, *, category: str, severity: AlertSeverity, message: str, dedup_suffix: str
    ) -> None:
        """No-op when `alert_session_factory` wasn't provided (every direct
        test instantiation, by default). Alerts every currently-ACTIVE
        workspace, same "no per-instrument/session context available here"
        shape `scheduler.health_check.HealthCheckScheduler` already uses —
        this is an underlying-agnostic, account-wide feed event, not tied to
        one trading_session. `mode` is left at `send_alert`'s own `None`
        default (infra-level, never paper-suppressed, same reasoning as
        `health_check_failed`/`market_data_stale`).

        `dedup_suffix`: distinguishes the two real call sites of this method
        (a successful switch to backup vs. backup itself failing to
        subscribe) so a "both feeds down" alert can never suppress a later,
        genuinely different "switched to backup" success message within the
        same 15-minute dedup window, or vice versa.
        """
        if self._alert_session_factory is None:
            return
        try:
            with self._alert_session_factory() as db:
                workspace_ids = {
                    s.workspace_id
                    for s in db.query(TradingSession)
                    .filter(TradingSession.status == TradingSessionStatus.ACTIVE)
                    .all()
                }
                for workspace_id in workspace_ids:
                    # dedup_key includes workspace_id -- a shared key here
                    # would mean only the first workspace in this loop ever
                    # actually pushes, silently swallowing every other
                    # workspace's own genuinely separate alert.
                    send_alert(
                        db,
                        workspace_id=workspace_id,
                        severity=severity,
                        category=category,
                        message=message,
                        dedup_key=(
                            f"{category}:{self._primary_name}:{self._backup_name}:"
                            f"{dedup_suffix}:{workspace_id}"
                        ),
                    )
                db.commit()
        except Exception:  # noqa: BLE001 - an alerting failure must never break failover itself
            logger.exception("failed to raise market-data failover alert")

    @property
    def active_provider_name(self) -> str:
        with self._lock:
            return self._active

    @property
    def manual_override(self) -> str | None:
        with self._lock:
            return self._manual_override

    def set_manual_override(self, provider_name: str | None) -> None:
        """Ops-Hardening Phase 4. A user-driven override on top of this
        class's own automatic health-based switching (`run_once`'s
        `_check_primary_health`/`_check_recovery` dispatch) -- not a
        replacement for it. `provider_name=None` clears the override and
        resumes normal automatic behavior from whichever leg is currently
        active (deliberately *not* snapped back to primary on clear -- if
        the override left backup active, clearing it correctly falls
        through to `_check_recovery`'s own stabilization-window logic next
        cycle, the same health-confirmed path an automatic failover's
        recovery already goes through, rather than yanking back to primary
        with no health check at all).

        Forcing to the backup leg subscribes it immediately if it isn't
        already (reusing `_ensure_backup_subscribed`) -- a forced switch
        must actually be live now, not deferred to whenever the automatic
        watchdog would have gotten around to it. Raises `RuntimeError` if
        that subscribe fails *or the backup isn't ready* (2026-08-25 --
        `_ensure_backup_subscribed`'s own `is_ready()` gate applies here
        too, e.g. forcing to a disconnected Alice Blue), rather than
        silently marking the backup "active" while it's actually not
        receiving anything -- unlike the automatic failover path (which
        just retries next cycle, appropriate for an unattended background
        loop), this is a synchronous, user-initiated call and the caller
        (the PATCH endpoint) needs to surface the failure immediately, not
        have it discovered later as a silent data gap.

        While an override is set, `run_once` skips its own automatic
        switching entirely (health tracking in `_make_tick_handler` is
        unaffected either way -- `_last_primary_tick_at` keeps updating
        regardless of `_active`, so recovery health data isn't stale by the
        time the override is cleared).
        """
        if provider_name is not None and provider_name not in (
            self._primary_name,
            self._backup_name,
        ):
            raise ValueError(
                f"manual override {provider_name!r} must be one of "
                f"{(self._primary_name, self._backup_name)!r}"
            )

        if provider_name == self._backup_name and not self._ensure_backup_subscribed(
            self._clock()
        ):
            raise RuntimeError(
                f"Failed to subscribe backup provider {self._backup_name!r} for the "
                "manual override -- not applied."
            )

        with self._lock:
            if provider_name is not None:
                self._active = provider_name
            self._manual_override = provider_name
        logger.warning("Market-data provider manual override set to %r", provider_name)

    def replace_backup(self, new_backup: BaseMarketDataProvider) -> None:
        """2026-08-20: lets a Shoonya reconnect refresh just this leg's
        possibly-stale reference (`BrokerPortMarketDataAdapter` captures
        `get_broker()` once at construction, never re-fetches it — see that
        class's own docstring) without disturbing a healthy primary
        connection at all. Deliberately narrower than
        `provider_composition.reset_for_reconnect`'s full-pipeline rebuild,
        which tears down and reconstructs this *entire*
        `FailoverMarketDataProvider` (primary included) — appropriate when
        Shoonya itself is primary, but would mean every Shoonya reconnect
        also briefly interrupts a perfectly healthy TrueData/Angel One
        primary for no reason when Shoonya is only the backup. This method
        only ever touches `self._backup`.

        Safe regardless of whether backup is currently the active leg: if
        it's dormant (the common case — primary healthy, backup never
        subscribed), this just swaps the reference; the next real failover
        subscribes the new instance fresh, correctly, with no special
        handling needed. If backup is *already* active (a real primary
        outage is in progress right now), this disconnects the old backup
        and immediately resubscribes the new one with the same
        symbols/handlers, so an in-progress failover doesn't go dark for
        however long the swap takes.
        """
        with self._lock:
            was_subscribed = self._backup_subscribed
            old_backup = self._backup
            symbols = sorted(self._symbols)

        if was_subscribed:
            try:
                old_backup.disconnect()
            except Exception:
                logger.exception(
                    "Failed to disconnect the old backup provider %r before replacing it "
                    "-- continuing anyway, its ticks are simply dropped once replaced",
                    self._backup_name,
                )

        with self._lock:
            self._backup = new_backup

        if was_subscribed:
            try:
                new_backup.subscribe_ticks(
                    symbols,
                    self._make_tick_handler(self._backup_name),
                    self._make_depth_handler(self._backup_name),
                )
            except Exception:
                logger.critical(
                    "New backup provider %r failed to subscribe after being refreshed for "
                    "a Shoonya reconnect -- both market-data feeds may now be unavailable",
                    self._backup_name,
                    exc_info=True,
                )
                with self._lock:
                    self._backup_subscribed = False
                return

        logger.warning("Failover backup provider %r reference refreshed", self._backup_name)

    # -- BaseMarketDataProvider -------------------------------------------

    def connect(self) -> None:
        self._primary.connect()

    def disconnect(self) -> None:
        self._stop_watchdog()
        self._primary.disconnect()
        if self._backup_subscribed:
            self._backup.disconnect()
        # Disconnect is a full teardown -- start clean on whatever the next
        # subscribe_ticks() call turns out to be, rather than risking a
        # resume with active/backup_subscribed left pointing at a backup
        # leg that was just torn down and won't otherwise get resubscribed
        # (a plain subscribe_ticks() call only ever (re)subscribes primary).
        with self._lock:
            self._active = self._primary_name
            self._subscribed_at = None
            self._recovery_started_at = None
        self._backup_subscribed = False

    def subscribe_ticks(
        self,
        symbols: list[str],
        on_tick: TickCallback,
        on_depth: DepthCallback | None = None,
    ) -> None:
        # Additive across calls, not a replacement -- MarketDataIngestionService
        # calls this once per *new* underlying (registry.ensure_ingestion_running
        # subscribes one symbol at a time), so a second strategy's subscribe
        # must not drop the first strategy's symbol from what a later
        # failover subscribes on backup.
        new_symbols = [s for s in symbols if s not in self._symbols]
        self._symbols |= set(symbols)
        with self._lock:
            for symbol in symbols:
                # First registrant wins -- see 2026-08-17 incident note in
                # this class's own docstring. PositionManager subscribing
                # the *underlying* (for its own live-price read, via a
                # no-op callback) after MarketDataIngestionService already
                # registered the real persistence callback for that same
                # symbol must not clobber it.
                self._on_tick_by_symbol.setdefault(symbol, on_tick)
                if on_depth is not None:
                    self._on_depth_by_symbol.setdefault(symbol, on_depth)
            if self._subscribed_at is None:
                self._subscribed_at = self._clock()
        self._primary.subscribe_ticks(
            symbols,
            self._make_tick_handler(self._primary_name),
            self._make_depth_handler(self._primary_name) if on_depth is not None else None,
        )
        if self._backup_subscribed and new_symbols:
            self._backup.subscribe_ticks(
                new_symbols,
                self._make_tick_handler(self._backup_name),
                self._make_depth_handler(self._backup_name) if on_depth is not None else None,
            )
        self._start_watchdog()

    def unsubscribe_ticks(self, symbols: list[str]) -> None:
        self._primary.unsubscribe_ticks(symbols)
        if self._backup_subscribed:
            self._backup.unsubscribe_ticks(symbols)
        self._symbols -= set(symbols)
        with self._lock:
            for symbol in symbols:
                self._on_tick_by_symbol.pop(symbol, None)
                self._on_depth_by_symbol.pop(symbol, None)

    def get_latest_tick(self, symbol: str) -> Tick | None:
        active = self.active_provider_name
        if active == self._primary_name:
            return self._primary.get_latest_tick(symbol)
        return self._backup.get_latest_tick(symbol)

    def get_price_history(
        self, underlying: str, start: datetime, end: datetime, timeframe_seconds: int = 60
    ) -> list[PriceCandle]:
        active = self.active_provider_name
        if active == self._primary_name:
            return self._primary.get_price_history(underlying, start, end, timeframe_seconds)
        return self._backup.get_price_history(underlying, start, end, timeframe_seconds)

    def close(self) -> None:
        """Real, live bug fixed here 2026-08-14: this used to skip
        `disconnect()` entirely and only probe both legs for an *optional*
        `close()` — which `BrokerPortMarketDataAdapter` (Shoonya) has never
        implemented, so `getattr(..., "close", None)` silently found
        nothing and the primary leg's actual WS subscription was never torn
        down. `disconnect()` is the one `BaseMarketDataProvider` method
        every provider is guaranteed to implement (an `@abstractmethod`),
        so it's the correct unconditional teardown call; the optional
        `close()` probe still runs afterward for provider-specific extra
        cleanup `disconnect()` doesn't otherwise cover (e.g. Angel One's own
        REST client) — same two-step shape `AngelOneMarketDataProvider.close`
        already gets right.
        """
        self.disconnect()
        close = getattr(self._primary, "close", None)
        if callable(close):
            close()
        close = getattr(self._backup, "close", None)
        if callable(close):
            close()

    # -- tick routing -------------------------------------------------------

    def _make_tick_handler(self, source_name: str) -> TickCallback:
        def _handle(tick: Tick) -> None:
            callback = None
            with self._lock:
                if source_name == self._primary_name:
                    self._last_primary_tick_at = self._clock()
                if self._active == source_name:
                    callback = self._on_tick_by_symbol.get(tick.contract_symbol)
            if callback is not None:
                callback(tick)

        return _handle

    def _make_depth_handler(self, source_name: str) -> DepthCallback:
        def _handle(depth: DepthSnapshot) -> None:
            callback = None
            with self._lock:
                if self._active == source_name:
                    callback = self._on_depth_by_symbol.get(depth.contract_symbol)
            if callback is not None:
                callback(depth)

        return _handle

    # -- watchdog -------------------------------------------------------

    def _start_watchdog(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _stop_watchdog(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval_seconds + 5)
            if self._thread.is_alive():
                # The join timed out -- most plausibly the watchdog is
                # currently blocked inside _ensure_backup_subscribed's
                # network call to the backup provider (the same
                # connectivity issue that likely triggered this teardown in
                # the first place). Proceeding regardless (matching every
                # other call site's "cleanup is never blocked" discipline)
                # would previously leave this fact completely invisible --
                # the caller just moves on as if teardown fully succeeded,
                # while the old OS thread keeps running indefinitely as a
                # daemon, discovered only via a live py-spy dump. Logging
                # loudly here is the whole fix for that half of the
                # 2026-08-14 incident; the thread itself is still a daemon
                # so it can't outlive the process, but it can very much
                # outlive this provider instance being discarded.
                logger.error(
                    "Failover watchdog thread did not stop within %.1fs -- likely "
                    "blocked in a backup-provider network call. Proceeding with "
                    "teardown anyway; this thread may keep running as an orphan "
                    "until it unblocks on its own.",
                    self._poll_interval_seconds + 5,
                )
            self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - a background loop must never die silently-crashed
                logger.exception("failover watchdog cycle failed")
            self._stop_event.wait(self._poll_interval_seconds)

    def run_once(self) -> None:
        now = self._clock()
        with self._lock:
            active = self._active
            overridden = self._manual_override is not None

        # A manual override suspends this class's own automatic switching
        # entirely -- see set_manual_override's own docstring for why.
        # Health tracking (_make_tick_handler updating _last_primary_tick_at)
        # is unaffected, since it doesn't route through run_once at all.
        if overridden:
            return

        if active == self._primary_name:
            self._check_primary_health(now)
        else:
            self._check_recovery(now)

    def _check_primary_health(self, now: float) -> None:
        with self._lock:
            last = self._last_primary_tick_at
            subscribed_at = self._subscribed_at
        if last is not None:
            healthy = (now - last) <= self._failover_threshold_seconds
        else:
            # No primary tick has ever landed yet -- e.g. the first watchdog
            # cycle can fire well before a real WS tick could possibly have
            # arrived. Treat this as healthy until failover_threshold_seconds
            # have elapsed since subscribing, matching
            # MarketDataIngestionService's own WS-health-grace reasoning.
            healthy = subscribed_at is not None and (
                now - subscribed_at
            ) <= self._failover_threshold_seconds
        if healthy:
            return

        if not self._ensure_backup_subscribed(now):
            return
        with self._lock:
            self._active = self._backup_name
            self._recovery_started_at = None
        message = (
            f"No tick from {self._primary_name!r} for > {self._failover_threshold_seconds:.0f}s "
            f"— switched active market-data provider to {self._backup_name!r}."
        )
        logger.warning("FAILOVER: %s", message)
        # 2026-08-31: WARNING, not CRITICAL -- a successful automatic failover
        # is the system self-healing exactly as designed, not something that
        # needs a human to act on. Still written to system_alerts (audit
        # trail intact) and still logged; it just no longer pages via
        # Telegram/Attention Required (both gate on CRITICAL). A real "no
        # feed at all" outage is still covered -- see "backup_not_ready"/
        # "both_down" below (stay CRITICAL) and
        # HealthCheckScheduler._check_market_data_staleness, which alerts
        # CRITICAL if neither leg has produced a usable tick/bar for 5+
        # minutes, regardless of which provider is "active".
        self._alert(
            category="market_data_failover_switch",
            severity=AlertSeverity.WARNING,
            message=message,
            dedup_suffix="switched",
        )

    def _ensure_backup_subscribed(self, now: float) -> bool:
        if self._backup_subscribed:
            return True
        with self._lock:
            next_attempt = self._next_backup_attempt_at
        if next_attempt is not None and now < next_attempt:
            return False

        if not self._backup.is_ready():
            # 2026-08-25: gate added specifically for Shoonya-primary/
            # Alice-Blue-backup -- Alice Blue's auth is a one-time human
            # browser login with no backend-triggerable retry (see
            # AliceBlueMarketDataProvider.is_ready's own docstring), so
            # attempting subscribe_ticks() here would always fail the exact
            # same way subscribe_error handling below already does, just
            # after paying for a doomed call. Checking first means an
            # unconnected backup never even attempts the call, and the primary
            # keeps being retried every watchdog cycle for its own recovery
            # rather than this leg being marked "tripped" to a backup that
            # can't actually take over -- i.e. keep waiting for the primary
            # to recover on its own, exactly as if no failover were
            # configured at all, until a human connects the backup.
            logger.warning(
                "Failover backup provider %r is not ready (no live session) -- "
                "%r stays the active leg despite being unhealthy; rechecking "
                "readiness in %.0fs",
                self._backup_name,
                self._primary_name,
                self._backup_retry_seconds,
            )
            self._alert(
                category="market_data_failover_switch",
                severity=AlertSeverity.CRITICAL,
                message=(
                    f"{self._primary_name!r} is unhealthy and backup "
                    f"{self._backup_name!r} isn't connected — no automatic failover "
                    f"is possible until it is. Connect {self._backup_name!r} "
                    "(Market Terminal) to enable it."
                ),
                dedup_suffix="backup_not_ready",
            )
            with self._lock:
                self._next_backup_attempt_at = now + self._backup_retry_seconds
            return False

        try:
            self._backup.subscribe_ticks(
                sorted(self._symbols),
                self._make_tick_handler(self._backup_name),
                self._make_depth_handler(self._backup_name),
            )
        except Exception:
            logger.critical(
                "Failover backup provider %r failed to subscribe — both market-data "
                "feeds are now unavailable; retrying in %.0fs",
                self._backup_name,
                self._backup_retry_seconds,
                exc_info=True,
            )
            self._alert(
                category="market_data_failover_switch",
                severity=AlertSeverity.CRITICAL,
                message=(
                    f"Backup provider {self._backup_name!r} failed to subscribe — both "
                    f"market-data feeds are now unavailable; retrying in "
                    f"{self._backup_retry_seconds:.0f}s."
                ),
                dedup_suffix="both_down",
            )
            with self._lock:
                self._next_backup_attempt_at = now + self._backup_retry_seconds
            return False
        self._backup_subscribed = True
        return True

    def _check_recovery(self, now: float) -> None:
        with self._lock:
            last = self._last_primary_tick_at
        primary_healthy = last is not None and (now - last) <= self._failover_threshold_seconds

        with self._lock:
            if not primary_healthy:
                if self._recovery_started_at is not None:
                    logger.warning(
                        "Failover recovery: %r dropped during the anti-flap stabilization "
                        "window — resetting recovery timer",
                        self._primary_name,
                    )
                self._recovery_started_at = None
                return

            if self._recovery_started_at is None:
                self._recovery_started_at = now
                logger.info(
                    "Failover recovery: %r is back online — starting %.0fs "
                    "stabilization window before switching back",
                    self._primary_name,
                    self._recovery_stabilization_seconds,
                )
                return

            if (now - self._recovery_started_at) < self._recovery_stabilization_seconds:
                return

            self._active = self._primary_name
            self._recovery_started_at = None

        recovery_message = (
            f"{self._primary_name!r} stable for {self._recovery_stabilization_seconds:.0f}s "
            f"— switching active market-data provider back to {self._primary_name!r}."
        )
        logger.warning("FAILOVER RECOVERY: %s", recovery_message)
        # WARNING, not CRITICAL -- good news (the earlier disconnection
        # resolved itself), stays DB-only per send_alert's own
        # CRITICAL-only Telegram gate; the outage itself already pushed via
        # the "switched"/"both_down" alerts above.
        self._alert(
            category="market_data_failover_switch",
            severity=AlertSeverity.WARNING,
            message=recovery_message,
            dedup_suffix="recovered",
        )
        try:
            self._backup.unsubscribe_ticks(sorted(self._symbols))
        except Exception:
            logger.exception(
                "Failed to unsubscribe backup provider %r after recovery — continuing "
                "anyway, its ticks are simply dropped since it's no longer active",
                self._backup_name,
            )
        self._backup_subscribed = False
