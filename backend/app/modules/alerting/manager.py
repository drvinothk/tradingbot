"""Ops-Hardening Phase 2. Centralized alert dispatch — every alert is a
dual-write: a `SystemAlert` DB row (the same shape `HealthCheckScheduler`
already writes) plus a best-effort Telegram notification. The DB write is
the source of truth (already readable via `GET /system-alerts`, per the
existing recovery-panel batch); Telegram is a convenience notification
layered on top, never the only record of an alert.

Threading model matches every other background worker in this codebase
(`HealthCheckScheduler`, `MarketDataScheduler`, `PositionManager`,
`StrategyRunner`, `FailoverMarketDataProvider`): plain `threading`, no
asyncio anywhere. `send_alert` is called synchronously from whichever
background thread raised the alert (e.g. `StrategyRunner`'s own loop) —
the Telegram HTTP call is a plain, bounded-timeout `httpx.post`, not
offloaded to a second thread, since these calls are already off any hot
path (once per stalled cycle, not per tick) and a hard 3s timeout bounds
the worst case precisely.

**2026-08-25: Telegram push gating, added after a real noise complaint.**
Before this, every `send_alert` call unconditionally attempted a Telegram
push — including the 5 `auto_spawn_*` categories, which can each fire once
per misconfigured strategy_config every single morning (a one-time config
fix, not something needing a phone interrupt). `SystemAlert` rows are
still written unconditionally for every category/severity — this section
only gates whether the *Telegram* push additionally happens, per an
explicit user classification pass. Five independent conditions must all
hold, checked in `_should_push_to_telegram`:

1. **Category allowlist** (`TELEGRAM_ALLOWED_CATEGORIES`) — an allowlist,
   not a severity threshold, since some CRITICAL categories (auto_spawn_*)
   are routine and some WARNING/INFO instances of an otherwise-allowed
   category (`health_check_failed`'s NTP-drift case) shouldn't push either
   — folded into the severity check below rather than a second list.
2. **Severity must be CRITICAL** — combined with (1), this is what lets
   `health_check_failed` push only for its disk-failure (CRITICAL) case,
   not its NTP-drift (WARNING) case, without a third list.
3. **`mode` must not be `OrderMode.PAPER`** — explicit user instruction:
   "no notification for paper trade at all". `mode=None` (the default) is
   for alerts with no specific paper/live position or order behind them at
   all (health checks, instrument-master sync) — infrastructure-level, not
   suppressed by this rule. One explicit exception: a caller may pass
   `override_paper_mode_suppression=True` to push a paper-mode alert
   anyway (reconciliation does this for a paper-book mismatch on a
   live-active session — a broken paper book is then a system-health
   signal the user must still see). All other conditions (1, 2, 4, 5)
   still apply.
4. **Time window (09:00-15:30 IST)** — a live position genuinely at risk
   off-hours isn't reachable this system can't already act on: the broker
   itself force-squares-off, per explicit user reasoning ("the position
   can't be stuck off-market hours, if the app is unable to square off,
   broker will do it").
5. **Dedup**: at most one push per `(category, dedup_key)` per
   `_DEDUP_COOLDOWN_SECONDS` (15 min, per explicit user instruction) — a
   still-unresolved issue re-notifies after the cooldown; a *different*
   position/order hitting the same category is a different `dedup_key` and
   pushes immediately, never suppressed as if it were the same issue.
   In-memory only (module-level dict + lock, same shape as
   `strategy_engine.runner`'s own per-run stall-alert throttle predating
   this) — resets on a backend restart, which is acceptable for the same
   reason that one already is.

The SystemAlert DB write always happens regardless of any of the above —
this section can only suppress the Telegram push, never the durable record.

**2026-09-03: self-healing grace window, added after a real "is this just
FYI or does it need me" complaint.** `protective_stop_cancel_unresolved`,
`exit_order_unfilled`, and `reconciliation_mismatch` are raised on an
*ambiguous* intermediate state (Shoonya's real Cancel/Place acks are often
status-less; a reconciliation pass sampling mid-fill) — not a confirmed,
durable failure. `PositionManager`'s own 3-second poll cycle (plus each
broker adapter's own follow-up status check) resolves the large majority of
these within about a second, with zero human action ever involved. Live
evidence from 2026-09-03: two real live positions each hit this pair of
alerts once and closed correctly about a second later — the alert fired
before the system had even had one full retry cycle to resolve itself.

`_SELF_HEALING_GRACE_CATEGORIES` (checked in `_should_push_to_telegram`,
same ordering discipline as condition 4 — before dedup, so a suppressed
candidate never consumes a dedup slot) holds a push until the *same*
`dedup_key` has been continuously observed for `_SELF_HEALING_GRACE_SECONDS`
(10s, ~3 poll cycles — enough margin over the ~1s typical resolution time
without being so long it delays a genuinely stuck case). Tracked via its own
small in-memory dict (`_first_seen_by_key`), deliberately independent of the
`SystemAlert` row/its own dedup_key lookup — no schema dependency, and the
grace timer survives regardless of whether/how the row itself is written.
Same reset-on-restart acceptance as condition 5's dedup dict.

Deliberately excludes `protective_stop_cancel_failed`/
`protective_stop_placement_failed` (a real exception, not an ambiguous
ack — never observed to self-heal) and `exit_order_attempts_exhausted` (the
actual "retries exhausted, needs you now" terminal signal, which must never
be delayed). The mode-machine's own reaction to a genuine live-book
`reconciliation_mismatch` (`transition_mode` to `RECONCILIATION_LOCK`) is
unaffected by this — that call happens in `reconciliation/service.py`
independently of whether this alert ever reaches Telegram, and is
separately audited via `audit_events` regardless.

This only ever holds back the *Telegram push and the Control Room Attention
card* (`ControlRoomPage.tsx`'s `AttentionCard` applies the identical
grace window client-side, computed from each alert's own `created_at` — kept
in sync with this list by comment, same as the existing
`TELEGRAM_ALLOWED_CATEGORIES`/`ATTENTION_ALERT_CATEGORIES` pair already are).
The `SystemAlert` row is unaffected — written immediately regardless, and
remains fully visible without any grace delay on the Advanced page's "System
errors" card (`GET /system-alerts?is_resolved=false`, no category or age
filter) the whole time.

**Suggested-action tips**: per explicit user decision, these are static,
hand-written one-liners per category (`TELEGRAM_SUGGESTED_ACTIONS`), not an
LLM-generated suggestion — this execution core is deliberately non-AI/
deterministic (see CLAUDE.md), and this alert path is synchronous
background-thread code with a hard 3s Telegram timeout; a live LLM call
here would add latency, cost, and a new external failure mode to a path
whose entire job is telling the user something is already wrong. A missing
entry just omits the tip line, never blocks the alert itself.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import UTC, datetime
from datetime import time as dt_time

import httpx
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.clock import now_ist
from app.domain.execution.models import OrderMode
from app.domain.ops.models import AlertSeverity, SystemAlert
from app.modules.ops import weekend_rest

logger = logging.getLogger("app.alerting.manager")

_TELEGRAM_TIMEOUT_SECONDS = 3.0

# Logged once per process, not once per missed alert — a genuinely
# unconfigured Telegram bot would otherwise spam a warning on every single
# alert this process ever raises for its entire lifetime.
_warned_missing_config = False

# See this module's own docstring for the full per-condition reasoning.
TELEGRAM_ALLOWED_CATEGORIES = frozenset(
    {
        "strategy_run_stalled",
        "stale_session_not_closed",
        "protective_stop_placement_failed",
        "protective_stop_cancel_failed",
        "protective_stop_cancel_unresolved",
        "exit_order_unfilled",
        "margin_breach_square_off",
        "daily_loss_cap_breached",
        "reconciliation_mismatch",
        "health_check_failed",
        "order_rejected",
        "broker_disconnected",
        "market_data_stale",
        "market_data_failover_switch",
        "market_data_no_session",
        "trade_approval_pending",
        "exit_legs_collapsed",
        "db_pool_saturated",
        "lock_contention_high",
    }
)

# Deliberately narrower than market_data.market_hours' ~08:30-16:00
# connectivity window or core.clock's 09:31-15:09 trade-entry window — this
# is specifically "when is it worth interrupting the user," not a data or
# risk gate. TradingSession.cutoff_time's own default (15:09) sits well
# inside this window, so EOD square-off is always still inside it.
_ALERT_WINDOW_START = dt_time(9, 0)
_ALERT_WINDOW_END = dt_time(15, 30)

_DEDUP_COOLDOWN_SECONDS = 15 * 60

# Static, hand-written one-liners — see this module's own docstring for why
# these are not LLM-generated. Keyed by category; a category with no entry
# here just gets no tip line appended, never an error.
TELEGRAM_SUGGESTED_ACTIONS: dict[str, str] = {
    "strategy_run_stalled": (
        "Check Shoonya connectivity and market-data flow; square off manually if the "
        "position is unprotected."
    ),
    "stale_session_not_closed": (
        "Open Sessions and check yesterday's session for real open risk before "
        "starting today's trading."
    ),
    "protective_stop_placement_failed": (
        "Position has no broker-side stop -- verify broker connectivity and place a "
        "manual SL immediately."
    ),
    "protective_stop_cancel_failed": (
        "A resting stop may still be live at the broker -- check the order book "
        "before placing a new exit."
    ),
    "protective_stop_cancel_unresolved": (
        "Check the broker order book directly; a duplicate exit order is possible "
        "if this resolves late."
    ),
    "exit_order_unfilled": (
        "Confirm the exit in the broker's order book -- the position may still be "
        "open longer than expected."
    ),
    "margin_breach_square_off": (
        "Review margin usage and confirm the emergency square-off actually closed "
        "the position at the broker."
    ),
    "daily_loss_cap_breached": (
        "Entries are paused for the day -- review today's trades before considering "
        "a manual override."
    ),
    "reconciliation_mismatch": (
        "Compare the Reconciliation Runs panel against the broker's own position "
        "book before taking any action."
    ),
    "health_check_failed": (
        "Check the host's disk space and system clock; a live session may "
        "already be in degraded_mode."
    ),
    "order_rejected": (
        "Check the broker's rejection reason in the message above -- often margin, "
        "freeze qty, or an invalid symbol."
    ),
    "broker_disconnected": (
        "Reconnect Shoonya from the Sessions page; the session may already be in "
        "degraded_mode."
    ),
    "market_data_stale": (
        "Check Shoonya WS/REST connectivity for this underlying; consider the "
        "failback provider if this persists."
    ),
    "market_data_failover_switch": (
        "The backup market-data provider is now active -- confirm the primary "
        "provider's health before switching back."
    ),
    "market_data_no_session": (
        "Neither Shoonya nor the failback provider has a live session yet -- "
        "connect at least one from the Sessions page before market open."
    ),
    "trade_approval_pending": (
        "Open Control Room and Approve/Reject before the approval window expires."
    ),
    "exit_legs_collapsed": (
        "A live position's staged-exit config was ignored -- it is running on a single "
        "full-qty stop/target instead. Review params.exit_legs for this strategy."
    ),
    "db_pool_saturated": (
        "Check for a burst of near-simultaneous order dispatch/exits -- should self-clear "
        "once broker calls finish; escalate only if it stays saturated."
    ),
    "lock_contention_high": (
        "Order dispatch is queuing on the execution lock -- check broker response times; "
        "a dispatch may fail with a lock-timeout error if this continues."
    ),
}

_dedup_lock = threading.Lock()
_last_pushed_by_key: dict[str, datetime] = {}

# See this module's own docstring ("2026-09-03: self-healing grace window")
# for the full reasoning. Deliberately excludes protective_stop_cancel_failed/
# protective_stop_placement_failed (a real exception, not an ambiguous ack)
# and exit_order_attempts_exhausted (the terminal "give up" signal, must
# never be delayed). Keep in sync with ControlRoomPage.tsx's
# SELF_HEALING_GRACE_CATEGORIES if this changes.
_SELF_HEALING_GRACE_CATEGORIES = frozenset(
    {"protective_stop_cancel_unresolved", "exit_order_unfilled", "reconciliation_mismatch"}
)
_SELF_HEALING_GRACE_SECONDS = 10.0

_first_seen_lock = threading.Lock()
_first_seen_by_key: dict[str, datetime] = {}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _within_alert_window(now: datetime) -> bool:
    return _ALERT_WINDOW_START <= now.time() <= _ALERT_WINDOW_END


def _dedup_allows_push(dedup_key: str, now: datetime) -> bool:
    with _dedup_lock:
        last_pushed = _last_pushed_by_key.get(dedup_key)
        if (
            last_pushed is not None
            and (now - last_pushed).total_seconds() < _DEDUP_COOLDOWN_SECONDS
        ):
            return False
        _last_pushed_by_key[dedup_key] = now
        return True


def _self_healing_grace_elapsed(dedup_key: str, now: datetime) -> bool:
    """Records the first time `dedup_key` is seen; returns `True` once it has
    been continuously observed for at least `_SELF_HEALING_GRACE_SECONDS`.
    In-memory only, independent of the `SystemAlert` row itself — see this
    module's own docstring. Never removes an entry once resolved (matching
    `_last_pushed_by_key`'s own unbounded-growth acceptance — a `dedup_key`
    here is normally scoped to one position/session, so this only grows by
    real trading volume, not per-cycle re-checks of the same issue).
    """
    with _first_seen_lock:
        first_seen = _first_seen_by_key.get(dedup_key)
        if first_seen is None:
            _first_seen_by_key[dedup_key] = now
            return False
        return (now - first_seen).total_seconds() >= _SELF_HEALING_GRACE_SECONDS


def _should_push_to_telegram(
    *,
    category: str,
    severity: AlertSeverity,
    mode: OrderMode | None,
    dedup_key: str,
    override_paper_mode_suppression: bool = False,
) -> bool:
    if category not in TELEGRAM_ALLOWED_CATEGORIES:
        return False
    if severity != AlertSeverity.CRITICAL:
        return False
    if mode == OrderMode.PAPER and not override_paper_mode_suppression:
        return False
    # Weekend rest mode: while the system is dormant (a weekend with no
    # signed-in user), suppress the push -- checked before the dedup step
    # below so a suppressed candidate never consumes/extends a dedup slot,
    # same ordering rationale as the time-window check. The SystemAlert DB
    # row is still written unconditionally by send_alert. No-op Mon-Fri.
    if weekend_rest.is_dormant():
        return False
    # Self-healing grace window -- checked before the dedup step below for
    # the same reason: a candidate held back here must never start/extend
    # that issue's dedup cooldown, since it was never actually pushed.
    if category in _SELF_HEALING_GRACE_CATEGORIES and not _self_healing_grace_elapsed(
        dedup_key, _utcnow()
    ):
        return False
    now = now_ist()
    if not _within_alert_window(now):
        return False
    # Dedup is checked last and only consumes a dedup slot once every other
    # condition above has already passed — a candidate rejected for being
    # outside the allowlist/severity/mode/window must never start (or
    # extend) that issue's cooldown, since it was never actually pushed.
    return _dedup_allows_push(dedup_key, now)


def _send_telegram(message: str) -> None:
    global _warned_missing_config
    telegram = get_settings().telegram
    bot_token = telegram.bot_token.get_secret_value()
    if not bot_token or not telegram.chat_id:
        if not _warned_missing_config:
            logger.warning(
                "Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
                "missing) -- alerts will be written to SystemAlert only"
            )
            _warned_missing_config = True
        return

    try:
        httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": telegram.chat_id, "text": message},
            timeout=_TELEGRAM_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        # Never lets a Telegram outage take down the caller's own cycle --
        # the SystemAlert row (written before this call) is already the
        # durable record regardless of what happens here.
        logger.warning("Telegram alert dispatch failed", exc_info=True)


def send_alert(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    severity: AlertSeverity,
    category: str,
    message: str,
    trading_session_id: uuid.UUID | None = None,
    mode: OrderMode | None = None,
    dedup_key: str | None = None,
    payload: dict | None = None,
    override_paper_mode_suppression: bool = False,
) -> SystemAlert:
    """Writes the `SystemAlert` row first, then attempts Telegram — matching
    `audit_service.record_event`'s own contract, this only flushes, never
    commits; the caller's surrounding transaction (already open in every
    real call site: `StrategyRunner`'s per-cycle `session_scope()`,
    `HealthCheckScheduler`'s own loop) owns the commit.

    `mode`: the `OrderMode` of the specific position/order this alert
    concerns, if any. `OrderMode.PAPER` blocks the Telegram push (see
    module docstring) unless `override_paper_mode_suppression=True`; `None`
    is for alerts with no specific paper/live position behind them (health
    checks, instrument-master sync) and is never paper-suppressed. Ignored
    entirely for the `SystemAlert` row itself — that's written the same
    regardless.

    `override_paper_mode_suppression`: when `True`, a `mode=OrderMode.PAPER`
    alert is still eligible for Telegram (all other gates — allowlist,
    CRITICAL, time window, dedup — still apply). Used by reconciliation for
    a paper-book mismatch on a live-active session.

    `dedup_key`: identifies "the same issue" for the 15-minute Telegram
    dedup window. Defaults to `f"{category}:{trading_session_id or
    workspace_id}"` — callers with a more specific entity (a position, an
    order) should pass an explicit key (e.g. `f"{category}:{position.id}"`)
    so a *different* position hitting the same category isn't mistaken for
    a repeat of the same issue.

    `payload`: structured data stored on the `SystemAlert` row's own
    `payload` JSONB column (dashboard/API consumers only — never included
    in the Telegram text). Defaults to `{}`, matching the column's own
    model-level default.
    """
    alert = SystemAlert(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        trading_session_id=trading_session_id,
        severity=severity,
        category=category,
        message=message,
        payload=payload if payload is not None else {},
        created_at=_utcnow(),
    )
    db.add(alert)
    db.flush()

    effective_dedup_key = dedup_key or f"{category}:{trading_session_id or workspace_id}"
    if _should_push_to_telegram(
        category=category,
        severity=severity,
        mode=mode,
        dedup_key=effective_dedup_key,
        override_paper_mode_suppression=override_paper_mode_suppression,
    ):
        text = f"[{severity.value.upper()}] {category}: {message}"
        tip = TELEGRAM_SUGGESTED_ACTIONS.get(category)
        if tip:
            text = f"{text}\n\nSuggested: {tip}"
        _send_telegram(text)

    return alert
