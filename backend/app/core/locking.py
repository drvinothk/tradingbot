"""Postgres advisory locks — the one mechanism behind every "exactly one at a
time" guarantee in this system: the Execution singleton, the serialized Risk
evaluation queue, and the audit hash-chain append (each new row's prev_hash
must be computed against a stable "last row", which requires excluding
concurrent writers, not just relying on transaction isolation).

Named locks are mapped to a stable bigint via hashing the name — Postgres
advisory locks are keyed by integers, not strings.

**`advisory_lock()` uses transaction-scoped locks (`pg_advisory_xact_lock`),
not session-scoped.** A real incident (found live during Phase 7's browser
verification) is why: `SQLAlchemy Session.commit()` releases the connection
back to the pool, and any `db.execute()` call after a `commit()` — including
the `finally: pg_advisory_unlock(...)` a session-scoped lock needs — can get
handed a *different* physical connection than the one that acquired the
lock. `pg_advisory_unlock` on the wrong connection silently returns `false`
(no error), while the original connection goes back into the pool still
holding the lock, forever, invisible to any per-request diagnostic. Several
call sites (`start_strategy`, `approve_trade_approval`,
`reject_trade_approval`, `create_session`) call `db.commit()` while still
inside a `with advisory_lock(...)` block — by design, since the check-then-
act sequence they guard needs to be durable *before* the lock releases, not
after. `pg_advisory_xact_lock` has no separate unlock call at all: release is
automatic, tied to whatever connection the transaction commits or rolls back
on, which makes this whole leak class structurally impossible rather than
just less likely. See `docs/architecture/build-plan.md`'s Phase 7 section for
the full root-cause writeup.
"""

from __future__ import annotations

import threading
import time
import zlib
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

# Stable names for the locks this system relies on. Add new ones here rather
# than inventing ad-hoc strings at call sites, so every lock in use is visible
# in one place.
LOCK_EXECUTION_SINGLETON = "execution_singleton"
LOCK_RISK_EVALUATION_QUEUE = "risk_evaluation_queue"
LOCK_AUDIT_CHAIN = "audit_chain"
# Mode transitions deliberately share LOCK_EXECUTION_SINGLETON rather than
# having their own lock — a transition into kill_switch must never interleave
# with an in-flight order dispatch acquiring the same singleton.

# Distinct from LOCK_EXECUTION_SINGLETON above: that one is acquired briefly
# per operation (one transition, one dispatch). This one is acquired ONCE at
# process startup and held for the process's entire lifetime, so a second
# backend process accidentally started alongside the first (e.g. the Windows
# Service already running plus someone launching it manually) fails fast at
# startup instead of both processes believing they're the sole engine.
# Deliberately NOT converted to a transaction-scoped lock like the three
# above — it must outlive any single transaction, so it keeps the
# session-scoped, dedicated-connection pattern in app.main instead (a raw
# engine.connect() held open for the process lifetime, never returned to the
# pool, so the leak class above doesn't apply to it).
LOCK_PROCESS_SINGLETON = "engine_process_singleton"

# How long a caller will wait to acquire any of the transaction-scoped locks
# below before failing loudly, rather than blocking forever — defense in
# depth against any future stuck-lock scenario, confirmed or not: a fast,
# diagnosable OperationalError beats a silent hang in a live trading system.
# Callers aren't expected to catch this specifically; PositionManager's own
# `_loop` already logs-and-retries-next-cycle on any uncaught exception, and
# API endpoints surface it as a 500 — a clean typed exception/HTTP mapping is
# a reasonable future refinement, not required for the leak fix itself.
LOCK_ACQUIRE_TIMEOUT = "10s"


def _lock_key(name: str) -> int:
    # Signed 32-bit range, matching pg_advisory_lock(int)'s single-arg form.
    return zlib.crc32(name.encode("utf-8")) - (1 << 31)


# 2026-08-31: in-memory-only slow-acquire tracking, added after the live
# whole-app-hang incident traced to LOCK_EXECUTION_SINGLETON queuing under a
# multi-strategy spike (see settings.py's DBSettings.pool_size comment for
# the full incident). This module stays free of any DB write or alerting
# import — HealthCheckScheduler drains `pop_lock_wait_stats()` on its own
# periodic cycle and does all recording/alerting there, keeping this
# safety-critical primitive's own blast radius unchanged. Below the
# threshold, this costs one `time.monotonic()` read and a comparison per
# acquisition — negligible next to the Postgres round-trip on the very next
# line, and nowhere near the scale of the actual incident (connections
# blocked on broker I/O, not CPU bookkeeping).
_SLOW_ACQUIRE_THRESHOLD_SECONDS = 1.0
_lock_stats_guard = threading.Lock()
_lock_wait_stats: dict[str, tuple[float, int]] = {}  # name -> (max_wait_seconds, slow_count)


def _record_acquire_wait(name: str, wait_seconds: float) -> None:
    if wait_seconds < _SLOW_ACQUIRE_THRESHOLD_SECONDS:
        return
    with _lock_stats_guard:
        max_wait, slow_count = _lock_wait_stats.get(name, (0.0, 0))
        _lock_wait_stats[name] = (max(max_wait, wait_seconds), slow_count + 1)


def pop_lock_wait_stats() -> dict[str, tuple[float, int]]:
    """Returns and clears every lock name's accumulated (max_wait_seconds,
    slow_count) since the last call — a periodic reader
    (`HealthCheckScheduler`) drains this each cycle. Draining on read means a
    caller that never reads (e.g. a test exercising `advisory_lock` directly)
    simply lets entries accumulate harmlessly until something does.
    """
    with _lock_stats_guard:
        stats = dict(_lock_wait_stats)
        _lock_wait_stats.clear()
    return stats


# 2026-08-31: hold-time tracking, added alongside the wait-time tracker above
# to answer the question that started this whole investigation directly,
# rather than just inferring it: is a slow *acquire* actually caused by a
# slow *broker call* while the lock is held? Same in-memory-only shape, same
# guard, same drain-on-read contract. Deliberately a second dict rather than
# folding into `_lock_wait_stats` -- wait and hold are different signals
# (HealthCheckScheduler alerts on them at different severities), and keeping
# them separate keeps each one independently readable/testable.
_SLOW_HOLD_THRESHOLD_SECONDS = 1.0
_lock_hold_stats: dict[str, tuple[float, int]] = {}  # name -> (max_hold_seconds, slow_count)


def _record_hold(name: str, hold_seconds: float) -> None:
    if hold_seconds < _SLOW_HOLD_THRESHOLD_SECONDS:
        return
    with _lock_stats_guard:
        max_hold, slow_count = _lock_hold_stats.get(name, (0.0, 0))
        _lock_hold_stats[name] = (max(max_hold, hold_seconds), slow_count + 1)


def pop_lock_hold_stats() -> dict[str, tuple[float, int]]:
    """Returns and clears every lock name's accumulated (max_hold_seconds,
    slow_count) since the last call — same drain-on-read contract as
    `pop_lock_wait_stats`, read by the same periodic caller.
    """
    with _lock_stats_guard:
        stats = dict(_lock_hold_stats)
        _lock_hold_stats.clear()
    return stats


@contextmanager
def advisory_lock(db: Session, name: str) -> Generator[None, None, None]:
    """Transaction-scoped advisory lock — released automatically at whatever
    commit or rollback ends the current transaction on `db`'s connection, not
    at the `with` block's own exit (see this module's docstring for why).
    Reentrant within one transaction (a second acquisition of the same key by
    the same session/transaction is a fast no-op), same as the session-scoped
    primitive it replaced. Blocks until acquired, bounded by
    `LOCK_ACQUIRE_TIMEOUT` — callers needing a non-blocking attempt should use
    try_advisory_lock instead (session-scoped; only `LOCK_PROCESS_SINGLETON`
    uses it, see that constant's own docstring for why it's different).

    Also times how long the caller spends inside the `with` block itself
    (`pop_lock_hold_stats`) — a faithful measure of real hold time for
    callers that commit at their own surrounding session boundary rather
    than inside this block (`dispatch_trade_intent`/`close_position`, the
    two call sites this exists to diagnose), but an underestimate for the
    few call sites that commit *early*, inside the block, by design
    (`start_strategy`/`approve_trade_approval`/`reject_trade_approval`/
    `create_session` — see this module's own docstring above) — the lock is
    actually released at that inner commit, before the `with` block exits.
    """
    key = _lock_key(name)
    # SET LOCAL doesn't accept bind parameters in Postgres (it's a utility
    # statement, not DML) — safe to inline here since LOCK_ACQUIRE_TIMEOUT is
    # a fixed internal constant, never user input. Transaction-scoped, same
    # as the lock itself, so it reverts automatically at commit/rollback.
    db.execute(text(f"SET LOCAL lock_timeout = '{LOCK_ACQUIRE_TIMEOUT}'"))
    started = time.monotonic()
    try:
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    finally:
        # Runs even when the SELECT itself raised (a genuine lock timeout) —
        # that's the case most worth capturing, not just the success path.
        # Never raises itself: a pure in-memory dict/lock update must not be
        # able to mask the real exception this finally block re-propagates.
        _record_acquire_wait(name, time.monotonic() - started)

    hold_started = time.monotonic()
    try:
        yield
    finally:
        # Runs even when the caller's own block body raises -- standard
        # @contextmanager semantics (the exception is thrown into this
        # generator at the yield above), already exercised today by every
        # existing `raise ValueError(...)` inside a `with advisory_lock(...)`
        # block elsewhere in this codebase. Never raises itself, same
        # reasoning as _record_acquire_wait above.
        _record_hold(name, time.monotonic() - hold_started)


def try_advisory_lock(db: Session, name: str) -> bool:
    """Non-blocking attempt. Returns True (and holds the lock) if acquired,
    False if another session already holds it. Caller is responsible for
    calling release_advisory_lock when done if this returns True — used by
    the Execution singleton startup check, which should fail fast rather than
    block if a second instance is accidentally started.
    """
    key = _lock_key(name)
    row = db.execute(text("SELECT pg_try_advisory_lock(:key) AS ok"), {"key": key}).one()
    return bool(row.ok)


def release_advisory_lock(db: Session, name: str) -> None:
    key = _lock_key(name)
    db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
