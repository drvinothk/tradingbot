"""risk domain, strategy runtime domain, system_alerts, session P&L columns

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-25

Phase 2: strike-ranking engine (no schema of its own — reads option_contracts/
quote_ticks/option_chain_snapshots/depth_snapshots from 0004), Risk Service
(risk_limit_configs, risk_decisions), the Signal->TradeIntent->RiskDecision
skeleton (strategy_configs, strategy_runs, signals, trade_intents,
pending_trade_approvals), system_alerts (pulled forward from the full Ops
schema so a limit breach has somewhere to become visible), and
synthetic_trade_outcomes (a Phase-2-only stand-in for Phase 3's real
trade_outcomes — see app/domain/strategy/models.py's module docstring and the
Phase 2 amendment note in docs/architecture/build-plan.md).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("trading_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("category", sa.String(60), nullable=False),
        sa.Column("message", sa.String(1000), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_system_alerts_workspace_created", "system_alerts", ["workspace_id", "created_at"]
    )
    op.create_index("ix_system_alerts_trading_session", "system_alerts", ["trading_session_id"])

    op.create_table(
        "risk_limit_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("max_concurrent_positions", sa.Integer(), nullable=False),
        sa.Column("max_trades_per_day", sa.Integer(), nullable=False),
        sa.Column("consecutive_loss_pause_threshold", sa.Integer(), nullable=False),
        sa.Column("daily_loss_cap", sa.Numeric(14, 2), nullable=False),
        sa.Column("daily_target_profit", sa.Numeric(14, 2), nullable=False),
        sa.Column("per_trade_lot_cap", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_risk_limit_configs_workspace_active",
        "risk_limit_configs",
        ["workspace_id", "is_active"],
    )

    op.create_table(
        "strategy_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(30), nullable=False, server_default="research"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "name", name="uq_strategy_config_name"),
    )

    op.create_table(
        "strategy_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "strategy_config_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_configs.id"),
            nullable=False,
        ),
        sa.Column(
            "trading_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trading_sessions.id"),
            nullable=False,
        ),
        sa.Column("execution_mode", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="scanning"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "started_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
    )
    op.create_index("ix_strategy_runs_session", "strategy_runs", ["trading_session_id"])
    op.create_index("ix_strategy_runs_config", "strategy_runs", ["strategy_config_id"])

    op.create_table(
        "signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column(
            "strategy_config_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_configs.id"),
            nullable=False,
        ),
        sa.Column(
            "strategy_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_runs.id"),
            nullable=False,
        ),
        sa.Column(
            "trading_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trading_sessions.id"),
            nullable=False,
        ),
        sa.Column(
            "option_contract_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("option_contracts.id"),
            nullable=False,
        ),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("entry_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("stop_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("target_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("qty_lots", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_signals_run", "signals", ["strategy_run_id"])
    op.create_index("ix_signals_session", "signals", ["trading_session_id"])

    op.create_table(
        "trade_intents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column(
            "signal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("signals.id"), nullable=False
        ),
        sa.Column(
            "strategy_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_runs.id"),
            nullable=False,
        ),
        sa.Column(
            "trading_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trading_sessions.id"),
            nullable=False,
        ),
        sa.Column(
            "option_contract_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("option_contracts.id"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("qty_lots", sa.Integer(), nullable=False),
        sa.Column("entry_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("stop_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("target_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending_risk"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_trade_intent_idempotency_key"),
    )
    op.create_index("ix_trade_intents_session", "trade_intents", ["trading_session_id"])
    op.create_index("ix_trade_intents_run", "trade_intents", ["strategy_run_id"])
    op.create_index("ix_trade_intents_contract", "trade_intents", ["option_contract_id"])

    op.create_table(
        "risk_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column(
            "trade_intent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trade_intents.id"),
            nullable=False,
        ),
        sa.Column(
            "risk_limit_config_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("risk_limit_configs.id"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column("reasons", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("checked_margin", sa.Boolean(), nullable=False),
        sa.Column("funding_mode", sa.String(10), nullable=False),
        sa.Column("capital_required", sa.Numeric(14, 2), nullable=False),
        sa.Column("breakeven_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("pnl_scenarios", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_risk_decisions_trade_intent", "risk_decisions", ["trade_intent_id"])
    op.create_index(
        "ix_risk_decisions_workspace_created", "risk_decisions", ["workspace_id", "created_at"]
    )

    op.create_table(
        "pending_trade_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "trade_intent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trade_intents.id"),
            nullable=False,
        ),
        sa.Column(
            "strategy_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_runs.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("capital_required", sa.Numeric(14, 2), nullable=False),
        sa.Column("breakeven_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("pnl_scenarios", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "decided_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("trade_intent_id", name="uq_pending_trade_approval_intent"),
    )
    op.create_index(
        "ix_pending_trade_approvals_run", "pending_trade_approvals", ["strategy_run_id"]
    )

    op.create_table(
        "synthetic_trade_outcomes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "trade_intent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trade_intents.id"),
            nullable=False,
        ),
        sa.Column("realized_pnl", sa.Numeric(14, 2), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("trade_intent_id", name="uq_synthetic_trade_outcome_intent"),
    )

    op.add_column(
        "trading_sessions",
        sa.Column("cumulative_realized_pnl", sa.Numeric(14, 2), nullable=False, server_default="0"),
    )
    op.add_column(
        "trading_sessions",
        sa.Column("consecutive_losses", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("trading_sessions", "consecutive_losses")
    op.drop_column("trading_sessions", "cumulative_realized_pnl")
    op.drop_table("synthetic_trade_outcomes")
    op.drop_table("pending_trade_approvals")
    op.drop_table("risk_decisions")
    op.drop_table("trade_intents")
    op.drop_table("signals")
    op.drop_table("strategy_runs")
    op.drop_table("strategy_configs")
    op.drop_table("risk_limit_configs")
    op.drop_table("system_alerts")
