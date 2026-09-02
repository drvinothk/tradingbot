"""Market-data provider preference — Ops-Hardening Phase 4. A manual
override on top of `FailoverMarketDataProvider`'s own automatic health-based
switching, not a replacement for it — see `MarketDataProviderPreference`'s
own docstring (`app.domain.ops.models`) and `FailoverMarketDataProvider
.set_manual_override`'s own docstring for the full design.

Gated behind `session.start`, matching `api.v1.shoonya`'s own broker-
connectivity endpoints (login/status/connect) — this is the same "who
manages which feed is live" concern, not a risk-governance action.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.clock import now_ist
from app.core.db.session import get_db
from app.core.security.rbac import require_permission
from app.domain.audit.models import ActorType, EventCategory
from app.domain.identity.models import User
from app.domain.market.models import OptionChainSnapshot, PriceBar
from app.domain.ops.models import MarketDataProviderPreference
from app.modules.audit_service.service import record_event
from app.modules.market_data import diagnostic_session
from app.modules.market_data import registry as market_data_registry
from app.modules.market_data.provider_composition import get_market_data_provider
from app.modules.market_data.providers.failover import FailoverMarketDataProvider

logger = logging.getLogger("app.api.market_data")

router = APIRouter(prefix="/market-data", tags=["market-data"])

# Matches provider_composition._RECOGNIZED_FAILOVER_BACKUPS's own scope --
# TrueData isn't a supported failover leg yet, "mock" is never a real
# provider to force, and a provider can't be its own alternative.
# "angel_one" archived 2026-08-21 (kept aside pending an IP/proxy fix, see
# CLAUDE.md's Angel One section) -- deliberately not removed from
# provider_composition._RECOGNIZED_FAILOVER_BACKUPS or AngelOneSettings,
# only from this UI-driven override list, so reactivating it later is a
# one-line env-var change (MARKET_DATA_FAILOVER_BACKUP_PROVIDER=angel_one),
# not a code change -- same "explicit config, not a casual dropdown click"
# reasoning as MARKET_DATA_ALLOW_OFFHOURS_TESTING. "alice_blue" added
# 2026-08-25 once promoted to the real failover backup for Shoonya (see
# provider_composition._RECOGNIZED_FAILOVER_BACKUPS's own comment) --
# "shoonya" stays listed too so a user can force back to primary manually
# without waiting out the automatic recovery stabilization window.
RECOGNIZED_OVERRIDE_PROVIDERS = ("shoonya", "alice_blue")


class ProviderPreferenceOut(BaseModel):
    active_provider: str | None
    # What FailoverMarketDataProvider is actually doing right now, if a
    # live failover-wrapped singleton exists -- distinct from
    # active_provider (the persisted preference) since the two can
    # legitimately disagree (e.g. failover isn't currently active, or the
    # preference was just saved but not yet applied to a running process).
    live_active_leg: str | None


class SetProviderPreferenceRequest(BaseModel):
    active_provider: str | None  # None clears the override


def _find_failover_provider() -> FailoverMarketDataProvider | None:
    provider = get_market_data_provider()
    inner = getattr(provider, "_inner", provider)
    return inner if isinstance(inner, FailoverMarketDataProvider) else None


@router.get("/provider-preference", response_model=ProviderPreferenceOut)
def get_provider_preference(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.start")),
) -> ProviderPreferenceOut:
    pref = (
        db.query(MarketDataProviderPreference)
        .filter(MarketDataProviderPreference.workspace_id == user.workspace_id)
        .one_or_none()
    )
    failover = _find_failover_provider()
    return ProviderPreferenceOut(
        active_provider=pref.active_provider if pref is not None else None,
        live_active_leg=failover.active_provider_name if failover is not None else None,
    )


class UnderlyingFeedTelemetryOut(BaseModel):
    symbol: str
    feed_age_seconds: float | None
    feed_state: str
    # Latest persisted `indicator_snapshots` values for this symbol --
    # computed continuously by ingestion for every subscribed instrument
    # (independent of whether any strategy is currently running), so these
    # populate for INDIA VIX too, not just the two tradable underlyings.
    # `None` before the first bar/tick has warmed the calculator up.
    rsi14: float | None = None
    ema9: float | None = None
    ema20: float | None = None
    vwap: float | None = None
    # Put/call ratio against the nearest tradable expiry's latest
    # option-chain snapshot -- `None` for INDIA VIX (no option chain) and for
    # a tradable underlying with no snapshot captured yet today (nothing has
    # scanned it -- see get_market_data_telemetry's own docstring). Unlike
    # feed_age_seconds above (continuous tick freshness), pcr_age_seconds is
    # "how old is this snapshot" -- option chains are refreshed on demand,
    # not per-tick, so a present PCR value can honestly be a while old.
    pcr_oi: float | None = None
    pcr_vol: float | None = None
    pcr_age_seconds: float | None = None


class VolumeProxySymbolTelemetryOut(BaseModel):
    # A "calculated"/derived symbol — genuinely subscribed on the wire but
    # never persisted to price_bars/indicator_snapshots, so it's otherwise
    # invisible here. Today this is only ever the Shoonya front-month future
    # whose volume is spliced onto NIFTY/BANKNIFTY's own index ticks (see
    # `ShoonyaWSClient.set_volume_proxy`'s own docstring) -- empty for any
    # other provider or before the first subscribe has run.
    target_symbol: str | None
    source_symbol: str | None
    subscribed: bool
    last_price: float | None
    last_cum_volume: float | None


class MarketDataTelemetryOut(BaseModel):
    underlyings: list[UnderlyingFeedTelemetryOut]
    calculated_symbols: list[VolumeProxySymbolTelemetryOut] = []


def _latest_indicator_values(db: Session, instrument_id: uuid.UUID) -> dict[str, float]:
    from app.domain.market.models import IndicatorSnapshot

    values: dict[str, float] = {}
    for name in ("RSI14", "EMA9", "EMA20", "VWAP"):
        row = (
            db.query(IndicatorSnapshot)
            .filter(
                IndicatorSnapshot.instrument_id == instrument_id,
                IndicatorSnapshot.indicator_name == name,
            )
            .order_by(IndicatorSnapshot.ts.desc())
            .first()
        )
        if row is not None:
            values[name] = float(row.value)
    return values


@router.get("/telemetry", response_model=MarketDataTelemetryOut)
def get_market_data_telemetry(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.start")),
) -> MarketDataTelemetryOut:
    """2026-08-30, activating the Advanced page's telemetry card. Reuses
    `market_data.freshness.underlying_feed_freshness` -- the exact same
    function `GET /shoonya/status`'s `feed_age_seconds`/`feed_state`
    already calls -- just per-underlying instead of that endpoint's
    best-of-both-underlyings single value, so NIFTY and BANKNIFTY staleness
    are distinguishable here. No new tables, no new background service:
    purely a read over `quote_ticks`/`price_bars`/`indicator_snapshots`/
    `option_chain_snapshots`. Active-provider/live-active-leg is
    deliberately NOT duplicated here -- the frontend already fetches that
    from `GET /market-data/provider-preference` (used by the Global settings
    card) and shares the cache.

    2026-09-01: extended to also list INDIA VIX (genuinely streamed --
    `market_hours.ENV_METRIC_SYMBOLS` -- but never shown here before) and
    the computed RSI14/EMA9/EMA20/VWAP/PCR values every row already has
    persisted somewhere, even though no strategy gates on most of them
    today. VIX deliberately uses `vix_feed_freshness`, not
    `underlying_feed_freshness` -- see that function's own docstring for why
    reusing the tradable-underlying thresholds would show VIX as permanently
    stale by design, not as a real problem.

    2026-09-02: added `calculated_symbols` -- the `underlyings` list above
    is still a hardcoded (TRADABLE_UNDERLYINGS + ENV_METRIC_SYMBOLS) set,
    not literally "every genuinely streamed symbol" despite this endpoint's
    own frontend copy claiming that; the Shoonya VWAP volume-proxy fix
    (2026-08-27) subscribes a real front-month future per underlying that
    never reaches `price_bars`/`indicator_snapshots` since it's cache-only
    (never dispatched), so it was invisible here. Read directly off the
    live `ShoonyaWSClient`'s in-memory state via
    `ShoonyaBrokerAdapter.get_volume_proxy_symbols` -- best-effort, wrapped
    in try/except so a resolution hiccup can never break the rest of this
    response; empty for any non-Shoonya provider or before the first
    subscribe has run.
    """
    from app.domain.market.models import Instrument
    from app.modules.market_data.freshness import (
        FreshnessState,
        underlying_feed_freshness,
        vix_feed_freshness,
    )
    from app.modules.market_data.market_hours import ENV_METRIC_SYMBOLS, TRADABLE_UNDERLYINGS
    from app.modules.strategy_engine.auto_spawner import resolve_nearest_expiry
    from app.modules.strategy_engine.env_metrics import compute_pcr

    now = datetime.now(UTC)
    today = now_ist().date()
    underlyings = []

    for symbol in TRADABLE_UNDERLYINGS:
        age, state = underlying_feed_freshness(db, (symbol,))
        instrument = db.query(Instrument).filter(Instrument.symbol == symbol).one_or_none()
        indicators = _latest_indicator_values(db, instrument.id) if instrument is not None else {}

        pcr_oi = pcr_vol = pcr_age_seconds = None
        if instrument is not None:
            expiry = resolve_nearest_expiry(db, instrument.id, today)
            if expiry is not None:
                snapshot = (
                    db.query(OptionChainSnapshot)
                    .filter(
                        OptionChainSnapshot.instrument_id == instrument.id,
                        OptionChainSnapshot.expiry_date == expiry,
                    )
                    .order_by(OptionChainSnapshot.ts.desc())
                    .first()
                )
                if snapshot is not None:
                    pcr_oi, pcr_vol = compute_pcr(snapshot.chain_data)  # type: ignore[arg-type]
                    pcr_age_seconds = max((now - snapshot.ts).total_seconds(), 0.0)

        underlyings.append(
            UnderlyingFeedTelemetryOut(
                symbol=symbol,
                feed_age_seconds=age,
                feed_state=state.value,
                rsi14=indicators.get("RSI14"),
                ema9=indicators.get("EMA9"),
                ema20=indicators.get("EMA20"),
                vwap=indicators.get("VWAP"),
                pcr_oi=pcr_oi,
                pcr_vol=pcr_vol,
                pcr_age_seconds=pcr_age_seconds,
            )
        )

    for symbol in ENV_METRIC_SYMBOLS:
        instrument = db.query(Instrument).filter(Instrument.symbol == symbol).one_or_none()
        if instrument is None:
            age, state = None, FreshnessState.DEAD
        else:
            age, state = vix_feed_freshness(db, instrument.id)
        indicators = _latest_indicator_values(db, instrument.id) if instrument is not None else {}
        underlyings.append(
            UnderlyingFeedTelemetryOut(
                symbol=symbol,
                feed_age_seconds=age,
                feed_state=state.value,
                rsi14=indicators.get("RSI14"),
                ema9=indicators.get("EMA9"),
                ema20=indicators.get("EMA20"),
                vwap=indicators.get("VWAP"),
            )
        )

    calculated_symbols: list[VolumeProxySymbolTelemetryOut] = []
    try:
        from app.modules.broker_adapter.composition import get_broker, unwrap_broker
        from app.modules.broker_adapter.shoonya.adapter import ShoonyaBrokerAdapter

        inner = unwrap_broker(get_broker())
        if isinstance(inner, ShoonyaBrokerAdapter):
            calculated_symbols = [
                VolumeProxySymbolTelemetryOut(**row) for row in inner.get_volume_proxy_symbols()
            ]
    except Exception:
        logger.warning(
            "Failed to read volume-proxy telemetry; omitting from response", exc_info=True
        )

    return MarketDataTelemetryOut(underlyings=underlyings, calculated_symbols=calculated_symbols)


@router.patch("/provider-preference", response_model=ProviderPreferenceOut)
def set_provider_preference(
    body: SetProviderPreferenceRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("session.start")),
) -> ProviderPreferenceOut:
    """Persists the preference first, then applies it live if a
    failover-wrapped singleton currently exists -- if not (failover
    disabled, or provider is "mock"), the preference is saved and will only
    take effect the next time failover *is* active; that distinction is
    logged clearly rather than silently no-op'd, so it isn't mistaken for
    the toggle simply not having worked.
    """
    if (
        body.active_provider is not None
        and body.active_provider not in RECOGNIZED_OVERRIDE_PROVIDERS
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"active_provider must be one of {RECOGNIZED_OVERRIDE_PROVIDERS} or null "
            "to clear the override",
        )

    pref = (
        db.query(MarketDataProviderPreference)
        .filter(MarketDataProviderPreference.workspace_id == user.workspace_id)
        .one_or_none()
    )
    previous = pref.active_provider if pref is not None else None
    if pref is None:
        pref = MarketDataProviderPreference(
            workspace_id=user.workspace_id, active_provider=body.active_provider
        )
        db.add(pref)
    else:
        pref.active_provider = body.active_provider
    db.flush()

    record_event(
        db,
        workspace_id=user.workspace_id,
        actor_type=ActorType.USER,
        actor_id=user.id,
        event_category=EventCategory.MARKET_DATA_CONNECTIVITY,
        event_type="market_data_provider_preference.updated",
        entity_type="market_data_provider_preference",
        entity_id=pref.id,
        payload={"previous": previous, "new": body.active_provider},
    )

    failover = _find_failover_provider()
    live_active_leg: str | None = None
    if failover is not None:
        try:
            failover.set_manual_override(body.active_provider)
            live_active_leg = failover.active_provider_name
        except (ValueError, RuntimeError) as exc:
            db.commit()  # the preference itself is still valid and saved
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    else:
        logger.warning(
            "Market-data provider preference saved (%r) but no live failover-wrapped "
            "provider exists right now (failover disabled, or provider is mock) -- "
            "will take effect next time failover is active, not immediately.",
            body.active_provider,
        )

    db.commit()
    db.refresh(pref)
    return ProviderPreferenceOut(
        active_provider=pref.active_provider, live_active_leg=live_active_leg
    )


# ---------- WS quality diagnostic (Market Terminal "Test Default"/"Test
# Failback"/"Both") -- see diagnostic_session.py's own module docstring for
# the full design, especially why "default"/"failback" never name a broker.

_DIAGNOSTIC_ROLES = ("default", "failback")


class DiagnosticModeRequest(BaseModel):
    mode: str  # "default" | "failback" | "both"


def _roles_for_mode(mode: str) -> list[str]:
    if mode == "both":
        return list(_DIAGNOSTIC_ROLES)
    if mode in _DIAGNOSTIC_ROLES:
        return [mode]
    raise HTTPException(
        status.HTTP_400_BAD_REQUEST, "mode must be one of ('default', 'failback', 'both')"
    )


@router.post("/diagnostic/start")
def start_diagnostic(
    body: DiagnosticModeRequest,
    user: User = Depends(require_permission("session.start")),
) -> dict:
    """`start_many` validates every requested role before starting any of
    them — see its own docstring for why "both" mode must be atomic (an
    error response must mean nothing started, not "half of it did").
    """
    try:
        return diagnostic_session.start_many(_roles_for_mode(body.mode), user.workspace_id)
    except (RuntimeError, diagnostic_session.UnsupportedFailbackProviderError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/diagnostic/stop")
def stop_diagnostic(
    body: DiagnosticModeRequest,
    user: User = Depends(require_permission("session.start")),
) -> dict:
    return {role: diagnostic_session.stop(role) for role in _roles_for_mode(body.mode)}


@router.get("/diagnostic/status")
def get_diagnostic_status(user: User = Depends(require_permission("session.start"))) -> dict:
    return diagnostic_session.status()


class CandleOut(BaseModel):
    bucket_start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    # Serialized straight from PriceBar ORM rows below (Decimal-backed
    # Numeric columns coerce to float fine under Pydantic's from_attributes
    # validation) -- same pattern StrategyConfigOut already uses.
    model_config = {"from_attributes": True}


class StreamingSymbolsOut(BaseModel):
    symbols: list[str]


# Market Terminal's own-data live chart (2026-08-30) — read-only reads
# against price_bars, gated on strategy.view (matching /strategies/running
# and /instruments's own convention for "view live trading state") rather
# than this router's own session.start default, which is scoped to who
# manages broker/provider connectivity, a different concern.
@router.get("/candles", response_model=list[CandleOut])
def get_candles(
    instrument_id: uuid.UUID,
    # "60s" -- strategy_engine.common_rules.BAR_TIMEFRAME, the only
    # timeframe anything in this codebase persists (see that module's own
    # comment). Not imported from there directly -- this API layer
    # shouldn't depend on strategy_engine for a plain string literal.
    timeframe: str = "60s",
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.view")),
) -> list[PriceBar]:
    """Most recent `limit` completed bars for `instrument_id`/`timeframe`,
    chronological order. `price_bars` only accumulates while
    `MarketDataIngestionService` is actually running for this instrument
    (see `market_data.registry`'s own docstring) — an instrument with no
    strategy currently scanning it returns an empty list, not an error;
    the frontend's own `PriceChart` renders an explicit "not streaming"
    empty state for that case rather than a blank chart.
    """
    rows = (
        db.query(PriceBar)
        .filter(PriceBar.instrument_id == instrument_id, PriceBar.timeframe == timeframe)
        .order_by(PriceBar.bucket_start.desc())
        .limit(limit)
        .all()
    )
    return list(reversed(rows))


@router.get("/streaming-symbols", response_model=StreamingSymbolsOut)
def get_streaming_symbols(
    user: User = Depends(require_permission("strategy.view")),
) -> StreamingSymbolsOut:
    """The underlying symbols `market_data.registry` is actually ingesting
    right now — drives the chart's symbol picker so it never offers one
    that's genuinely dead. See `registry.subscribed_symbols`'s own
    docstring.
    """
    return StreamingSymbolsOut(symbols=sorted(market_data_registry.subscribed_symbols()))
