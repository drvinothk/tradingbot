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

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Numeric, String
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
    this codebase (`SafeMode`, `ExecutionMode`, etc.) — validated at
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


# 2026-08-20 originals, still the seed/fallback values -- match
# RiskDefaults (settings.py) so a freshly-bootstrapped workspace with no
# row here yet behaves identically to before this table existed.
DEFAULT_DAILY_BUDGET_AMOUNT: float = 50_000.0
DEFAULT_DAILY_TARGET_PROFIT: float = 5_000.0
DEFAULT_DAILY_LOSS_CAP: float = 5_000.0
DEFAULT_FUNDING_MODE: str = "cash"  # FundingMode.CASH.value -- see module note below


class GlobalDailyLimitsConfig(Base, UUIDPkMixin, TimestampMixin):
    """One row per workspace -- a read/update-able settings surface for the
    session Daily Plan's *default* values (budget, target profit, loss cap,
    funding mode), following the exact same DB-backed-settings-row pattern
    as `InstrumentFirewallConfig`/`MarketDataProviderPreference` above (GET
    returns the row or a documented default when none exists yet, PATCH
    upserts + audits).

    2026-08-30 consolidation: originally shipped 2026-08-20 as "total daily
    budget" + "total lots per day", a settings surface with no enforcement
    wiring at all (see git history for that version's docstring). Redesigned
    after a design discussion landed on a clearer split of concerns:
    - `daily_max_lots` is dropped entirely -- superseded by a genuine
      per-strategy lot cap (`StrategyConfig.params["qty_lots"]`, enforced in
      `risk_engine.service.evaluate_trade_intent` via `resolve_qty_lots`),
      which closes the real gap a single global lot number never could (a
      wrong-sized entry on strategy A no longer needs to be judged against
      every other strategy's combined allowance).
    - `daily_budget_amount`/`daily_target_profit`/`daily_loss_cap`/
      `funding_mode` now mirror `TradingSession`'s own Daily Plan fields
      1:1 (see that class's docstring) and serve as their **default
      values** -- read by `session.bootstrapper._bootstrap_workspace` (the
      path that actually creates each day's session) and
      `api.v1.sessions.create_session`, falling back to `RiskDefaults`
      (env-settings, requires a restart to change) only when no row exists
      yet for the workspace. This is still a defaults surface, not a
      second enforcement path -- the *actual* daily_loss_cap/
      daily_target_profit/budget_amount checks in `risk_engine.service`
      read the values already sitting on that day's `TradingSession` row
      (set from these defaults at creation, then independently editable
      per-session via `POST /sessions/{id}/daily-plan` same as before).
      Editing this console changes what *tomorrow's* (or the next
      freshly-created) session starts with; it never rewrites an
      already-active session's own Daily Plan.
    """

    __tablename__ = "global_daily_limits_configs"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"), unique=True)
    daily_budget_amount: Mapped[float] = mapped_column(Numeric(14, 2))
    daily_target_profit: Mapped[float] = mapped_column(Numeric(14, 2))
    daily_loss_cap: Mapped[float] = mapped_column(Numeric(14, 2))
    funding_mode: Mapped[str] = mapped_column(String(10), default=DEFAULT_FUNDING_MODE)


class MarketDataDiagnosticRun(Base, UUIDPkMixin, TimestampMixin):
    """2026-08-22. One row per "Test Default"/"Test Failback" click (`POST
    /market-data/diagnostic/start`) — see `market_data.diagnostic_session`'s
    own module docstring for the full mechanism. `role` is `"default"` |
    `"failback"`, never a broker name directly — the whole point (per
    explicit user request) is that these buttons never hardcode which
    provider they test, only *which slot* (primary vs. failover backup),
    resolved fresh from `Settings.market_data.provider`/
    `failover_backup_provider` at start time. `provider` records whichever
    concrete name that resolved to *at that moment*, purely for the report
    — a mid-run config change is never retroactively relabeled.
    """

    __tablename__ = "market_data_diagnostic_runs"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id"))
    role: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20))  # "running" | "stopped" | "error"
    detail: Mapped[str] = mapped_column(String(500), default="")

    __table_args__ = (
        Index("ix_market_data_diagnostic_runs_workspace_started", "workspace_id", "started_at"),
    )


class MarketDataDiagnosticSnapshot(Base, UUIDPkMixin):
    """One row per (run, symbol) roughly every 30s while a run is active —
    deliberately not every raw tick (which could be several rows a second
    per symbol across a full trading day for no analytical benefit this
    report actually needs) — see `diagnostic_session._SNAPSHOT_INTERVAL_
    SECONDS`. No `TimestampMixin`: `recorded_at` already carries the "when"
    this row means, same reasoning as `ScripMasterSyncLog.run_at`.
    """

    __tablename__ = "market_data_diagnostic_snapshots"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("market_data_diagnostic_runs.id"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(30))
    connected: Mapped[bool] = mapped_column(Boolean)
    ltp: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    tick_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_market_data_diagnostic_snapshots_run_recorded", "run_id", "recorded_at"),
    )
