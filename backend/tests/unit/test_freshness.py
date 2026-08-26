from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.market.models import OptionChainSnapshot as OptionChainSnapshotRow
from app.modules.broker_adapter.base.contracts import Tick
from app.modules.market_data.freshness import (
    FreshnessState,
    FreshnessThresholds,
    _snapshot_has_live_prices,
    check_price_drift,
    classify_age,
    fresh_tick_or_none,
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


def _tick(seconds_ago: float) -> Tick:
    return Tick(
        contract_symbol="NIFTY", ltp=100.0, bid=99.5, ask=100.5, volume=0, oi=None,
        ts=_ts_ago(seconds_ago),
    )


def test_fresh_tick_or_none_returns_the_tick_when_live():
    tick = _tick(5)
    assert fresh_tick_or_none(tick, datetime.now(UTC)) is tick


def test_fresh_tick_or_none_returns_the_tick_when_degraded():
    tick = _tick(15)
    assert fresh_tick_or_none(tick, datetime.now(UTC)) is tick


def test_fresh_tick_or_none_returns_none_when_stale():
    assert fresh_tick_or_none(_tick(120), datetime.now(UTC)) is None


def test_fresh_tick_or_none_returns_none_when_tick_is_none():
    assert fresh_tick_or_none(None, datetime.now(UTC)) is None


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


def _snapshot(chain_data: list[dict]) -> OptionChainSnapshotRow:
    return OptionChainSnapshotRow(chain_data=chain_data)


def test_snapshot_has_live_prices_true_when_any_entry_has_nonzero_ltp():
    snapshot = _snapshot(
        [{"ltp": 0.0, "strike": 100.0}, {"ltp": 142.35, "strike": 105.0}]
    )
    assert _snapshot_has_live_prices(snapshot) is True


def test_snapshot_has_live_prices_false_when_every_entry_is_zero():
    """The real, live-observed symptom this guards against: a
    structurally-valid GetOptionChain response (real strikes/symbols) where
    every strike's live quote came back zero — e.g. a broker entitlement or
    connectivity gap, not a genuinely quiet market.
    """
    snapshot = _snapshot([{"ltp": 0.0, "strike": 100.0}, {"ltp": 0.0, "strike": 105.0}])
    assert _snapshot_has_live_prices(snapshot) is False


def test_snapshot_has_live_prices_false_when_chain_is_empty():
    assert _snapshot_has_live_prices(_snapshot([])) is False
