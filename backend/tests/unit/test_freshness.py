from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.modules.market_data.freshness import (
    FreshnessState,
    FreshnessThresholds,
    check_price_drift,
    classify_age,
    worse_of,
)

_THRESHOLDS = FreshnessThresholds(degraded_after_seconds=10.0, stale_after_seconds=60.0)


def _ts_ago(seconds: float) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


def test_classify_age_live_within_degraded_threshold():
    assert classify_age(_ts_ago(5), datetime.now(UTC), _THRESHOLDS) == FreshnessState.LIVE


def test_classify_age_degraded_between_thresholds():
    assert classify_age(_ts_ago(30), datetime.now(UTC), _THRESHOLDS) == FreshnessState.DEGRADED


def test_classify_age_stale_past_stale_threshold():
    assert classify_age(_ts_ago(120), datetime.now(UTC), _THRESHOLDS) == FreshnessState.STALE


def test_classify_age_dead_past_dead_ceiling():
    assert classify_age(_ts_ago(4000), datetime.now(UTC), _THRESHOLDS) == FreshnessState.DEAD


def test_classify_age_future_timestamp_treated_as_live():
    # Clock skew between writer/reader shouldn't itself be a staleness signal.
    assert classify_age(_ts_ago(-5), datetime.now(UTC), _THRESHOLDS) == FreshnessState.LIVE


def test_worse_of_picks_more_severe_state():
    assert worse_of(FreshnessState.LIVE, FreshnessState.STALE) == FreshnessState.STALE
    assert worse_of(FreshnessState.DEAD, FreshnessState.LIVE) == FreshnessState.DEAD
    assert worse_of(FreshnessState.DEGRADED, FreshnessState.DEGRADED) == FreshnessState.DEGRADED


def test_check_price_drift_within_tolerance_is_false():
    assert check_price_drift(101.0, 100.0, tolerance_pct=0.03) is False


def test_check_price_drift_beyond_tolerance_is_true():
    assert check_price_drift(110.0, 100.0, tolerance_pct=0.03) is True


def test_check_price_drift_zero_reference_price_never_drifts():
    assert check_price_drift(50.0, 0.0, tolerance_pct=0.03) is False
