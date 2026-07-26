"""execution domain (orders/order_events/positions/stop_plans/trail_plans/
trade_outcomes), broker-sync domain (broker_sync_states/reconciliation_runs),
drop synthetic_trade_outcomes

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-25

Phase 3: the real Order/Position/StopPlan/TrailPlan/TradeOutcome lifecycle
that replaces Phase 2's synthetic_trade_outcomes stand-in (see
app/domain/strategy/models.py's and app/domain/execution/models.py's module
docstrings), plus broker_sync_states/reconciliation_runs for Reconciliation
Service.

orders <-> positions is a circular FK pair (an entry Order creates a
Position; a Position's closing_order_id points back at the exit Order) —
orders.position_id is created without its FK constraint, positions is
created next (its FKs to orders.id are fine, orders already exists), and the
orders.position_id FK is added last via a separate op, mirroring the
use_alter=True the ORM model declares for the same column.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
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
        sa.Column(
            "trade_intent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trade_intents.id"),
            nullable=True,
        ),
        # FK to positions.id added below, after positions exists.
        sa.Column("position_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False, server_default="paper"),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("order_type", sa.String(20), nullable=False, server_default="market"),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("limit_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("trigger_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("filled_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_fill_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("broker_order_id", sa.String(60), nullable=False, server_default=""),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_order_idempotency_key"),
        sa.CheckConstraint(
            "(trade_intent_id IS NOT NULL) <> (position_id IS NOT NULL)",
            name="ck_order_exactly_one_of_intent_or_position",
        ),
    )
    op.create_index("ix_orders_session", "orders", ["trading_session_id"])
    op.create_index("ix_orders_trade_intent", "orders", ["trade_intent_id"])
    op.create_index("ix_orders_position", "orders", ["position_id"])

    op.create_table(
        "positions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
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
        sa.Column(
            "trade_intent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trade_intents.id"),
            nullable=False,
        ),
        sa.Column(
            "opening_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id"),
            nullable=False,
        ),
        sa.Column(
            "closing_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id"),
            nullable=True,
        ),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("entry_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="open"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("trade_intent_id", name="uq_position_trade_intent"),
    )
    op.create_index("ix_positions_session_status", "positions", ["trading_session_id", "status"])
    op.create_index("ix_positions_option_contract", "positions", ["option_contract_id"])

    op.create_foreign_key(
        "fk_orders_position_id", "orders", "positions", ["position_id"], ["id"]
    )

    op.create_table(
        "order_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id"), nullable=False
        ),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_order_events_order", "order_events", ["order_id"])

    op.create_table(
        "stop_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("positions.id"),
            nullable=False,
        ),
        sa.Column("stop_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="confirmed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("position_id", name="uq_stop_plan_position"),
    )

    op.create_table(
        "trail_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("positions.id"),
            nullable=False,
        ),
        sa.Column("trail_type", sa.String(40), nullable=False),
        sa.Column("activation_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("trail_value", sa.Numeric(12, 4), nullable=False),
        sa.Column("current_stop_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="inactive"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("position_id", name="uq_trail_plan_position"),
    )

    op.create_table(
        "trade_outcomes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column(
            "trading_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trading_sessions.id"),
            nullable=False,
        ),
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("positions.id"),
            nullable=False,
        ),
        sa.Column(
            "trade_intent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trade_intents.id"),
            nullable=False,
        ),
        sa.Column("entry_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("exit_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(14, 2), nullable=False),
        sa.Column("slippage", sa.Numeric(14, 2), nullable=False),
        sa.Column("exit_reason", sa.String(20), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("position_id", name="uq_trade_outcome_position"),
    )
    op.create_index("ix_trade_outcomes_session", "trade_outcomes", ["trading_session_id"])

    op.create_table(
        "broker_sync_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
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
        sa.Column("local_qty", sa.Integer(), nullable=False),
        sa.Column("broker_qty", sa.Integer(), nullable=False),
        sa.Column("is_mismatched", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "trading_session_id",
            "option_contract_id",
            name="uq_broker_sync_state_session_contract",
        ),
    )

    op.create_table(
        "reconciliation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column(
            "trading_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trading_sessions.id"),
            nullable=False,
        ),
        sa.Column("trigger_type", sa.String(10), nullable=False),
        sa.Column("mismatches_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("action_taken", sa.String(60), nullable=False, server_default="none"),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_reconciliation_runs_session", "reconciliation_runs", ["trading_session_id"]
    )

    # Phase-2-only stand-in, fully replaced by the above — see this
    # migration's and app/domain/execution/models.py's module docstrings.
    op.drop_table("synthetic_trade_outcomes")


def downgrade() -> None:
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

    op.drop_table("reconciliation_runs")
    op.drop_table("broker_sync_states")
    op.drop_table("trade_outcomes")
    op.drop_table("trail_plans")
    op.drop_table("stop_plans")
    op.drop_table("order_events")
    op.drop_constraint("fk_orders_position_id", "orders", type_="foreignkey")
    op.drop_table("positions")
    op.drop_table("orders")
