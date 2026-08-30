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
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.db.session import session_scope
from app.domain.market.models import Instrument
from app.domain.market.models import OptionChainSnapshot as OptionChainSnapshotRow
from app.modules.broker_adapter.base.broker_port import BrokerPort
from app.modules.broker_adapter.base.contracts import Tick
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

# "Is the Shoonya feed live right now" for the connection indicator
# (`GET /shoonya/status`) — deliberately looser than TICK_THRESHOLDS so a
# brief hiccup doesn't flap the badge, and split per source: during a WS
# outage the ingestion service auto-falls-back to REST polling, which writes
# `price_bars` (a completed 60s bucket, so its newest row legitimately lags
# ~60-150s even while perfectly healthy) but not `quote_ticks`. Either source
# being fresh means the feed is up.
UI_TICK_FRESH_THRESHOLDS = FreshnessThresholds(
    degraded_after_seconds=30.0, stale_after_seconds=120.0
)
UI_BAR_FRESH_THRESHOLDS = FreshnessThresholds(
    degraded_after_seconds=90.0, stale_after_seconds=210.0
)

# How much a contract's premium may have moved since a price was proposed
# (a Signal/TradeIntent's entry_price) before it's treated as stale — shared
# by the manual-approval re-check and evaluate_trade_intent's AUTO-mode
# equivalent, see check_price_drift / fresh_reference_premium below. 5%
# (raised from 3% on 2026-08-28): option premiums routinely move more than 3%
# cycle-to-cycle without the proposal being meaningfully stale.
PRICE_DRIFT_TOLERANCE_PCT = 0.05


def classify_age(ts: datetime, now: datetime, thresholds: FreshnessThresholds) -> FreshnessState:
    age = max((now - ts).total_seconds(), 0.0)
    if age <= thresholds.degraded_after_seconds:
        return FreshnessState.LIVE
    if age <= thresholds.stale_after_seconds:
        return FreshnessState.DEGRADED
    if age <= DEAD_AFTER_SECONDS:
        return FreshnessState.STALE
    return FreshnessState.DEAD


def fresh_tick_or_none(tick: Tick | None, now: datetime) -> Tick | None:
    """`tick` if it's LIVE/DEGRADED by `TICK_THRESHOLDS`, else `None` -- the
    "is this live feed reading actually usable, or should the caller fall
    back to a REST quote" gate. `execution_engine.paper.service
    .current_contract_price` uses this directly; `PositionManager._live_tick`
    calls `classify_age` itself instead (same threshold, same shared
    primitive) since it also logs *which* freshness state triggered the
    fallback -- this function intentionally doesn't expose that, to keep
    the common case (no logging, just "is it fresh") a one-liner.
    """
    if tick is None:
        return None
    state = classify_age(tick.ts, now, TICK_THRESHOLDS)
    if state in (FreshnessState.LIVE, FreshnessState.DEGRADED):
        return tick
    return None


def _latest_tick_ts(db: Session, instrument_id: uuid.UUID) -> datetime | None:
    from app.domain.market.models import QuoteTick

    latest = (
        db.query(QuoteTick)
        .filter(QuoteTick.instrument_id == instrument_id)
        .order_by(QuoteTick.ts.desc())
        .first()
    )
    return latest.ts if latest is not None else None


def _latest_bar_effective_ts(
    db: Session, instrument_id: uuid.UUID, *, timeframe_seconds: int = 60
) -> datetime | None:
    """A completed bucket represents data through `bucket_start + timeframe`,
    so that sum is the effective "as of" time (`price_bars` has no write
    timestamp column).
    """
    from app.domain.market.models import PriceBar

    latest = (
        db.query(PriceBar)
        .filter(
            PriceBar.instrument_id == instrument_id,
            PriceBar.timeframe == f"{timeframe_seconds}s",
        )
        .order_by(PriceBar.bucket_start.desc())
        .first()
    )
    if latest is None:
        return None
    return latest.bucket_start + timedelta(seconds=timeframe_seconds)


def classify_latest_tick(
    db: Session, instrument_id: uuid.UUID, *, thresholds: FreshnessThresholds = TICK_THRESHOLDS
) -> FreshnessState:
    """Read-only classification of the latest underlying `QuoteTick` — no
    refresh action exists for a stalled tick stream (unlike the option-chain
    case below), so a caller finding this STALE/DEAD is a real ingestion-
    health signal, not something to silently paper over.
    """
    ts = _latest_tick_ts(db, instrument_id)
    if ts is None:
        return FreshnessState.DEAD
    return classify_age(ts, datetime.now(UTC), thresholds)


def classify_latest_bar(
    db: Session,
    instrument_id: uuid.UUID,
    *,
    timeframe_seconds: int = 60,
    thresholds: FreshnessThresholds = TICK_THRESHOLDS,
) -> FreshnessState:
    """Read-only classification of the latest underlying `PriceBar` — the
    second, independent "is data still arriving" signal alongside
    `classify_latest_tick`. `price_bars` are the one thing the WS→REST
    polling fallback (`MarketDataIngestionService._poll_loop`) keeps writing
    when `quote_ticks` have stopped, so a caller that treats a stale tick
    stream as "feed dead" without also checking this would misread a healthy
    REST-fallback period.
    """
    ts = _latest_bar_effective_ts(db, instrument_id, timeframe_seconds=timeframe_seconds)
    if ts is None:
        return FreshnessState.DEAD
    return classify_age(ts, datetime.now(UTC), thresholds)


def underlying_feed_state(
    db: Session,
    instrument_id: uuid.UUID,
    *,
    tick_thresholds: FreshnessThresholds = UI_TICK_FRESH_THRESHOLDS,
    bar_thresholds: FreshnessThresholds = UI_BAR_FRESH_THRESHOLDS,
) -> FreshnessState:
    """The better (fresher) of the tick stream and the REST-fallback bar
    stream for one underlying — either being live means the feed is up.
    """
    return better_of(
        classify_latest_tick(db, instrument_id, thresholds=tick_thresholds),
        classify_latest_bar(db, instrument_id, thresholds=bar_thresholds),
    )


def any_underlying_feed_fresh(db: Session, symbols: tuple[str, ...]) -> bool:
    """`True` if *any* of `symbols` (an `Instrument.symbol`) currently has a
    live/degraded underlying feed. The Shoonya WS connection is shared across
    both underlyings, so one flowing is enough to call the connection up;
    per-underlying staleness is a separate concern handled by the health
    check's `market_data_stale` alert.
    """
    for symbol in symbols:
        instrument = db.query(Instrument).filter(Instrument.symbol == symbol).one_or_none()
        if instrument is None:
            continue
        if underlying_feed_state(db, instrument.id) in (
            FreshnessState.LIVE,
            FreshnessState.DEGRADED,
        ):
            return True
    return False


def underlying_feed_freshness(
    db: Session, symbols: tuple[str, ...]
) -> tuple[float | None, FreshnessState]:
    """The age (seconds) and `FreshnessState` of whichever signal — streamed
    tick or REST-fallback bar, across every symbol in `symbols` — is
    freshest right now. Same "either source, any underlying" logic as
    `any_underlying_feed_fresh`, but returns the actual number for display
    (`GET /shoonya/status`'s feed-latency badge) instead of a boolean.
    `(None, FreshnessState.DEAD)` means no tick/bar exists for any symbol.
    """
    now = datetime.now(UTC)
    best_state = FreshnessState.DEAD
    best_ts: datetime | None = None
    for symbol in symbols:
        instrument = db.query(Instrument).filter(Instrument.symbol == symbol).one_or_none()
        if instrument is None:
            continue
        for ts, thresholds in (
            (_latest_tick_ts(db, instrument.id), UI_TICK_FRESH_THRESHOLDS),
            (_latest_bar_effective_ts(db, instrument.id), UI_BAR_FRESH_THRESHOLDS),
        ):
            if ts is None:
                continue
            state = classify_age(ts, now, thresholds)
            is_better = best_ts is None or _SEVERITY[state] < _SEVERITY[best_state]
            is_tied_but_newer = best_ts is not None and state == best_state and ts > best_ts
            if is_better or is_tied_but_newer:
                best_state, best_ts = state, ts
    if best_ts is None:
        return None, FreshnessState.DEAD
    return max((now - best_ts).total_seconds(), 0.0), best_state


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


def latest_snapshot_tick(
    db: Session, instrument_id: uuid.UUID, expiry_date: date, contract_symbol: str
) -> Tick | None:
    """A specific contract's price from the latest `OptionChainSnapshot` for
    its instrument+expiry -- the same REST-sourced data every strategy's
    `rank_from_latest_snapshot` (`strategy_engine.strike_ranking.engine`)
    already reads to propose a trade, so pricing an already-open position
    against this agrees with whatever number the strategy itself reasoned
    about, instead of an independent source (see
    `execution_engine.paper.service.current_contract_price`, the caller
    this exists for).

    `None` if no snapshot exists yet for this instrument+expiry, or this
    contract isn't in it (chain data for a contract that expired/rolled off
    between snapshots, say) -- a real gap the caller must have its own
    further fallback for, never silently trade on nothing.
    """
    snapshot = _latest_chain_snapshot(db, instrument_id, expiry_date)
    if snapshot is None:
        return None
    entries: list[dict] = snapshot.chain_data or []  # type: ignore[assignment]
    for entry in entries:
        if entry.get("contract_symbol") == contract_symbol:
            return Tick(
                contract_symbol=contract_symbol,
                ltp=float(entry.get("ltp", 0.0) or 0.0),
                bid=float(entry.get("bid", 0.0) or 0.0),
                ask=float(entry.get("ask", 0.0) or 0.0),
                volume=int(entry.get("volume", 0) or 0),
                oi=entry.get("oi"),
                ts=snapshot.ts,
            )
    return None


def _snapshot_has_live_prices(snapshot: OptionChainSnapshotRow) -> bool:
    """A snapshot can be time-fresh (just written) while still being useless
    for trading — e.g. a broker entitlement/connectivity problem that leaves
    every strike's `ltp` at 0. Age alone can't catch that; this checks the
    data actually has *something* real in it.
    """
    entries: list[dict] = snapshot.chain_data or []  # type: ignore[assignment]
    return any(float(entry.get("ltp", 0) or 0) > 0 for entry in entries)


def classify_option_chain(
    db: Session,
    instrument_id: uuid.UUID,
    expiry_date: date,
    *,
    thresholds: FreshnessThresholds = OPTION_CHAIN_THRESHOLDS,
) -> FreshnessState:
    """Read-only classification, no refresh — used for display (e.g.
    `GET /strategies/running`) where a broker call would be inappropriate.

    A time-fresh snapshot whose every strike still shows zero live price
    data is forced to DEAD regardless of age — the point of this whole
    module is "don't trade on data we can't vouch for," and an all-zero
    chain can't be vouched for no matter how recently it was written. This
    is what actually makes `ensure_fresh_option_chain`'s post-refresh
    classification (and `StrategyRunner.run_cycle`'s existing "skip this
    cycle on STALE/DEAD" handling) halt trading rather than proceed on
    prices that were never real.
    """
    latest = _latest_chain_snapshot(db, instrument_id, expiry_date)
    if latest is None:
        return FreshnessState.DEAD
    state = classify_age(latest.ts, datetime.now(UTC), thresholds)
    if state != FreshnessState.DEAD and not _snapshot_has_live_prices(latest):
        logger.warning(
            "option-chain snapshot for instrument %s expiry %s is time-fresh "
            "(age classification %s) but every strike shows zero live price "
            "data — treating as dead rather than trading blind",
            instrument_id,
            expiry_date,
            state.value,
        )
        return FreshnessState.DEAD
    return state


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


def better_of(a: FreshnessState, b: FreshnessState) -> FreshnessState:
    return a if _SEVERITY[a] <= _SEVERITY[b] else b


def check_price_drift(latest_ltp: float, reference_price: float, *, tolerance_pct: float) -> bool:
    """True if `latest_ltp` has moved more than `tolerance_pct` away from
    `reference_price` — shared by the manual-approval staleness check
    (`api.v1.strategies.approve_trade_approval`) and, newly,
    `risk_engine.evaluate_trade_intent`'s AUTO-mode equivalent.
    """
    if reference_price == 0:
        return False
    return abs(latest_ltp - reference_price) / reference_price > tolerance_pct


def fresh_reference_premium(
    db: Session,
    *,
    option_contract_id: uuid.UUID,
    instrument_id: uuid.UUID,
    expiry_date: date,
    contract_symbol: str,
    now: datetime | None = None,
) -> float | None:
    """The freshest *usable* premium for a contract, to re-validate a proposed
    `entry_price` against — resolved in priority order:

    1. The latest streamed `QuoteTick` for the contract, **if fresh**
       (`classify_age` LIVE/DEGRADED by `TICK_THRESHOLDS`, i.e. ≤ 60s) and
       `ltp > 0` — the continuous WS feed, used only while it's actually
       flowing (a contract with an open position, or one subscribed for
       another reason).
    2. Else the latest `OptionChainSnapshot` LTP for the contract via
       `latest_snapshot_tick`, **if fresh** (LIVE/DEGRADED by
       `OPTION_CHAIN_THRESHOLDS`, ≤ 600s) and `ltp > 0` — always available,
       continuously refreshed each run cycle by `ensure_fresh_option_chain`,
       the same source the strategy proposed from.
    3. Else `None` — no fresh reference exists; the caller must **skip** the
       drift check, never fabricate a rejection from stale data (the
       2026-08-28 bug: a 4-hour-old per-contract tick read as a 37% "drift").
       Proposal-time staleness is already gated upstream by
       `ensure_fresh_option_chain`.
    """
    from app.domain.market.models import QuoteTick

    now = now or datetime.now(UTC)

    tick = (
        db.query(QuoteTick)
        .filter(QuoteTick.option_contract_id == option_contract_id)
        .order_by(QuoteTick.ts.desc())
        .first()
    )
    if (
        tick is not None
        and float(tick.ltp) > 0
        and classify_age(tick.ts, now, TICK_THRESHOLDS)
        in (FreshnessState.LIVE, FreshnessState.DEGRADED)
    ):
        return float(tick.ltp)

    snapshot_tick = latest_snapshot_tick(db, instrument_id, expiry_date, contract_symbol)
    if (
        snapshot_tick is not None
        and snapshot_tick.ltp > 0
        and classify_age(snapshot_tick.ts, now, OPTION_CHAIN_THRESHOLDS)
        in (FreshnessState.LIVE, FreshnessState.DEGRADED)
    ):
        return snapshot_tick.ltp

    return None
