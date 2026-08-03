"""Regression tests for the `LOCK_EXECUTION_SINGLETON` leak fixed in Phase 7:
a session-scoped advisory lock + SQLAlchemy connection pooling could leave a
lock held forever the moment any caller committed while still holding it
(`SQLAlchemy Session.commit()` releases the connection back to the pool, so
the `finally: pg_advisory_unlock(...)` a session-scoped lock needs could run
on a *different* pooled connection than the one that acquired the lock — see
`core/locking.py`'s own module docstring for the full root-cause writeup).

All of these need real, independent Postgres connections/commits — the
rolled-back `db` fixture from conftest.py never reaches a real commit, so it
can't exercise connection-pool behavior at all. Follows
`test_phase4_strategies_e2e.py`'s `real_commit_factory` pattern instead.

Deliberately does NOT try to force SQLAlchemy's connection pool into handing
out a specific physical connection to prove the leak reproduces — tried
first (a small-pool engine plus several threads racing the same
acquire/commit/touch pattern), and it was unreliable in both directions:
sometimes passed against the known-broken pre-fix code, and independently
showed inconsistent pool-contention timing between a plain script and a
pytest run of the same logic. Real pool internals aren't a contract this
test suite should assume control over. `test_session_scoped_unlock_on_wrong_
connection_leaks` below proves the exact same underlying mechanism
deterministically instead, by using two explicit connections directly rather
than hoping the pool hands out the "wrong" one.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.core.locking import advisory_lock, try_advisory_lock
from app.core.security.passwords import hash_password
from app.domain.identity.models import (
    BrokerAccount,
    BrokerAccountStatus,
    BrokerType,
    User,
    Workspace,
)
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import (
    ExecutionMode,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
)

TEST_LOCK_NAME = "test_locking_regression"


@pytest.fixture
def real_commit_factory(engine):
    session_factory = sessionmaker(bind=engine, future=True)

    @contextmanager
    def _scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _scope


def test_session_scoped_unlock_on_wrong_connection_leaks(engine):
    """Documents the exact mechanism that made the pre-fix `advisory_lock()`
    dangerous, deterministically — no pool-timing/thread-race dependency,
    unlike trying to force SQLAlchemy's pool to hand out a specific
    connection (tried first; empirically unreliable both directions, since
    whether a lone session gets its own connection back or a different one
    depends on internal pool state a test shouldn't assume control over).
    `pg_advisory_lock` is tied to a specific physical connection;
    `pg_advisory_unlock` called on a *different* connection is a silent
    no-op (returns `false`, no error) — precisely what could happen to the
    old code's `finally: pg_advisory_unlock(...)` once `Session.commit()`
    (known, per SQLAlchemy's own docs, to release the connection back to
    the pool) ran first. `pg_advisory_xact_lock` (the actual fix) has no
    separate unlock call at all, which is what makes this whole failure
    mode structurally impossible rather than just less likely.
    """
    key = 918273645
    conn_a = engine.connect()
    conn_b = engine.connect()
    try:
        conn_a.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
        conn_a.commit()

        wrong_connection_unlock = conn_b.execute(
            text("SELECT pg_advisory_unlock(:key) AS ok"), {"key": key}
        ).one()
        conn_b.commit()
        assert wrong_connection_unlock.ok is False, (
            "unlock from a different connection should silently no-op, not succeed"
        )

        conn_c = engine.connect()
        try:
            still_locked = conn_c.execute(
                text("SELECT pg_try_advisory_lock(:key) AS ok"), {"key": key}
            ).one()
            conn_c.commit()
            assert still_locked.ok is False, "lock should still be held by conn_a"
        finally:
            conn_c.close()
    finally:
        conn_a.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        conn_a.commit()
        conn_a.close()
        conn_b.close()


def test_advisory_lock_releases_on_commit_not_on_with_block_exit(real_commit_factory):
    """The actual fixed `advisory_lock()`: acquire, commit *inside* the
    `with` block (the exact shape `start_strategy`/`approve_trade_approval`/
    `reject_trade_approval`/`create_session` all use), do one more
    statement, then let the block exit — a second, independent session must
    be able to acquire the lock immediately after the commit, proving
    release is tied to the commit itself and not dependent on which
    connection eventually runs the `with` block's own exit.
    """
    with real_commit_factory() as db1:
        with advisory_lock(db1, TEST_LOCK_NAME):
            db1.commit()
            db1.execute(text("SELECT 1"))

    with real_commit_factory() as db2:
        acquired = try_advisory_lock(db2, TEST_LOCK_NAME)
        assert acquired is True, "lock not released after commit"
        db2.execute(text("SELECT pg_advisory_unlock_all()"))


def test_reentrant_within_same_transaction(real_commit_factory):
    """A second acquisition of the same key by the same session/transaction
    must be a fast no-op, not a self-deadlock — this is what makes
    dispatch_trade_intent's nested transition_mode call (both acquiring
    LOCK_EXECUTION_SINGLETON) safe.
    """
    with real_commit_factory() as db:
        with advisory_lock(db, TEST_LOCK_NAME):
            with advisory_lock(db, TEST_LOCK_NAME):
                db.execute(text("SELECT 1"))


def test_second_session_blocks_until_first_commits(real_commit_factory):
    """Genuine mutual exclusion across sessions: a second session's
    acquisition attempt must wait for the first to release (at commit), not
    succeed early — proving the lock still actually excludes concurrent
    callers, not just that it stops leaking.
    """
    first_holds_lock = threading.Event()
    second_acquired = threading.Event()
    release_first = threading.Event()

    def _hold_then_release():
        with real_commit_factory() as db:
            with advisory_lock(db, TEST_LOCK_NAME):
                first_holds_lock.set()
                release_first.wait(timeout=10)
                db.commit()

    def _wait_then_acquire():
        first_holds_lock.wait(timeout=10)
        with real_commit_factory() as db:
            with advisory_lock(db, TEST_LOCK_NAME):
                second_acquired.set()

    t1 = threading.Thread(target=_hold_then_release)
    t2 = threading.Thread(target=_wait_then_acquire)
    t1.start()
    first_holds_lock.wait(timeout=10)
    t2.start()

    # Give the second thread a moment to actually attempt acquisition —
    # it must NOT have succeeded yet, since the first thread still holds
    # the lock (hasn't committed/released).
    assert not second_acquired.wait(timeout=1), "second session acquired the lock too early"

    release_first.set()
    t1.join(timeout=10)
    assert second_acquired.wait(timeout=10), "second session never acquired the lock"
    t2.join(timeout=10)


def test_concurrent_strategy_run_creation_exactly_one_wins(real_commit_factory):
    """End-to-end reproduction of the original failure scenario, at the DB
    layer: two threads racing to create a StrategyRun for the same
    strategy_config_id, each replicating start_strategy's own
    check-then-insert-then-commit-while-locked shape. Must complete without
    hanging, exactly one StrategyRun must exist afterward, and the lock must
    be free immediately after (proving no leak from either the winner or
    the loser's path).
    """
    with real_commit_factory() as setup_db:
        workspace = Workspace(id=uuid.uuid4(), name=f"lock-test-{uuid.uuid4().hex[:8]}")
        setup_db.add(workspace)
        setup_db.flush()
        user = User(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            email=f"lock-test-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("correct horse battery staple"),
            display_name="Lock Test User",
            is_active=True,
        )
        setup_db.add(user)
        setup_db.flush()
        broker_account = BrokerAccount(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            broker_type=BrokerType.SHOONYA,
            label="lock-test-account",
            credentials_ref="config/credentials/shoonya.env",
            status=BrokerAccountStatus.ACTIVE,
        )
        setup_db.add(broker_account)
        setup_db.flush()
        trading_session = TradingSession(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            broker_account_id=broker_account.id,
            started_by_user_id=user.id,
            mode=SafeMode.PAPER_ONLY,
            started_at=datetime.now(UTC),
            budget_amount=1_000_000,
            daily_target_profit=1_000_000,
            daily_loss_cap=1_000_000,
            funding_mode=FundingMode.CASH,
        )
        setup_db.add(trading_session)
        setup_db.flush()
        config = StrategyConfig(
            id=uuid.uuid4(), workspace_id=workspace.id, name="lock-race-test"
        )
        setup_db.add(config)
        setup_db.flush()
        workspace_id, user_id, config_id = workspace.id, user.id, config.id
        broker_account_id, trading_session_id = broker_account.id, trading_session.id

    results: list[str] = []
    errors: list[Exception] = []
    start_barrier = threading.Barrier(2, timeout=10)

    def _attempt_start():
        try:
            start_barrier.wait()
            with real_commit_factory() as db:
                with advisory_lock(db, TEST_LOCK_NAME):
                    existing = (
                        db.query(StrategyRun)
                        .filter(
                            StrategyRun.strategy_config_id == config_id,
                            StrategyRun.status != StrategyRunStatus.STOPPED,
                        )
                        .one_or_none()
                    )
                    if existing is not None:
                        results.append("rejected")
                        return
                    run = StrategyRun(
                        id=uuid.uuid4(),
                        strategy_config_id=config_id,
                        trading_session_id=trading_session_id,
                        execution_mode=ExecutionMode.AUTO,
                        status=StrategyRunStatus.SCANNING,
                        started_at=datetime.now(UTC),
                        started_by_user_id=user_id,
                    )
                    db.add(run)
                    db.commit()
                    results.append("created")
        except Exception as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_attempt_start) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "a thread is still hung — the lock leaked again"

    assert not errors, f"unexpected errors: {errors}"
    assert sorted(results) == ["created", "rejected"]

    with real_commit_factory() as verify_db:
        run_count = (
            verify_db.query(StrategyRun)
            .filter(StrategyRun.strategy_config_id == config_id)
            .count()
        )
        assert run_count == 1

        acquired = try_advisory_lock(verify_db, TEST_LOCK_NAME)
        assert acquired is True, "lock still held after both threads finished"
        verify_db.execute(text("SELECT pg_advisory_unlock_all()"))

        # Cleanup, FK-safe order.
        verify_db.query(StrategyRun).filter(
            StrategyRun.strategy_config_id == config_id
        ).delete(synchronize_session=False)
        verify_db.query(StrategyConfig).filter(StrategyConfig.id == config_id).delete(
            synchronize_session=False
        )
        verify_db.query(TradingSession).filter(
            TradingSession.id == trading_session_id
        ).delete(synchronize_session=False)
        verify_db.query(BrokerAccount).filter(BrokerAccount.id == broker_account_id).delete(
            synchronize_session=False
        )
        verify_db.query(User).filter(User.id == user_id).delete(synchronize_session=False)
        verify_db.query(Workspace).filter(Workspace.id == workspace_id).delete(
            synchronize_session=False
        )
        verify_db.commit()
