"""Ops domain. `SystemAlert` is pulled forward from the full Ops schema
(system_alerts / metric_series / scheduler_job_runs) because Phase 2's Risk
Service needs somewhere to make a limit breach visible beyond the audit log.
`MetricSeries` follows in the Addendum hardening batch, the first real writer
being the periodic health-check loop (`scheduler/health_check.py`) — see
`modules/ops/metrics_service.py`'s `record_metric`. `scheduler_job_runs`
stays out of scope until whatever phase actually needs it.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, TimestampMixin, UUIDPkMixin


class AlertSeverity(enum.StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class SystemAlert(Base, UUIDPkMixin):
    __tablename__ = "system_alerts"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    trading_session_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    severity: Mapped[AlertSeverity] = mapped_column(String(20))
    category: Mapped[str] = mapped_column(String(60))
    message: Mapped[str] = mapped_column(String(1000))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("ix_system_alerts_workspace_created", "workspace_id", "created_at"),
        Index("ix_system_alerts_trading_session", "trading_session_id"),
    )


class MetricSeries(Base, UUIDPkMixin):
    __tablename__ = "metric_series"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    trading_session_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    metric_name: Mapped[str] = mapped_column(String(100))
    value: Mapped[float] = mapped_column(Float)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tags: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        Index(
            "ix_metric_series_workspace_name_recorded",
            "workspace_id",
            "metric_name",
            "recorded_at",
        ),
        Index("ix_metric_series_trading_session", "trading_session_id"),
    )


class MarketDataProviderPreference(Base, UUIDPkMixin, TimestampMixin):
    """Ops-Hardening Phase 4. One row per workspace — a manual override on
    top of `FailoverMarketDataProvider`'s own automatic health-based
    switching (`app.modules.market_data.providers.failover`), not a
    replacement for it: this records which provider the user wants active
    *right now* (e.g. "Shoonya's having a bad day, force Angel One"), read
    at `provider_composition.get_market_data_provider()` construction time
    to seed the initial override, and applied live via
    `FailoverMarketDataProvider.set_manual_override` by the PATCH endpoint
    whenever a live failover-wrapped singleton already exists. `String(30)`,
    not a native Postgres enum, matching every other enum-shaped column in
    this codebase (`StrategyStatus`, `ExecutionMode`, etc.) — validated at
    the API layer against `provider_composition._RECOGNIZED_FAILOVER_
    BACKUPS`-equivalent values, not a DB constraint.
    """

    __tablename__ = "market_data_provider_preferences"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"), unique=True)
    # Nullable, same "None means no override" convention as
    # StrategyConfig.runtime_mode (Ops-Hardening Phase 1) -- not an empty
    # string sentinel.
    active_provider: Mapped[str | None] = mapped_column(String(30), nullable=True)


# Ops-Hardening Phase 7: the only two underlyings this system ever trades
# (same scoping as api.v1.shoonya._KNOWN_UNDERLYINGS / adapter.py's
# KNOWN_UNDERLYINGS) -- used both as the DB seed value and as the fallback
# `broker_adapter.composition._instrument_firewall_allows` returns to when a
# workspace genuinely has no row yet (never fail open to "everything
# allowed").
DEFAULT_ACTIVE_LIVE_INSTRUMENTS: tuple[str, ...] = ("NIFTY",)


class InstrumentFirewallConfig(Base, UUIDPkMixin, TimestampMixin):
    """Ops-Hardening Phase 7. One row per workspace -- the DB-backed
    replacement for what would otherwise be a hardcoded instrument allowlist.
    `get_execution_broker` (`broker_adapter/composition.py`) checks this on
    every *new* live-money dispatch (never on closes/reconciliation/margin
    reads, which have no "which instrument is this opening" question to ask)
    before routing a trade to the real broker -- an instrument not on this
    list gets a `ConfigurationError`, never a silent downgrade to paper, same
    "never silently fall back for a live-intent trade" principle
    `ALLOW_REAL_MONEY_DISPATCH`/the 1-lot hardcap already established in
    Phase 5.
    """

    __tablename__ = "instrument_firewall_configs"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"), unique=True)
    # JSONB list of Instrument.symbol strings, matching every other
    # list/dict-shaped column in this codebase (StrategyConfig.params,
    # OptionChainSnapshot.chain_data) rather than a native Postgres ARRAY.
    active_live_instruments: Mapped[list[str]] = mapped_column(JSONB)


# 2026-08-20: seed/fallback values for GlobalDailyLimitsConfig below --
# same values as RiskDefaults.default_budget (settings.py) and
# RiskLimitConfig.per_trade_lot_cap's own default (1), reused here only as
# starting points for a *separate* concept (see that class's own
# docstring for why this isn't just an alias of either).
DEFAULT_DAILY_BUDGET_AMOUNT: float = 50_000.0
DEFAULT_DAILY_MAX_LOTS: int = 1


class GlobalDailyLimitsConfig(Base, UUIDPkMixin, TimestampMixin):
    """One row per workspace -- a read/update-able global settings surface
    for "total daily budget" and "total lots per day", following the exact
    same DB-backed-settings-row pattern as `InstrumentFirewallConfig`/
    `MarketDataProviderPreference` above (GET returns the row or a
    documented default when none exists yet, PATCH upserts + audits).

    Deliberately NOT a rename of an existing concept -- checked both
    candidates in `app.modules.risk_engine` before adding this table:
    `RiskLimitConfig.per_trade_lot_cap` caps lots *per individual trade*
    (versioned, already enforced in `evaluate_trade_intent`'s
    `per_trade_lot_cap_exceeded` check), not a running total across a
    day's worth of trades; `TradingSession.budget_amount` is a *per-session*
    value seeded once at session-creation time from
    `RiskDefaults.default_budget` (`app.config.settings`), not a live,
    workspace-wide setting a user can change mid-day the way this table's
    PATCH allows. Neither is "total lots dispatched today" or "the daily
    budget right now" in the workspace-global, always-current sense the UI
    dashboard plan asked for.

    Scope note: this is a settings surface only (mirrors the UI plan's
    "small new settings surface... displayed above the Strategy Control
    card" spec) -- reading/writing the two values, nothing more. It is not
    wired into any pre-trade enforcement path (unlike `per_trade_lot_cap`/
    `budget_amount`, which already are); wiring a real "total lots used
    today" / "total budget committed today" check into
    `risk_engine.evaluate_trade_intent` is a separate, not-yet-scoped
    follow-up, same "settings surface first, enforcement later" shape this
    codebase's own Global Execution Timings item is flagged the same way
    in the build plan.
    """

    __tablename__ = "global_daily_limits_configs"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"), unique=True)
    daily_budget_amount: Mapped[float] = mapped_column(Numeric(14, 2))
    daily_max_lots: Mapped[int] = mapped_column(Integer)
