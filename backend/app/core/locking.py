"""Postgres advisory locks — the one mechanism behind every "exactly one at a
time" guarantee in this system: the Execution singleton, the serialized Risk
evaluation queue, and the audit hash-chain append (each new row's prev_hash
must be computed against a stable "last row", which requires excluding
concurrent writers, not just relying on transaction isolation).

Named locks are mapped to a stable bigint via hashing the name — Postgres
advisory locks are keyed by integers, not strings.
"""

from __future__ import annotations

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
LOCK_PROCESS_SINGLETON = "engine_process_singleton"


def _lock_key(name: str) -> int:
    # Signed 32-bit range, matching pg_advisory_lock(int)'s single-arg form.
    return zlib.crc32(name.encode("utf-8")) - (1 << 31)


@contextmanager
def advisory_lock(db: Session, name: str) -> Generator[None, None, None]:
    """Session-level advisory lock, held for the lifetime of the `with` block
    on the given SQLAlchemy Session's underlying connection. Blocks until
    acquired — callers needing a non-blocking attempt should use
    try_advisory_lock instead.
    """
    key = _lock_key(name)
    db.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
    try:
        yield
    finally:
        db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


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
