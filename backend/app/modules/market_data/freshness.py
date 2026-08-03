"""Quote/option-chain freshness. Strategies never call the broker live — every
cycle reads whatever `QuoteTick`/`OptionChainSnapshot` row happens to be
"latest" in the DB at evaluation time (see `strike_ranking.engine`'s own
docstring). That's fine for continuously-streamed ticks, but was a real,
previously-flagged gap for `OptionChainSnapshot`: `record_option_chain_snapshot`
is otherwise only ever called once, at `start_strategy` time — this module is
what actually closes that gap (a refresh, not just a block), plus a shared
price-drift check generalizing the one used only by the manual-approval path
today.
"""

from __future__ import annotations

import enum
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.core.db.session import session_scope
from app.domain.market.models import Instrument
from app.domain.market.models import OptionChainSnapshot as OptionChainSnapshotRow
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.base.errors import BrokerError
from app.modules.market_data.ingestion import SessionFactory, record_option_chain_snapshot

logger = logging.getLogger("app.market_data.freshness")


class FreshnessState(enum.StrEnum):
    LIVE = "live"
    DEGRADED = "degraded"
    STALE = "stale"
    DEAD = "dead"


@dataclass(frozen=True)
class FreshnessThresholds:
    degraded_after_seconds: float
    stale_after_seconds: float


# Continuously-streamed QuoteTicks should be fresh within a handful of
# seconds under normal ingestion; option-chain snapshots are point-in-time
# pictures refreshed on demand rather than per-tick, so their thresholds are
# far more generous by design, not because staleness matters less there.
TICK_THRESHOLDS = FreshnessThresholds(degraded_after_seconds=10.0, stale_after_seconds=60.0)
OPTION_CHAIN_THRESHOLDS = FreshnessThresholds(
    degraded_after_seconds=120.0, stale_after_seconds=600.0
)

# Beyond this, data has no useful age left to reason about at all.
DEAD_AFTER_SECONDS = 3600.0

# How much the underlying's premium may have moved since a price was proposed
# (a Signal/TradeIntent's entry_price) before it's treated as stale — shared
# by the manual-approval re-check and evaluate_trade_intent's AUTO-mode
# equivalent, see check_price_drift below.
PRICE_DRIFT_TOLERANCE_PCT = 0.03


def classify_age(ts: datetime, now: datetime, thresholds: FreshnessThresholds) -> FreshnessState:
    age = max((now - ts).total_seconds(), 0.0)
    if age <= thresholds.degraded_after_seconds:
        return FreshnessState.LIVE
    if age <= thresholds.stale_after_seconds:
        return FreshnessState.DEGRADED
    if age <= DEAD_AFTER_SECONDS:
        return FreshnessState.STALE
    return FreshnessState.DEAD


def classify_latest_tick(
    db: Session, instrument_id: uuid.UUID, *, thresholds: FreshnessThresholds = TICK_THRESHOLDS
) -> FreshnessState:
    """Read-only classification of the latest underlying `QuoteTick` — no
    refresh action exists for a stalled tick stream (unlike the option-chain
    case below), so a caller finding this STALE/DEAD is a real ingestion-
    health signal, not something to silently paper over.
    """
    from app.domain.market.models import QuoteTick

    latest = (
        db.query(QuoteTick)
        .filter(QuoteTick.instrument_id == instrument_id)
        .order_by(QuoteTick.ts.desc())
        .first()
    )
    if latest is None:
        return FreshnessState.DEAD
    return classify_age(latest.ts, datetime.now(UTC), thresholds)


def _latest_chain_snapshot(
    db: Session, instrument_id: uuid.UUID, expiry_date: date
) -> OptionChainSnapshotRow | None:
    return (
        db.query(OptionChainSnapshotRow)
        .filter(
            OptionChainSnapshotRow.instrument_id == instrument_id,
            OptionChainSnapshotRow.expiry_date == expiry_date,
        )
        .order_by(OptionChainSnapshotRow.ts.desc())
        .first()
    )


def classify_option_chain(
    db: Session,
    instrument_id: uuid.UUID,
    expiry_date: date,
    *,
    thresholds: FreshnessThresholds = OPTION_CHAIN_THRESHOLDS,
) -> FreshnessState:
    """Read-only classification, no refresh — used for display (e.g.
    `GET /strategies/running`) where a broker call would be inappropriate.
    """
    latest = _latest_chain_snapshot(db, instrument_id, expiry_date)
    if latest is None:
        return FreshnessState.DEAD
    return classify_age(latest.ts, datetime.now(UTC), thresholds)


def ensure_fresh_option_chain(
    db: Session,
    broker: BrokerPort,
    instrument_id: uuid.UUID,
    expiry_date: date,
    *,
    thresholds: FreshnessThresholds = OPTION_CHAIN_THRESHOLDS,
    session_factory: SessionFactory | None = None,
) -> FreshnessState:
    """Classifies the latest `OptionChainSnapshot`; if it's anything but
    LIVE, refreshes it once via the existing `record_option_chain_snapshot`
    (the same call `start_strategy` already makes) rather than only
    reporting staleness — this is what actually fixes the "only ever
    snapshotted once per run" gap. A broker failure during refresh is
    reported as DEAD, never raised — callers (`StrategyRunner`) already
    catch-and-log-per-cycle same as every other background poller here.

    `session_factory`, when given, is threaded through to
    `record_option_chain_snapshot` instead of its own default — lets a
    caller that already has an open transaction (`StrategyRunner.run_cycle`)
    make the refresh part of that same transaction rather than a second,
    independently-committing connection: more correct (atomic with the rest
    of the cycle) and avoids the refresh being unable to see a row the
    caller's own transaction added but hasn't committed yet.
    """
    state = classify_option_chain(db, instrument_id, expiry_date, thresholds=thresholds)
    if state == FreshnessState.LIVE:
        return state

    instrument = db.get(Instrument, instrument_id)
    if instrument is None:
        return FreshnessState.DEAD
    try:
        record_option_chain_snapshot(
            instrument_id,
            broker,
            instrument.symbol,
            expiry_date,
            session_factory=session_factory or session_scope,
        )
    except BrokerError:
        logger.warning(
            "option-chain refresh failed for instrument %s expiry %s", instrument_id, expiry_date
        )
        return FreshnessState.DEAD

    return classify_option_chain(db, instrument_id, expiry_date, thresholds=thresholds)


_SEVERITY = {
    FreshnessState.LIVE: 0,
    FreshnessState.DEGRADED: 1,
    FreshnessState.STALE: 2,
    FreshnessState.DEAD: 3,
}


def worse_of(a: FreshnessState, b: FreshnessState) -> FreshnessState:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def check_price_drift(latest_ltp: float, reference_price: float, *, tolerance_pct: float) -> bool:
    """True if `latest_ltp` has moved more than `tolerance_pct` away from
    `reference_price` — shared by the manual-approval staleness check
    (`api.v1.strategies.approve_trade_approval`) and, newly,
    `risk_engine.evaluate_trade_intent`'s AUTO-mode equivalent.
    """
    if reference_price == 0:
        return False
    return abs(latest_ltp - reference_price) / reference_price > tolerance_pct
