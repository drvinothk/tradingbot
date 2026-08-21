"""global_daily_limits_configs

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-20

One row per workspace -- the DB-backed, read/update-able global "total
daily budget" / "total lots per day" settings surface, following the same
seed-every-existing-workspace pattern as migration 0017's
instrument_firewall_configs. See GlobalDailyLimitsConfig's own docstring
(app/domain/ops/models.py) for why this is a new table rather than a
rename of RiskLimitConfig.per_trade_lot_cap or TradingSession.budget_amount.
Every existing workspace is seeded here with the current defaults (50000.0
budget, 1 lot) so GET /system-settings/daily-limits returns real data
immediately rather than an empty/missing row on a database that's been
running since before this migration.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_DAILY_BUDGET_AMOUNT = 50_000.0
DEFAULT_DAILY_MAX_LOTS = 1

global_daily_limits_configs_table = sa.table(
    "global_daily_limits_configs",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("workspace_id", postgresql.UUID(as_uuid=True)),
    sa.column("daily_budget_amount", sa.Numeric(14, 2)),
    sa.column("daily_max_lots", sa.Integer),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
workspaces_table = sa.table("workspaces", sa.column("id", postgresql.UUID(as_uuid=True)))


def upgrade() -> None:
    op.create_table(
        "global_daily_limits_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("daily_budget_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("daily_max_lots", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    conn = op.get_bind()
    workspace_ids = [row[0] for row in conn.execute(sa.select(workspaces_table.c.id))]
    if workspace_ids:
        now = datetime.now(UTC)
        op.bulk_insert(
            global_daily_limits_configs_table,
            [
                {
                    "id": uuid.uuid4(),
                    "workspace_id": workspace_id,
                    "daily_budget_amount": DEFAULT_DAILY_BUDGET_AMOUNT,
                    "daily_max_lots": DEFAULT_DAILY_MAX_LOTS,
                    "created_at": now,
                    "updated_at": now,
                }
                for workspace_id in workspace_ids
            ],
        )


def downgrade() -> None:
    op.drop_table("global_daily_limits_configs")
