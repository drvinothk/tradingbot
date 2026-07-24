"""trading sessions, mode transitions, and the audit event log

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-24

Phase 0 needs a working, audited safe-mode state machine (paper_only /
kill_switch at minimum) even though the rest of the trading domain (Phase 1+)
doesn't exist yet — mode lives on trading_sessions per the design, so that
table (with its full column set, since splitting it across two later
migrations would buy nothing) comes in now alongside the audit log.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trading_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column(
            "broker_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("broker_accounts.id"),
            nullable=False,
        ),
        sa.Column(
            "started_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("mode", sa.String(30), nullable=False, server_default="paper_only"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("prior_mode", sa.String(30), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cutoff_time", sa.Time(), nullable=False, server_default="15:20:00"),
        sa.Column("budget_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("daily_target_profit", sa.Numeric(14, 2), nullable=False),
        sa.Column("daily_loss_cap", sa.Numeric(14, 2), nullable=False),
        sa.Column("funding_mode", sa.String(10), nullable=False, server_default="cash"),
        sa.Column("entries_paused_reason", sa.String(30), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_trading_sessions_broker_account", "trading_sessions", ["broker_account_id"]
    )

    op.create_table(
        "session_mode_transitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "trading_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trading_sessions.id"),
            nullable=False,
        ),
        sa.Column("from_mode", sa.String(30), nullable=True),
        sa.Column("to_mode", sa.String(30), nullable=False),
        sa.Column("trigger_type", sa.String(20), nullable=False),
        sa.Column(
            "triggered_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("reason", sa.String(500), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_session_mode_transitions_session",
        "session_mode_transitions",
        ["trading_session_id"],
    )

    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False, unique=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_category", sa.String(40), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("entity_type", sa.String(60), nullable=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("trading_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "broker_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("broker_accounts.id"),
            nullable=True,
        ),
        sa.Column("strategy_config_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("prev_hash", sa.String(64), nullable=True),
        sa.Column("hash", sa.String(64), nullable=False, unique=True),
    )
    op.create_index("ix_audit_events_workspace_ts", "audit_events", ["workspace_id", "ts"])
    op.create_index("ix_audit_events_actor", "audit_events", ["actor_type", "actor_id"])
    op.create_index("ix_audit_events_entity", "audit_events", ["entity_type", "entity_id"])
    op.create_index(
        "ix_audit_events_trading_session", "audit_events", ["trading_session_id"]
    )
    op.create_index(
        "ix_audit_events_broker_account", "audit_events", ["broker_account_id"]
    )
    op.create_index(
        "ix_audit_events_strategy_config", "audit_events", ["strategy_config_id"]
    )


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("session_mode_transitions")
    op.drop_table("trading_sessions")
