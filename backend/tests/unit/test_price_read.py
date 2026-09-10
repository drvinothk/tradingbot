"""`PriceRead.is_actionable` truth table — the single predicate every
execution-path price consumer keys its decision on."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.modules.broker_adapter.base.contracts import Tick
from app.modules.market_data.freshness import FreshnessState
from app.modules.market_data.price_read import PriceRead, PriceSource


def _tick() -> Tick:
    return Tick(
        contract_symbol="NIFTY26JUL22000CE",
        ltp=88.0, bid=87.5, ask=88.5, volume=1000, oi=5000, ts=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("source", "freshness", "plausible", "has_tick", "expected"),
    [
        (PriceSource.LIVE_FEED, FreshnessState.LIVE, True, True, True),
        (PriceSource.LIVE_FEED, FreshnessState.DEGRADED, True, True, True),
        (PriceSource.CHAIN_SNAPSHOT, FreshnessState.DEGRADED, True, True, True),
        (PriceSource.BROKER_QUOTE, FreshnessState.LIVE, True, True, True),
        # STALE / DEAD are labelled-but-not-actionable
        (PriceSource.CHAIN_SNAPSHOT, FreshnessState.STALE, True, True, False),
        (PriceSource.CHAIN_SNAPSHOT, FreshnessState.DEAD, True, True, False),
        # implausible never actionable regardless of freshness
        (PriceSource.LIVE_FEED, FreshnessState.LIVE, False, True, False),
        # NONE: no tick at all
        (PriceSource.NONE, FreshnessState.DEAD, False, False, False),
    ],
)
def test_is_actionable_truth_table(source, freshness, plausible, has_tick, expected):
    read = PriceRead(
        tick=_tick() if has_tick else None,
        source=source,
        freshness=freshness,
        plausible=plausible,
    )
    assert read.is_actionable is expected


def test_none_factory():
    read = PriceRead.none()
    assert read.tick is None
    assert read.source is PriceSource.NONE
    assert read.freshness is FreshnessState.DEAD
    assert read.plausible is False
    assert read.is_actionable is False
    assert read.ltp_or_none is None


def test_ltp_or_none_returns_the_price_when_present():
    read = PriceRead(_tick(), PriceSource.LIVE_FEED, FreshnessState.LIVE, plausible=True)
    assert read.ltp_or_none == 88.0
