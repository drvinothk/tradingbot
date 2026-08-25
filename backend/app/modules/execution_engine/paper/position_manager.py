"""`PositionManager`: a background-thread poller, same pattern as
`strategy_engine.runner.StrategyRunner` (daemon
thread, its own short-lived `session_scope()` per cycle, a `stop_event` for
clean shutdown). One instance per `trading_session`, started by
`api.v1.strategies.start_strategy` (mirroring exactly where
`StrategyRunner` itself gets started) and resumed by `app.main`'s
startup-recovery check after a restart — its job for the life of the
session is: check every open Position's stop/target/trail against the
current price, force a square-off once IST wall-clock passes
`cutoff_time`, watch for a margin breach on guarded-live/live sessions
(Addendum hardening batch's narrow emergency-square-off trigger), and
periodically run a polling reconciliation pass.

**Price source**: stop/target/trail checks read live price from
`market_data.provider_composition.get_market_data_provider()` first (works
reliably for underlyings, unreliable-to-nonexistent for individual option
contracts in this deployment — see `_live_tick`). For **underlyings**,
falling back to `broker.get_quote()` (the execution broker, always the mock
today) is still fine, since the live feed rarely misses there. For
**option contracts** specifically, `_live_tick`/`broker.get_quote()` is
*not* used as the fallback — see `execution_engine.paper.service
.current_contract_price`, which falls back to the same REST-based
`OptionChainSnapshot` the strategy itself proposed the trade from, since
`broker.get_quote()` would otherwise price the position from the mock's
own synthetic, strategy-independent seed (the exact price-source mismatch
this module's history had before this fix).
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.clock import now_ist
from app.core.db.session import session_scope
from app.core.modes.state_machine import ModeTransitionError, transition_mode
from app.domain.audit.models import ActorType, EventCategory
from app.domain.broker.models import ReconciliationTrigger
from app.domain.execution.models import OrderMode, Position, PositionStatus
from app.domain.market.models import Instrument, OptionContract
from app.domain.ops.models import AlertSeverity
from app.domain.session.models import (
    SafeMode,
    TradingSession,
    TradingSessionStatus,
    TransitionTriggerType,
)
from app.modules.alerting.manager import send_alert
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.base.contracts import Tick
from app.modules.broker_adapter.base.errors import BrokerAuthError, BrokerError
from app.modules.broker_adapter.composition import get_execution_broker
from app.modules.execution_engine.paper.service import (
    current_contract_price,
    evaluate_open_position,
    reconcile_pending_live_exit_orders,
    reconcile_pending_live_orders,
    resolve_broker_for_position,
)
from app.modules.market_data.freshness import TICK_THRESHOLDS, FreshnessState, classify_age
from app.modules.market_data.provider_composition import get_market_data_provider
from app.modules.market_data.providers.base import BaseMarketDataProvider
from app.modules.reconciliation.service import run_full_reconciliation

logger = logging.getLogger("app.execution_engine.paper.position_manager")

_MARGIN_BREACH_MODES = (SafeMode.PAPER_PLUS_GUARDED_LIVE, SafeMode.LIVE_ENABLED)

SessionFactory = Callable[[], AbstractContextManager[Session]]

DEFAULT_POLL_INTERVAL_SECONDS = 3.0
DEFAULT_RECONCILE_EVERY_N_CYCLES = 5

# 2026-08-20: ~30s at the default 3s poll interval -- the unconditional REST
# safety-net cadence for `reconcile_pending_live_orders`'s pending-LIVE-order
# check, deliberately flat/not WS-health-gated (see that function's own
# docstring for why). The fast, free, WS-push-cache check still runs every
# cycle regardless; only the REST fallback is throttled to this cadence.
DEFAULT_ORDER_POLL_EVERY_N_CYCLES = 10


class PositionManager:
    def __init__(
        self,
        trading_session_id: uuid.UUID,
        broker: BrokerPort | None = None,
        market_data_provider: BaseMarketDataProvider | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        reconcile_every_n_cycles: int = DEFAULT_RECONCILE_EVERY_N_CYCLES,
        order_poll_every_n_cycles: int = DEFAULT_ORDER_POLL_EVERY_N_CYCLES,
        session_factory: SessionFactory = session_scope,
    ) -> None:
        self.trading_session_id = trading_session_id
        # Not resolved eagerly: __init__ only has a trading_session_id, not
        # the TradingSession row get_execution_broker needs, and this class
        # is often constructed before a DB session is open (registry.py).
        # None means "resolve get_execution_broker(trading_session) fresh
        # every cycle in _run_cycle"; an explicit broker (tests, always)
        # is used as-is, unchanged from before. `broker` is still used here
        # — for order placement (EOD/margin-breach square-off) and as the
        # staleness fallback for live pricing (see _live_tick) — even though
        # position pricing itself now reads from market_data_provider first.
        self._broker_override = broker
        # Same optional-override shape as _broker_override, for the same
        # reasons (tests construct this class directly, before any DB
        # session/composition-root state exists).
        self._market_data_provider_override = market_data_provider
        self._poll_interval_seconds = poll_interval_seconds
        self._reconcile_every_n_cycles = reconcile_every_n_cycles
        self._order_poll_every_n_cycles = order_poll_every_n_cycles
        self._session_factory = session_factory
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._cycle_count = 0
        # Symbols this instance has successfully subscribed on the market-
        # data provider for pricing (see _ensure_symbol_subscribed) — a
        # symbol that fails to subscribe is *not* added, so it's retried on
        # the next cycle rather than silently given up on forever.
        self._subscribed_symbols: set[str] = set()

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval_seconds + 5)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_once(self) -> None:
        """One poll cycle, exposed separately from `_loop` so tests can
        drive it deterministically rather than sleeping on the background
        thread — same reasoning `SyntheticStrategy.run_cycle` is tested
        directly, independent of `StrategyRunner`'s timer.
        """
        with self._session_factory() as db:
            trading_session = db.get(TradingSession, self.trading_session_id)
            if trading_session is None or trading_session.status != TradingSessionStatus.ACTIVE:
                return

            try:
                self._run_cycle(db, trading_session)
            except BrokerAuthError as exc:
                self._handle_broker_auth_error(db, trading_session, exc)

    def _handle_broker_auth_error(
        self, db: Session, trading_session: TradingSession, exc: BrokerAuthError
    ) -> None:
        """Broker-agnostic reaction to Phase 5's "invalid credentials / IP
        mismatch / mid-session expiry" scenarios — catches the generic
        `BrokerAuthError` (see `broker_adapter/base/errors.py`'s own
        docstring for why this module never imports anything
        Shoonya-specific).

        Only actually moves the session to `degraded_mode` when the
        transitions table (`core/modes/transitions.py`) has a legal
        `SYSTEM`-triggered edge there from the current mode — which is
        `paper_plus_guarded_live`/`live_enabled` only. `degraded_mode`
        exists to protect *live* money; a `paper_only` session (all of
        Phase 5's real traffic, since it's still no-live-orders) has
        nothing for it to protect, so the state machine deliberately has
        no edge there at all. Logging is the entire response in that case
        — same "market data hiccup, not a safety event" reasoning this
        system already applies to a `MockBrokerAdapter`-only run.
        """
        logger.warning(
            "broker auth failure for session %s: %s", self.trading_session_id, exc
        )
        from_mode = SafeMode(trading_session.mode)
        if from_mode not in (SafeMode.PAPER_PLUS_GUARDED_LIVE, SafeMode.LIVE_ENABLED):
            return

        # 2026-08-25: this path previously only logged -- a live/guarded
        # session losing its broker connection got zero notification
        # anywhere outside the log file. Alerted (and committed
        # immediately, not left riding on the transition below) so the
        # alert survives even if the transition_mode call that follows
        # raises ModeTransitionError.
        send_alert(
            db,
            workspace_id=trading_session.workspace_id,
            trading_session_id=trading_session.id,
            severity=AlertSeverity.CRITICAL,
            category="broker_disconnected",
            message=f"Broker auth failure for session {self.trading_session_id}: {exc}"[:500],
            mode=OrderMode.LIVE,
            dedup_key=f"broker_disconnected:{trading_session.id}",
        )
        db.commit()
        try:
            transition_mode(
                db,
                trading_session,
                SafeMode.DEGRADED_MODE,
                TransitionTriggerType.SYSTEM,
                reason=f"broker auth failure: {exc}"[:500],
            )
            db.commit()
        except ModeTransitionError:
            logger.exception(
                "could not move session %s to degraded_mode after broker auth failure",
                self.trading_session_id,
            )

    def _check_margin_breach(
        self,
        db: Session,
        trading_session: TradingSession,
        broker: BrokerPort,
        market_data_provider: BaseMarketDataProvider,
    ) -> None:
        """Addendum hardening batch's one narrow automatic emergency-square-
        off trigger (build-plan.md: "a detected margin breach on a live
        position — not on connectivity loss, reconciliation lag, or any
        other transient condition"). Only called for
        `paper_plus_guarded_live`/`live_enabled` sessions with open
        positions (see `_run_cycle`'s guard) — kill-switch is deliberately
        untouched by this path; a manual "exit all" endpoint and the
        existing EOD square-off cover the other two legs of that same
        Addendum decision.
        """
        try:
            margin = broker.get_margin()
        except BrokerError:
            logger.exception(
                "get_margin failed during margin-breach check for session %s",
                self.trading_session_id,
            )
            return

        if margin.available_margin >= 0:
            return

        logger.warning(
            "margin breach detected for session %s: available_margin=%.2f",
            self.trading_session_id,
            margin.available_margin,
        )

        # Local import: same load-time-cycle reasoning as
        # run_eod_square_off's import in _run_cycle below.
        from app.modules.scheduler.eod_square_off import run_margin_breach_square_off

        # self._broker_override, not `broker` (the account-wide broker this
        # method used above for get_margin()) -- an account-wide margin
        # breach must still close each position via *its own* strategy's
        # correct broker, not force every position through whichever broker
        # happened to report the breach. See resolve_broker_for_position's
        # own docstring.
        run_margin_breach_square_off(
            db, self._broker_override, trading_session, market_data_provider=market_data_provider
        )

        reason = f"margin breach: available_margin={margin.available_margin:.2f}"[:500]
        send_alert(
            db,
            workspace_id=trading_session.workspace_id,
            trading_session_id=trading_session.id,
            severity=AlertSeverity.CRITICAL,
            category="margin_breach_square_off",
            message=reason,
            # Only ever called for paper_plus_guarded_live/live_enabled
            # sessions (see this method's own docstring) -- margin itself
            # is a real-broker-account concept, never a paper simulation.
            mode=OrderMode.LIVE,
            dedup_key=f"margin_breach_square_off:{trading_session.id}",
        )
        record_event(
            db,
            workspace_id=trading_session.workspace_id,
            actor_type=ActorType.SYSTEM,
            event_category=EventCategory.SYSTEM_HEALTH,
            event_type="margin_breach_square_off",
            entity_type="trading_session",
            entity_id=trading_session.id,
            trading_session_id=trading_session.id,
            payload={"available_margin": margin.available_margin},
        )
        db.commit()

    def _ensure_symbol_subscribed(self, provider: BaseMarketDataProvider, symbol: str) -> None:
        """Subscribes directly on `provider` — deliberately *not* via
        `market_data.registry.ensure_ingestion_running`/
        `MarketDataIngestionService`, whose default `session_factory` is the
        production `session_scope`. A QC pass on an earlier version of this
        change found that calling that registry function from paper
        execution (`_open_position_from_fill`) spawned real background
        streaming threads against the *real* dev database from ordinary
        test runs — thousands of stray `quote_ticks` rows, since dozens of
        existing tests call `dispatch_trade_intent` directly with no reason
        to expect it to touch market data at all. A provider's own
        `subscribe_ticks` only updates its in-memory latest-tick cache
        (see `BaseMarketDataProvider.get_latest_tick`'s own docstring) —
        no DB session anywhere in this call, so a test overriding
        `market_data_provider` (or accepting the safe "mock" default) stays
        fully isolated regardless.

        Idempotent per instance (`_subscribed_symbols`) and best-effort: a
        symbol that fails to subscribe is *not* recorded, so it's retried on
        the next cycle rather than silently given up on forever — the
        `_live_tick` fallback to `broker.get_quote` covers pricing in the
        meantime.
        """
        if symbol in self._subscribed_symbols:
            return
        try:
            provider.subscribe_ticks([symbol], on_tick=lambda _tick: None)
            self._subscribed_symbols.add(symbol)
        except Exception:
            logger.exception(
                "Failed to subscribe %s for live pricing; will retry next cycle", symbol
            )

    def _live_tick(
        self, provider: BaseMarketDataProvider, symbol: str, broker: BrokerPort
    ) -> Tick:
        """Prices from the live market-data feed (Angel One in production) —
        the "full decoupling" this system's stop/target/trail checks needed,
        since Shoonya's own feed is the fragile one. Falls back to a
        one-shot `broker.get_quote()` (Shoonya) for this cycle only when the
        feed hasn't delivered anything fresh, reusing the same
        LIVE/DEGRADED/STALE/DEAD classification `market_data.freshness`
        already applies to persisted ticks — never leaves a stop-loss check
        silently unevaluated just because the primary feed hiccuped for one
        cycle, same "wrap a possibly-imperfect primitive in our own
        supervision" discipline `market_data.ingestion`'s own WS -> REST
        fallback already established.
        """
        self._ensure_symbol_subscribed(provider, symbol)
        tick = provider.get_latest_tick(symbol)
        if tick is not None:
            state = classify_age(tick.ts, datetime.now(UTC), TICK_THRESHOLDS)
            if state in (FreshnessState.LIVE, FreshnessState.DEGRADED):
                return tick
            logger.warning(
                "live tick for %s is %s (age-based); falling back to broker.get_quote "
                "for this cycle",
                symbol,
                state.value,
            )
        else:
            logger.warning(
                "no live tick yet for %s; falling back to broker.get_quote for this cycle",
                symbol,
            )
        return broker.get_quote(symbol)

    def _run_cycle(self, db: Session, trading_session: TradingSession) -> None:
        market_data_provider = self._market_data_provider_override or get_market_data_provider()
        open_positions = (
            db.query(Position)
            .filter(
                Position.trading_session_id == trading_session.id,
                Position.status == PositionStatus.OPEN,
            )
            .all()
        )
        # Cache underlying quotes within this cycle — multiple open
        # positions (Phase 4: several concurrent strategy runs) commonly
        # share the same underlying instrument, and this only ever needs
        # its current price once per cycle regardless of how many
        # positions reference it. Keyed by (instrument_id, broker identity)
        # rather than instrument_id alone -- since 2026-08-19, different
        # positions in the same cycle can resolve different brokers (one
        # force_paper, one genuinely live), and the mock's synthetic price
        # is not interchangeable with a real broker's quote.
        underlying_price_cache: dict[tuple[uuid.UUID, int], float] = {}

        @contextmanager
        def _same_session() -> Iterator[Session]:
            # Reuses this cycle's already-open db/transaction for any
            # option-chain refresh current_contract_price triggers, same
            # reasoning as strategy_engine.runner.run_cycle's own
            # _same_session — keeps the refresh atomic with the rest of
            # this cycle rather than opening a second, independently-
            # committing connection.
            yield db

        for position in open_positions:
            try:
                # Resolved per-position, not once for the whole cycle -- see
                # resolve_broker_for_position's own docstring for why a
                # shared cycle-wide broker is unsafe once different open
                # positions can belong to differently-configured strategies.
                broker = self._broker_override or resolve_broker_for_position(
                    db, trading_session, position
                )
                option_contract = db.get(OptionContract, position.option_contract_id)
                if option_contract is None:
                    continue
                # Still subscribe (idempotent, tracked in self._subscribed_symbols)
                # even though this deployment's per-contract WS never actually
                # delivers anything today — current_contract_price only *reads*
                # the provider's cache, it doesn't subscribe, so without this the
                # "try live tick first" branch could never succeed even after a
                # future WS-for-contracts fix landed.
                self._ensure_symbol_subscribed(market_data_provider, option_contract.symbol)
                # Option-contract pricing: prefers a live WS tick (works today
                # for free if a future per-contract WS fix lands), otherwise
                # falls back to the same REST OptionChainSnapshot the strategy
                # itself proposed this trade from — not broker.get_quote()
                # directly, which (get_execution_broker() always being the
                # mock) would price this contract from the mock's own
                # synthetic, strategy-independent seed. See
                # current_contract_price's own docstring.
                tick = current_contract_price(
                    db,
                    option_contract,
                    broker,
                    market_data_provider=market_data_provider,
                    session_factory=_same_session,
                )

                cache_key = (option_contract.instrument_id, id(broker))
                underlying_price = underlying_price_cache.get(cache_key)
                if underlying_price is None:
                    instrument = db.get(Instrument, option_contract.instrument_id)
                    if instrument is not None:
                        # Underlyings are unaffected by this change -- WS
                        # ticks for NIFTY/BANKNIFTY are reliable, unlike
                        # per-contract ticks, so this stays on the existing
                        # live-feed -> broker.get_quote fallback.
                        underlying_price = self._live_tick(
                            market_data_provider, instrument.symbol, broker
                        ).ltp
                        underlying_price_cache[cache_key] = underlying_price

                evaluate_open_position(
                    db,
                    trading_session,
                    position,
                    tick.ltp,
                    broker=broker,
                    bid=tick.bid,
                    ask=tick.ask,
                    underlying_price=underlying_price,
                )
            except BrokerAuthError:
                # Must propagate to run_once's own except BrokerAuthError,
                # not be swallowed here -- that's what moves a guarded-live/
                # live session to degraded_mode (_handle_broker_auth_error).
                # A per-position try/except that caught this too would
                # silently defeat that existing safety response for every
                # position after the first one to hit it.
                raise
            except Exception:  # noqa: BLE001 - one position's failure must
                # never block every other open position's stop/target/trail
                # check in the same cycle, nor the reconciliation/EOD-
                # square-off steps later in _run_cycle. Real, live incident
                # 2026-08-25: right after a restart, Shoonya isn't
                # reconnected yet, so resolve_broker_for_position's
                # `_real_broker_or_raise` raises `ConfigurationError` for a
                # genuinely-live position -- uncaught (this loop had zero
                # try/except before this fix, already flagged as a known
                # gap in this file's own 2026-08-22 QC docstring but never
                # closed), that crashed the *entire* cycle every ~3s,
                # identical in shape to the same day's
                # reconcile_pending_live_orders incident. The broker-side
                # resting protective stop, once placed, stays live at
                # Shoonya independent of this loop running at all, so a
                # skipped cycle here degrades local monitoring/TSL only,
                # never the capital-protection floor itself.
                logger.exception(
                    "evaluating open position %s failed -- skipping it this cycle, "
                    "continuing with the rest",
                    position.id,
                )
                continue

        if open_positions and SafeMode(trading_session.mode) in _MARGIN_BREACH_MODES:
            # Session-level/account-wide broker -- used only here (margin-
            # breach detection), never for evaluating or closing any
            # *specific* position -- see resolve_broker_for_position's own
            # docstring for the 2026-08-19 incident that split fixed.
            # Resolved lazily, right where it's used, and inside its own
            # try/except -- real, live incident 2026-08-25: this used to
            # resolve unconditionally at the top of _run_cycle, before the
            # open_positions loop even ran (let alone this mode/positions
            # guard) -- so right after a restart, with Shoonya not yet
            # reconnected, `get_execution_broker`'s `_real_broker_or_raise`
            # raised `ConfigurationError` on *every* cycle before the loop's
            # own now-fixed try/except ever got a chance to run, crash-
            # looping `PositionManager` regardless of that fix. A skipped
            # margin-breach check for one cycle is an acceptable, bounded
            # degradation (existing open positions' own resting protective
            # stops stay live at the broker either way); silently never
            # running the rest of this cycle every ~3s was not.
            try:
                session_broker = self._broker_override or get_execution_broker(trading_session)
            except Exception:  # noqa: BLE001 - see the per-position loop's identical reasoning
                logger.exception(
                    "resolving the account-wide broker for margin-breach detection failed "
                    "-- skipping that check this cycle",
                )
            else:
                self._check_margin_breach(db, trading_session, session_broker, market_data_provider)

        # Local import: same load-time-cycle reasoning as
        # run_eod_square_off's import just below —
        # strategy_engine.service imports execution_engine.paper.service,
        # so importing it at module scope here would cycle back through
        # this package's own __init__.py.
        from app.modules.strategy_engine.service import expire_stale_pending_approvals

        expire_stale_pending_approvals(db, trading_session)

        # 2026-08-20: catches a LIVE entry order that didn't resolve
        # synchronously at dispatch (a LIMIT order sitting pending at the
        # broker, filled or rejected later) -- see
        # reconcile_pending_live_orders's own docstring for the live
        # incident this closes. The WS-push cache check runs every cycle
        # (free); only the REST fallback is throttled to
        # _order_poll_every_n_cycles, deliberately on a flat cadence rather
        # than gated on any WS-health signal.
        order_poll_fallback = self._cycle_count % self._order_poll_every_n_cycles == 0
        reconcile_pending_live_orders(db, trading_session, allow_rest_fallback=order_poll_fallback)
        reconcile_pending_live_exit_orders(
            db, trading_session, allow_rest_fallback=order_poll_fallback
        )

        if now_ist().time() >= trading_session.cutoff_time:
            # Local import: app.modules.scheduler's package __init__
            # eagerly re-exports eod_square_off, which itself imports
            # execution_engine.paper.service — importing it at module
            # scope here would form a load-time cycle through this
            # package's own __init__.py. Same one-directional-dependency
            # reasoning as close_position's local import of
            # risk_engine.service.
            from app.modules.scheduler.eod_square_off import run_eod_square_off

            # self._broker_override, not the position loop's `broker` --
            # that variable is scoped inside the loop (and undefined if
            # open_positions was empty) and, since 2026-08-19, resolves
            # per-position rather than once for the cycle. Passing None in
            # production lets run_eod_square_off resolve each position's
            # own broker itself, the same way; the override is preserved
            # for tests exactly as before.
            run_eod_square_off(
                db,
                self._broker_override,
                trading_session,
                market_data_provider=market_data_provider,
            )

        self._cycle_count += 1
        if self._cycle_count % self._reconcile_every_n_cycles == 0:
            # run_full_reconciliation, not a single session_broker call --
            # a session can hold both paper and live positions at once
            # (per-strategy graduation), and session_broker (resolved with
            # no strategy_run) would only ever be the real broker when the
            # *whole* session is live_enabled. See that function's own
            # docstring.
            run_full_reconciliation(db, trading_session, ReconciliationTrigger.POLL)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - a background loop must never die silently-crashed
                logger.exception(
                    "position manager cycle failed for session %s", self.trading_session_id
                )
            self._stop_event.wait(self._poll_interval_seconds)
