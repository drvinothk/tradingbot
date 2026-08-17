"""Generalized strategy run-cycle + background-thread runner. Phase 2's
`SyntheticStrategyRunner` (removed) hardcoded this loop to one `Strategy`
subclass; Phase 4 needs the identical evaluate -> submit_signal -> refresh-
status cycle shared by all four strategies (synthetic, ORB, VWAP Pullback,
EMA Micro-pullback) — implemented once here instead of duplicated per
strategy, same "common rules" principle as `strategy_engine.common_rules`.

`StrategyRunner` mirrors `execution_engine.paper.position_manager
.PositionManager`'s shape exactly: a daemon thread, its own short-lived
`session_scope()` per cycle (never shared across threads), a `stop_event`
for clean shutdown. `run_cycle` is exposed standalone, not just via
`StrategyRunner._loop`, so tests can drive it deterministically without a
real background thread — same reasoning `PositionManager.run_once` is.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.core.clock import is_past_eod_scanning_stop, is_within_global_trading_window, now_ist
from app.core.db.session import session_scope
from app.core.sleep_inhibitor import get_sleep_inhibitor
from app.domain.audit.models import ActorType, EventCategory
from app.domain.market.models import Instrument, PriceBar
from app.domain.ops.models import AlertSeverity
from app.domain.risk.models import RiskDecision
from app.domain.session.models import TradingSession
from app.domain.strategy.models import StrategyConfig, StrategyRun, StrategyRunStatus
from app.modules.alerting.manager import send_alert
from app.modules.audit_service.service import record_event
from app.modules.broker_adapter.composition import get_broker
from app.modules.market_data.freshness import (
    FreshnessState,
    FreshnessThresholds,
    classify_age,
    ensure_fresh_option_chain,
)
from app.modules.market_data.market_hours import is_within_market_hours
from app.modules.market_data.provider_composition import is_shoonya_market_data_ready
from app.modules.market_data.registry import unsubscribe_symbol
from app.modules.strategy_engine.common_rules import (
    get_open_position_for_run,
    get_recent_completed_bars,
)
from app.modules.strategy_engine.interface import Strategy
from app.modules.strategy_engine.service import submit_signal

logger = logging.getLogger("app.strategy_engine.runner")

SessionFactory = Callable[[], AbstractContextManager[Session]]

# Ops-Hardening Phase 2. Patterned after market_data.freshness's own
# FreshnessThresholds convention (a new instance, not a reuse of
# TICK_THRESHOLDS -- that one's 60s "stale" boundary is for raw tick
# freshness, a different granularity than "how long since this run
# evaluated a bar"). 180s is the actual alert-firing boundary, matching the
# original spec's own ">3 minutes" condition exactly; 120s is a softer
# "approaching stale" boundary kept for shape-parity with the freshness
# module's own three-tier convention, not wired to any distinct behavior
# yet -- only STALE/DEAD trigger an alert below.
RUNNER_STALL_THRESHOLDS = FreshnessThresholds(
    degraded_after_seconds=120.0, stale_after_seconds=180.0
)

# Per strategy_run, not global -- a systemic outage affecting several
# concurrent runs still lets each one alert once, just not every cycle.
RUNNER_STALL_ALERT_THROTTLE_SECONDS = 300.0

_stall_alert_lock = threading.Lock()
_last_stall_alert_at: dict[uuid.UUID, datetime] = {}


def _check_runner_watchdog(
    strategy_run: StrategyRun,
    trading_session: TradingSession,
    latest_bar: PriceBar | None,
    has_open_position: bool,
    *,
    session_factory: SessionFactory = session_scope,
) -> None:
    """Alerts (SystemAlert + Telegram, via app.modules.alerting.manager) when
    a run with an open position hasn't evaluated a fresh bar in
    RUNNER_STALL_THRESHOLDS.stale_after_seconds -- a real signal this run's
    own data feed has gone stale while genuine risk is still on the table.
    Distinct from PositionManager's own stop/target/trail polling, which
    prices from the live tick feed directly (not bars) and already has its
    own broker-auth-error handling — this watchdog is specifically about
    this run's bar-evaluation pipeline, not the position's own P&L pricing.

    Gated on `is_within_market_hours()` (08:30-16:00 IST) rather than the
    narrower `is_within_global_trading_window()` (09:31-15:09) `run_cycle`'s
    own evaluate/submit block uses -- deliberate: an open position is
    actively managed (including EOD square-off) through `cutoff_time`
    (15:20 by default), well past the entry window's own close, so the
    watchdog needs to keep watching through that whole span, not just while
    new entries are allowed.

    Takes `latest_bar` directly (the same `PriceBar | None` `run_cycle`
    already fetched this cycle) rather than that function's own `window_ts`
    — `window_ts` falls back to wall-clock `now_ist()` when no bar exists
    yet, which would read as "perfectly fresh" for the one case (a position
    somehow open with zero bars ever recorded) that most needs an alert.
    `latest_bar is None` is therefore treated as maximally stale
    (`FreshnessState.DEAD`) outright, not silently skipped.

    Uses its own `session_factory` (default `session_scope`) for the alert
    write, deliberately *not* `run_cycle`'s own `db` -- an alert reporting a
    stalled feed must durably commit on its own, independent of whatever
    happens to the rest of that cycle's transaction afterward. `strategy_run`/
    `trading_session` are read-only here (`.id`/`.workspace_id`, plain
    already-loaded scalar columns, safe to read regardless of which session
    is live) — only the alert write itself goes through `session_factory`.
    Callers needing test isolation inject their own (`StrategyRunner`
    already threads its own constructor-injected `session_factory` through
    for exactly this reason — same "never let a background write default to
    the production DB inside a test" discipline as `ensure_ingestion_running`/
    `record_option_chain_snapshot`'s own `session_factory` parameters).
    """
    if not has_open_position or not is_within_market_hours():
        return

    now = datetime.now(UTC)
    state = (
        FreshnessState.DEAD
        if latest_bar is None
        else classify_age(latest_bar.bucket_start, now, RUNNER_STALL_THRESHOLDS)
    )
    if state not in (FreshnessState.STALE, FreshnessState.DEAD):
        return

    with _stall_alert_lock:
        last_sent = _last_stall_alert_at.get(strategy_run.id)
        if (
            last_sent is not None
            and (now - last_sent).total_seconds() < RUNNER_STALL_ALERT_THROTTLE_SECONDS
        ):
            return
        _last_stall_alert_at[strategy_run.id] = now

    age_desc = (
        "no bar ever recorded"
        if latest_bar is None
        else f"{(now - latest_bar.bucket_start).total_seconds():.0f}s since last evaluated bar"
    )
    logger.warning(
        "run %s: stalled feed watchdog firing -- open position, %s", strategy_run.id, age_desc
    )
    with session_factory() as alert_db:
        send_alert(
            alert_db,
            workspace_id=trading_session.workspace_id,
            severity=AlertSeverity.CRITICAL,
            category="strategy_run_stalled",
            message=f"strategy_run {strategy_run.id}: open position but stalled feed ({age_desc})",
            trading_session_id=trading_session.id,
        )


def _maybe_stop_for_eod(
    db: Session,
    strategy_run: StrategyRun,
    trading_session: TradingSession,
    instrument_id: uuid.UUID,
) -> None:
    """EOD-stop for scanning-only runs, 2026-08-17: a run with zero open
    positions has nothing left to protect once no new entry can fire
    anyway (`TRADE_WINDOW_END`, 15:09 IST) — left scanning past
    `EOD_SCANNING_STOP_TIME` (15:10 IST) it just burns a background thread
    and a market-data subscription for the rest of the day. Confirmed live
    twice: a zombie `SCANNING` run surviving a full session, and five more
    the same way days later.

    Only called from `run_cycle` when this cycle's own `has_position` is
    already known `False` — never re-derives it. A run *with* an open
    position is untouched here; `PositionManager`'s own stop/target/trail/
    EOD-square-off keeps managing it through `TradingSession.cutoff_time`
    (15:20 by default) regardless of this function.

    Reuses the existing `STOPPED` status (identical to a manual
    `stop_strategy` call) rather than a new terminal status — `STOPPED` is
    already the app-wide "terminal, excluded from `/strategies/running`,
    excluded from resume-on-restart" signal everywhere else, so this needs
    zero new consumer-side plumbing. Audited as a `SYSTEM` actor (no
    `actor_id`), distinct `event_type` (`strategy_run.eod_stopped`) from a
    human's `strategy_run.stopped` so the audit trail can still tell the
    two apart.

    Market-data unsubscription is reference-counted, not unconditional:
    `market_data.registry` subscribes by *underlying* symbol, one shared
    stream across every concurrent strategy run on that underlying (see
    that module's own docstring — "one shared thing, not one per caller").
    Unsubscribing unconditionally here would silently kill ticks for
    another still-active run on the same underlying (including one still
    IN_POSITION, whose `PositionManager` pricing depends on that live
    feed). Only unsubscribes when this is genuinely the last non-`STOPPED`
    run left on that instrument.
    """
    # now_ist(), not a raw datetime.now(UTC) -- this module-level name is
    # what test fixtures already monkeypatch to freeze wall-clock time for
    # run_cycle's own trade-window fallback (see e.g.
    # test_phase4_strategies_e2e.py's _fixed_trade_window_clock); using it
    # here too means this gate respects the exact same freeze instead of
    # silently reading real wall-clock time regardless of what a test
    # pretends the current moment is.
    if not is_past_eod_scanning_stop(now_ist()):
        return

    strategy_run.status = StrategyRunStatus.STOPPED
    strategy_run.stopped_at = datetime.now(UTC)
    db.add(strategy_run)
    db.flush()

    get_sleep_inhibitor().release(f"strategy_run:{strategy_run.id}")

    record_event(
        db,
        workspace_id=trading_session.workspace_id,
        actor_type=ActorType.SYSTEM,
        actor_id=None,
        event_category=EventCategory.STRATEGY_STATE_CHANGE,
        event_type="strategy_run.eod_stopped",
        entity_type="strategy_run",
        entity_id=strategy_run.id,
        trading_session_id=trading_session.id,
        strategy_config_id=strategy_run.strategy_config_id,
        payload={"reason": "zero open positions at EOD scanning cutoff (15:10 IST)"},
    )

    other_active_run = (
        db.query(StrategyRun.id)
        .filter(
            StrategyRun.instrument_id == instrument_id,
            StrategyRun.status != StrategyRunStatus.STOPPED,
            StrategyRun.id != strategy_run.id,
        )
        .first()
    )
    if other_active_run is None:
        instrument = db.get(Instrument, instrument_id)
        if instrument is not None:
            unsubscribe_symbol(instrument.symbol)

    logger.info("run %s: EOD scanning stop applied (zero open positions)", strategy_run.id)


def run_cycle(
    db: Session,
    strategy: Strategy,
    strategy_run: StrategyRun,
    trading_session: TradingSession,
    strategy_config: StrategyConfig,
    *,
    alert_session_factory: SessionFactory = session_scope,
) -> RiskDecision | None:
    """One `evaluate` -> `submit_signal` cycle, plus a ground-truth refresh
    of `strategy_run.status` (IN_POSITION vs SCANNING) computed from whether
    this run actually has an open Position right now — correct regardless of
    which path a trade took this cycle (auto-dispatch, approval-required,
    risk-rejected, or no signal at all), unlike trying to track the
    transition manually. Never touches a `STOPPED` run's status.

    Before `evaluate()`, ensures the option-chain snapshot this strategy
    ranks against isn't stale (`market_data.freshness.ensure_fresh_option_
    chain` refreshes it once if needed — the actual fix for the "only ever
    snapshotted once, at start_strategy time" gap). `evaluate()` is skipped
    entirely (same as a normal "no signal" cycle) if data is still STALE/DEAD
    after that refresh attempt — a broker hiccup should mean "wait for next
    cycle," never "trade off data we can't vouch for."

    The freshness-check + `evaluate()` + `submit_signal()` block only runs
    inside `is_within_global_trading_window()` (09:31-15:09 IST) — outside
    it, this is a normal "no signal" cycle too, but the status-refresh below
    still always runs so an already-open position's status stays
    ground-truth.

    The window gate keys off the latest completed bar's own `bucket_start`,
    not wall-clock `now()` — fetched once, here, and threaded through to
    `evaluate()` so a bar-consuming strategy doesn't re-query the identical
    row a moment later. Falls back to wall-clock `now_ist()` when no bar
    exists yet for this instrument (a strategy that doesn't consume bars,
    e.g. `SyntheticStrategy`, or an instrument before its first bar
    completes) — that fallback also doubles as this function's empty-bar
    guard, so a cold-start cycle degrades to the old wall-clock behavior
    instead of ever touching `latest_bar.bucket_start` on a `None`.

    2026-08-14: also requires `is_shoonya_market_data_ready()` alongside the
    window check — `get_broker()` (used for the option-chain refresh right
    below) falls back to the mock broker until a human reconnects Shoonya,
    same root cause `market_data.registry.reset_for_reconnect`'s docstring
    documents for market-data ingestion. Without this, a strategy could
    evaluate and dispatch paper trades against a fully fabricated option
    chain during that window with no error anywhere to notice by.

    Ops-Hardening Phase 2: also calls `_check_runner_watchdog` (this
    module), which alerts if a run with an open position hasn't evaluated a
    fresh bar in a while — see that function's own docstring for the full
    reasoning, including why it uses a wider market-hours window than the
    trade-entry gate above. `alert_session_factory` is forwarded to it
    unchanged (default `session_scope`) — deliberately not reusing this
    function's own `db` parameter, since a stalled-feed alert must commit
    independently of whatever this cycle's own transaction does afterward.

    2026-08-17: also calls `_maybe_stop_for_eod` whenever this cycle finds
    zero open positions — self-stops a still-SCANNING run past 15:10 IST
    (see that function's own docstring). A run with an open position is
    left alone here; `PositionManager` keeps managing it independently.
    """
    decision: RiskDecision | None = None

    @contextmanager
    def _same_session():
        # Reuses run_cycle's own already-open db/transaction for any
        # option-chain refresh, rather than record_option_chain_snapshot's
        # own default of opening a second, independently-committing
        # connection — keeps the refresh atomic with the rest of this cycle.
        yield db

    latest_bars = get_recent_completed_bars(db, strategy.instrument_id, limit=1)
    if latest_bars:
        latest_bar = latest_bars[0]
        window_ts = latest_bar.bucket_start
    else:
        latest_bar = None
        window_ts = now_ist()

    if is_within_global_trading_window(window_ts) and is_shoonya_market_data_ready():
        freshness = ensure_fresh_option_chain(
            db,
            get_broker(),
            strategy.instrument_id,
            strategy.expiry_date,
            session_factory=_same_session,
        )
        if freshness in (FreshnessState.STALE, FreshnessState.DEAD):
            logger.warning(
                "run %s: option-chain data %s for instrument %s expiry %s — skipping cycle",
                strategy_run.id,
                freshness.value,
                strategy.instrument_id,
                strategy.expiry_date,
            )
            proposal = None
        else:
            proposal = strategy.evaluate(db, strategy_run, latest_bar)

        if proposal is not None:
            decision = submit_signal(db, strategy_run, trading_session, strategy_config, proposal)

    if strategy_run.status != StrategyRunStatus.STOPPED:
        has_position = get_open_position_for_run(db, strategy_run) is not None
        new_status = StrategyRunStatus.IN_POSITION if has_position else StrategyRunStatus.SCANNING
        if strategy_run.status != new_status:
            strategy_run.status = new_status
            db.add(strategy_run)
            db.flush()

        _check_runner_watchdog(
            strategy_run,
            trading_session,
            latest_bar,
            has_position,
            session_factory=alert_session_factory,
        )

        if not has_position:
            _maybe_stop_for_eod(db, strategy_run, trading_session, strategy.instrument_id)

    return decision


class StrategyRunner:
    def __init__(
        self,
        strategy: Strategy,
        strategy_run_id: uuid.UUID,
        interval_seconds: float = 30.0,
        session_factory: SessionFactory = session_scope,
        on_self_stop: Callable[[], object] | None = None,
    ) -> None:
        self._strategy = strategy
        self._strategy_run_id = strategy_run_id
        self._interval_seconds = interval_seconds
        self._session_factory = session_factory
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Called (at most once, from the loop's own thread) when this
        # runner notices its own strategy_run has gone STOPPED without a
        # `stop()` call from outside -- today, only `_maybe_stop_for_eod`
        # (this module) does that. `stop_strategy` (api.v1.strategies) still
        # pops `_RUNNERS` itself before calling `stop()`, so this firing
        # too on a manual stop is a harmless no-op double-pop, not a
        # required path for that case.
        self._on_self_stop = on_self_stop

    @property
    def instrument_id(self) -> uuid.UUID:
        return self._strategy.instrument_id

    @property
    def expiry_date(self) -> date:
        return self._strategy.expiry_date

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_seconds + 5)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                with self._session_factory() as db:
                    strategy_run = db.get(StrategyRun, self._strategy_run_id)
                    if strategy_run is None or strategy_run.status == StrategyRunStatus.STOPPED:
                        if self._on_self_stop is not None:
                            self._on_self_stop()
                        return
                    trading_session = db.get(TradingSession, strategy_run.trading_session_id)
                    strategy_config = db.get(StrategyConfig, strategy_run.strategy_config_id)
                    if trading_session is None or strategy_config is None:
                        logger.warning(
                            "strategy_run %s references a missing session/config — stopping",
                            self._strategy_run_id,
                        )
                        return
                    run_cycle(
                        db,
                        self._strategy,
                        strategy_run,
                        trading_session,
                        strategy_config,
                        alert_session_factory=self._session_factory,
                    )
            except Exception:  # noqa: BLE001 - a background loop must never die silently-crashed
                logger.exception("strategy cycle failed for run %s", self._strategy_run_id)
            self._stop_event.wait(self._interval_seconds)
