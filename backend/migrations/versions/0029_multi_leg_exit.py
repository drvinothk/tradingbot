"""position_exit_legs + trade_outcomes leg link + signals/trade_intents.exit_legs

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-30

Multi-leg exit engine (see the plan at .claude/plans/purring-wiggling-marble.md
and `PositionExitLeg`'s docstring in app/domain/execution/models.py). A
`Position` gains one or more `position_exit_legs` rows, each managing its own
stop/target/structure/trail against its own sub-lot slice and producing its
own `TradeOutcome` row.

`trade_outcomes`:
- drop `uq_trade_outcome_position` (a staged trade now has one outcome per leg)
- add nullable `position_exit_leg_id` FK (NULL for legacy single-outcome rows)
- add `uq_trade_outcome_position_leg` — one outcome per (position, leg). Existing
  rows all have `position_exit_leg_id IS NULL`; Postgres treats NULLs as
  distinct, and there is exactly one legacy outcome per position, so the swap
  is safe against live data with no backfill.

`signals`/`trade_intents`: nullable `exit_legs` JSONB — the frozen per-leg spec
(NULL = single full-qty exit, i.e. today's behaviour). `trade_intents.exit_legs`
is the copy execution reads at dispatch.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "position_exit_legs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("positions.id"),
            nullable=False,
        ),
        sa.Column("leg_index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False, server_default="single"),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("stop_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("target_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("structure_level", sa.Numeric(12, 4), nullable=True),
        sa.Column("structure_break_buffer", sa.Numeric(12, 4), nullable=True),
        sa.Column("structure_break_persistence_seconds", sa.Numeric(6, 2), nullable=True),
        sa.Column("trail_activation_fraction", sa.Numeric(6, 4), nullable=True),
        sa.Column("trail_lock_fraction", sa.Numeric(6, 4), nullable=True),
        sa.Column("max_loss_per_lot", sa.Numeric(14, 2), nullable=True),
        sa.Column("time_stop_minutes", sa.Numeric(6, 2), nullable=True),
        sa.Column("trail_status", sa.String(20), nullable=False, server_default="inactive"),
        sa.Column("trail_activation_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("trail_current_stop_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("structure_break_candidate_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("structure_break_candidate_extreme", sa.Numeric(12, 4), nullable=True),
        sa.Column("resting_order_id", sa.String(60), nullable=True),
        sa.Column("resting_order_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="open"),
        sa.Column(
            "closing_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id"),
            nullable=True,
        ),
        sa.Column("realized_pnl", sa.Numeric(14, 2), nullable=True),
        sa.Column("slippage", sa.Numeric(14, 2), nullable=True),
        sa.Column("exit_reason", sa.String(20), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("position_id", "leg_index", name="uq_exit_leg_position_index"),
    )
    op.create_index(
        "ix_position_exit_legs_position_status",
        "position_exit_legs",
        ["position_id", "status"],
    )

    op.drop_constraint("uq_trade_outcome_position", "trade_outcomes", type_="unique")
    op.add_column(
        "trade_outcomes",
        sa.Column(
            "position_exit_leg_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("position_exit_legs.id"),
            nullable=True,
        ),
    )
    op.create_unique_constraint(
        "uq_trade_outcome_position_leg",
        "trade_outcomes",
        ["position_id", "position_exit_leg_id"],
    )

    op.add_column("signals", sa.Column("exit_legs", postgresql.JSONB(), nullable=True))
    op.add_column("trade_intents", sa.Column("exit_legs", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("trade_intents", "exit_legs")
    op.drop_column("signals", "exit_legs")

    op.drop_constraint("uq_trade_outcome_position_leg", "trade_outcomes", type_="unique")
    op.drop_column("trade_outcomes", "position_exit_leg_id")
    op.create_unique_constraint(
        "uq_trade_outcome_position", "trade_outcomes", ["position_id"]
    )

    op.drop_index(
        "ix_position_exit_legs_position_status", table_name="position_exit_legs"
    )
    op.drop_table("position_exit_legs")
