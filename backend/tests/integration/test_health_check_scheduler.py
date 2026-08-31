"""HealthCheckScheduler — the periodic NTP/disk timer loop that replaces
`app.main`'s one-shot boot check with a real Scheduler, per the build plan's
Addendum. Requires real Postgres (mode transitions use the same advisory-
lock-backed `transition_mode` every other mode-transition test needs).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time

import pytest
from sqlalchemy.orm import Session

from app.core.clock import ClockCheckResult, DiskCheckResult
from app.domain.identity.models import BrokerAccount, BrokerAccountStatus, BrokerType, User
from app.domain.market.models import Instrument, PriceBar, QuoteTick
from app.domain.ops.models import AlertSeverity, MetricSeries, SystemAlert
from app.domain.session.models import (
    FundingMode,
    SafeMode,
    SessionModeTransition,
    TradingSession,
)
from app.modules.scheduler.health_check import HealthCheckScheduler

_OK_NTP = ClockCheckResult(ok=True, drift_seconds=0.1)
_OK_DISK = DiskCheckResult(ok=True, free_gb=50.0, total_gb=200.0)
_BAD_NTP = ClockCheckResult(ok=False, drift_seconds=None, error="unreachable")
_BAD_DISK = DiskCheckResult(ok=False, free_gb=0.5, total_gb=200.0)


@pytest.fixture
def broker_account(db: Session, workspace) -> BrokerAccount:
    account = BrokerAccount(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        broker_type=BrokerType.SHOONYA,
        label="health-check-test-account",
        credentials_ref="config/credentials/shoonya.env",
        status=BrokerAccountStatus.ACTIVE,
    )
    db.add(account)
    db.flush()
    return account


@pytest.fixture
def trading_session(db: Session, workspace, broker_account, user: User) -> TradingSession:
    ts = TradingSession(
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
        cutoff_time=dt_time(23, 59),
    )
    db.add(ts)
    db.flush()
    return ts


def _session_factory_for(db: Session):
    @contextmanager
    def _factory():
        yield db

    return _factory


def _scheduler_for(db: Session) -> HealthCheckScheduler:
    return HealthCheckScheduler(session_factory=_session_factory_for(db))


def test_run_once_records_metrics_for_active_sessions_workspace(
    db: Session, trading_session, monkeypatch
):
    monkeypatch.setattr(
        "app.modules.scheduler.health_check.check_ntp_drift", lambda: _OK_NTP
    )
    monkeypatch.setattr(
        "app.modules.scheduler.health_check.check_disk_space", lambda path: _OK_DISK
    )
    # Deterministic regardless of what any other test (e.g. test_locking.py's
    # slow-second-session test) may have left in the real, process-wide
    # lock-wait/hold-stats dicts -- pop_lock_wait_stats()/pop_lock_hold_stats()
    # are real module-level stores shared by anything that calls
    # advisory_lock, so this test must not assume they start empty.
    monkeypatch.setattr(
        "app.modules.scheduler.health_check.pop_lock_wait_stats", lambda: {}
    )
    monkeypatch.setattr(
        "app.modules.scheduler.health_check.pop_lock_hold_stats", lambda: {}
    )

    _scheduler_for(db).run_once()

    metrics = (
        db.query(MetricSeries)
        .filter(MetricSeries.workspace_id == trading_session.workspace_id)
        .all()
    )
    names = {m.metric_name for m in metrics}
    assert names == {"ntp_drift_seconds", "disk_free_gb", "db_pool_checked_out"}


def test_run_once_moves_guarded_live_session_to_degraded_mode_on_failed_check(
    db: Session, trading_session, monkeypatch
):
    trading_session.mode = SafeMode.LIVE_ENABLED
    db.flush()

    monkeypatch.setattr(
        "app.modules.scheduler.health_check.check_ntp_drift", lambda: _BAD_NTP
    )
    monkeypatch.setattr(
        "app.modules.scheduler.health_check.check_disk_space", lambda path: _OK_DISK
    )

    _scheduler_for(db).run_once()

    db.refresh(trading_session)
    assert trading_session.mode == SafeMode.DEGRADED_MODE
    assert (
        db.query(SessionModeTransition)
        .filter(SessionModeTransition.trading_session_id == trading_session.id)
        .count()
        == 1
    )
    assert (
        db.query(SystemAlert)
        .filter(
            SystemAlert.workspace_id == trading_session.workspace_id,
            SystemAlert.category == "health_check_failed",
        )
        .count()
        == 1
    )


def test_run_once_does_not_transition_paper_only_session_on_failed_check(
    db: Session, trading_session, monkeypatch
):
    assert trading_session.mode == SafeMode.PAPER_ONLY

    monkeypatch.setattr(
        "app.modules.scheduler.health_check.check_ntp_drift", lambda: _OK_NTP
    )
    monkeypatch.setattr(
        "app.modules.scheduler.health_check.check_disk_space", lambda path: _BAD_DISK
    )

    _scheduler_for(db).run_once()  # must not raise

    db.refresh(trading_session)
    assert trading_session.mode == SafeMode.PAPER_ONLY
    assert (
        db.query(SessionModeTransition)
        .filter(SessionModeTransition.trading_session_id == trading_session.id)
        .count()
        == 0
    )
    # Paper-only still gets alert visibility, just no mode escalation.
    assert (
        db.query(SystemAlert)
        .filter(
            SystemAlert.workspace_id == trading_session.workspace_id,
            SystemAlert.category == "health_check_failed",
        )
        .count()
        == 1
    )


def _seed_nifty(db: Session) -> Instrument:
    inst = Instrument(
        id=uuid.uuid4(), symbol="NIFTY", exchange="NSE", lot_size=75, tick_size=0.05
    )
    db.add(inst)
    db.flush()
    return inst


def _ntp_disk_ok(monkeypatch) -> None:
    monkeypatch.setattr("app.modules.scheduler.health_check.check_ntp_drift", lambda: _OK_NTP)
    monkeypatch.setattr(
        "app.modules.scheduler.health_check.check_disk_space", lambda path: _OK_DISK
    )
    monkeypatch.setattr(
        "app.modules.scheduler.health_check.is_within_market_hours", lambda: True
    )
    # The staleness sub-check is skipped on any weekend (calendar) -- force
    # "weekday" so these tests are deterministic whichever day CI runs on.
    monkeypatch.setattr(
        "app.modules.scheduler.health_check.weekend_rest.is_weekend_ist",
        lambda *a, **k: False,
    )


def test_market_data_staleness_no_alert_when_bar_stream_is_still_fresh(
    db: Session, trading_session, monkeypatch
):
    """WS ticks stopped, but the REST-fallback keeps writing `price_bars` —
    a healthy state, must not fire `market_data_stale`.
    """
    _ntp_disk_ok(monkeypatch)
    inst = _seed_nifty(db)
    db.add(
        QuoteTick(
            id=uuid.uuid4(), instrument_id=inst.id, ltp=100.0, bid=99.5, ask=100.5,
            volume=0, oi=None, ts=datetime.now(UTC) - timedelta(seconds=4000),
        )
    )
    db.add(
        PriceBar(
            id=uuid.uuid4(), instrument_id=inst.id, timeframe="60s",
            bucket_start=datetime.now(UTC) - timedelta(seconds=90),
            open=100.0, high=101.0, low=99.0, close=100.5, volume=1000,
        )
    )
    db.flush()

    _scheduler_for(db).run_once()

    assert (
        db.query(SystemAlert)
        .filter(
            SystemAlert.workspace_id == trading_session.workspace_id,
            SystemAlert.category == "market_data_stale",
        )
        .count()
        == 0
    )


def test_market_data_staleness_skipped_on_a_weekend_regardless_of_login(
    db: Session, trading_session, monkeypatch
):
    """On any weekend (calendar, not the awake/dormant state) the staleness
    sub-check is skipped -- NSE is closed, a stale index feed is expected.
    The NTP/disk body of _run_cycle still runs (metrics recorded)."""
    import app.modules.ops.weekend_rest as weekend_rest

    _ntp_disk_ok(monkeypatch)
    monkeypatch.setattr(weekend_rest, "is_weekend_ist", lambda *a, **k: True)
    inst = _seed_nifty(db)
    db.add(
        QuoteTick(
            id=uuid.uuid4(), instrument_id=inst.id, ltp=100.0, bid=99.5, ask=100.5,
            volume=0, oi=None, ts=datetime.now(UTC) - timedelta(seconds=4000),
        )
    )
    db.add(
        PriceBar(
            id=uuid.uuid4(), instrument_id=inst.id, timeframe="60s",
            bucket_start=datetime.now(UTC) - timedelta(seconds=4000),
            open=100.0, high=101.0, low=99.0, close=100.5, volume=1000,
        )
    )
    db.flush()

    _scheduler_for(db).run_once()

    assert (
        db.query(SystemAlert)
        .filter(
            SystemAlert.workspace_id == trading_session.workspace_id,
            SystemAlert.category == "market_data_stale",
        )
        .count()
        == 0
    )
    assert (
        db.query(MetricSeries)
        .filter(MetricSeries.workspace_id == trading_session.workspace_id)
        .count()
        > 0
    )


def test_market_data_staleness_alerts_when_tick_and_bar_are_both_stale(
    db: Session, trading_session, monkeypatch
):
    _ntp_disk_ok(monkeypatch)
    inst = _seed_nifty(db)
    db.add(
        QuoteTick(
            id=uuid.uuid4(), instrument_id=inst.id, ltp=100.0, bid=99.5, ask=100.5,
            volume=0, oi=None, ts=datetime.now(UTC) - timedelta(seconds=4000),
        )
    )
    db.add(
        PriceBar(
            id=uuid.uuid4(), instrument_id=inst.id, timeframe="60s",
            bucket_start=datetime.now(UTC) - timedelta(seconds=4000),
            open=100.0, high=101.0, low=99.0, close=100.5, volume=1000,
        )
    )
    db.flush()

    _scheduler_for(db).run_once()

    assert (
        db.query(SystemAlert)
        .filter(
            SystemAlert.workspace_id == trading_session.workspace_id,
            SystemAlert.category == "market_data_stale",
        )
        .count()
        == 1
    )


def test_db_pool_saturation_alerts_when_checked_out_ratio_is_high(
    db: Session, trading_session, monkeypatch
):
    """2026-08-31: leading indicator for the whole-app-hang incident fixed by
    raising DBSettings.pool_size/max_overflow -- alerts before the pool is
    fully exhausted, not after."""
    import app.modules.scheduler.health_check as hc

    _ntp_disk_ok(monkeypatch)
    monkeypatch.setattr(hc, "pop_lock_wait_stats", lambda: {})
    monkeypatch.setattr(hc.engine.pool, "checkedout", lambda: 35)  # 35/40 = 87.5%

    _scheduler_for(db).run_once()

    alerts = (
        db.query(SystemAlert)
        .filter(
            SystemAlert.workspace_id == trading_session.workspace_id,
            SystemAlert.category == "db_pool_saturated",
        )
        .all()
    )
    assert len(alerts) == 1
    assert "35" in alerts[0].message


def test_db_pool_saturation_no_alert_when_ratio_is_low(
    db: Session, trading_session, monkeypatch
):
    import app.modules.scheduler.health_check as hc

    _ntp_disk_ok(monkeypatch)
    monkeypatch.setattr(hc, "pop_lock_wait_stats", lambda: {})
    monkeypatch.setattr(hc.engine.pool, "checkedout", lambda: 2)

    _scheduler_for(db).run_once()

    assert (
        db.query(SystemAlert)
        .filter(
            SystemAlert.workspace_id == trading_session.workspace_id,
            SystemAlert.category == "db_pool_saturated",
        )
        .count()
        == 0
    )
    # Metric still recorded every cycle regardless -- visibility even when healthy.
    assert (
        db.query(MetricSeries)
        .filter(
            MetricSeries.workspace_id == trading_session.workspace_id,
            MetricSeries.metric_name == "db_pool_checked_out",
        )
        .count()
        == 1
    )


def test_lock_contention_alerts_when_max_wait_exceeds_threshold(
    db: Session, trading_session, monkeypatch
):
    import app.modules.scheduler.health_check as hc
    from app.core.locking import LOCK_EXECUTION_SINGLETON

    _ntp_disk_ok(monkeypatch)
    monkeypatch.setattr(
        hc, "pop_lock_wait_stats", lambda: {LOCK_EXECUTION_SINGLETON: (8.2, 3)}
    )

    _scheduler_for(db).run_once()

    alerts = (
        db.query(SystemAlert)
        .filter(
            SystemAlert.workspace_id == trading_session.workspace_id,
            SystemAlert.category == "lock_contention_high",
        )
        .all()
    )
    assert len(alerts) == 1
    assert LOCK_EXECUTION_SINGLETON in alerts[0].message


def test_lock_contention_records_metric_but_no_alert_below_threshold(
    db: Session, trading_session, monkeypatch
):
    import app.modules.scheduler.health_check as hc
    from app.core.locking import LOCK_EXECUTION_SINGLETON

    _ntp_disk_ok(monkeypatch)
    monkeypatch.setattr(
        hc, "pop_lock_wait_stats", lambda: {LOCK_EXECUTION_SINGLETON: (1.5, 2)}
    )

    _scheduler_for(db).run_once()

    assert (
        db.query(SystemAlert)
        .filter(SystemAlert.category == "lock_contention_high")
        .count()
        == 0
    )
    assert (
        db.query(MetricSeries)
        .filter(
            MetricSeries.workspace_id == trading_session.workspace_id,
            MetricSeries.metric_name == "lock_wait_max_seconds",
        )
        .count()
        == 1
    )


def test_lock_contention_ignores_unmonitored_lock_names(
    db: Session, trading_session, monkeypatch
):
    """Proves the exact filter that keeps a throwaway/unrelated lock name
    (e.g. test_locking.py's own TEST_LOCK_NAME, sharing the same
    process-wide stats dict) from ever surfacing here as a metric or alert.
    """
    import app.modules.scheduler.health_check as hc

    _ntp_disk_ok(monkeypatch)
    monkeypatch.setattr(hc, "pop_lock_wait_stats", lambda: {"some_other_lock": (9.0, 5)})

    _scheduler_for(db).run_once()

    assert (
        db.query(SystemAlert).filter(SystemAlert.category == "lock_contention_high").count()
        == 0
    )
    assert (
        db.query(MetricSeries).filter(MetricSeries.metric_name == "lock_wait_max_seconds").count()
        == 0
    )


def test_lock_hold_time_alerts_when_max_hold_exceeds_threshold(
    db: Session, trading_session, monkeypatch
):
    """2026-08-31: root-cause counterpart to lock_contention_high -- answers
    whether a slow acquire was actually caused by a slow broker call while
    the lock was held. WARNING severity, per _check_lock_hold_time's own
    docstring, so this never reaches Telegram regardless of dedup/window --
    only the SystemAlert row is asserted here.
    """
    import app.modules.scheduler.health_check as hc
    from app.core.locking import LOCK_EXECUTION_SINGLETON

    _ntp_disk_ok(monkeypatch)
    monkeypatch.setattr(hc, "pop_lock_wait_stats", lambda: {})
    monkeypatch.setattr(
        hc, "pop_lock_hold_stats", lambda: {LOCK_EXECUTION_SINGLETON: (6.4, 2)}
    )

    _scheduler_for(db).run_once()

    alerts = (
        db.query(SystemAlert)
        .filter(
            SystemAlert.workspace_id == trading_session.workspace_id,
            SystemAlert.category == "lock_hold_high",
        )
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert LOCK_EXECUTION_SINGLETON in alerts[0].message


def test_lock_hold_time_records_metric_but_no_alert_below_threshold(
    db: Session, trading_session, monkeypatch
):
    import app.modules.scheduler.health_check as hc
    from app.core.locking import LOCK_EXECUTION_SINGLETON

    _ntp_disk_ok(monkeypatch)
    monkeypatch.setattr(hc, "pop_lock_wait_stats", lambda: {})
    monkeypatch.setattr(
        hc, "pop_lock_hold_stats", lambda: {LOCK_EXECUTION_SINGLETON: (1.2, 1)}
    )

    _scheduler_for(db).run_once()

    assert (
        db.query(SystemAlert).filter(SystemAlert.category == "lock_hold_high").count() == 0
    )
    assert (
        db.query(MetricSeries)
        .filter(
            MetricSeries.workspace_id == trading_session.workspace_id,
            MetricSeries.metric_name == "lock_hold_max_seconds",
        )
        .count()
        == 1
    )


def test_lock_hold_time_ignores_unmonitored_lock_names(
    db: Session, trading_session, monkeypatch
):
    """Same filter as test_lock_contention_ignores_unmonitored_lock_names,
    proven independently for the hold-time side of the tracker.
    """
    import app.modules.scheduler.health_check as hc

    _ntp_disk_ok(monkeypatch)
    monkeypatch.setattr(hc, "pop_lock_wait_stats", lambda: {})
    monkeypatch.setattr(hc, "pop_lock_hold_stats", lambda: {"some_other_lock": (9.0, 3)})

    _scheduler_for(db).run_once()

    assert db.query(SystemAlert).filter(SystemAlert.category == "lock_hold_high").count() == 0
    assert (
        db.query(MetricSeries).filter(MetricSeries.metric_name == "lock_hold_max_seconds").count()
        == 0
    )


def test_wait_seconds_is_4x_on_a_weekend_and_normal_on_a_weekday(monkeypatch):
    """The background _loop polls 1/4 as often on weekends; run_once() (what
    every other test drives) is untouched."""
    import app.modules.ops.weekend_rest as weekend_rest
    from app.modules.scheduler.health_check import (
        DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
        WEEKEND_INTERVAL_MULTIPLIER,
    )

    sched = HealthCheckScheduler()

    monkeypatch.setattr(weekend_rest, "is_weekend_ist", lambda *a, **k: False)
    assert sched._wait_seconds() == DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS  # noqa: SLF001

    monkeypatch.setattr(weekend_rest, "is_weekend_ist", lambda *a, **k: True)
    assert sched._wait_seconds() == (  # noqa: SLF001
        DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS * WEEKEND_INTERVAL_MULTIPLIER
    )
