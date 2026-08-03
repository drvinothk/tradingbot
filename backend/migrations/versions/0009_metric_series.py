"""metric_series

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-03

Addendum hardening batch: the Ops schema table has existed on paper since
Phase 2 (see domain/ops/models.py's own module docstring) but nothing ever
wrote to it. The periodic health-check loop (scheduler/health_check.py) is
the first real writer, via modules/ops/metrics_service.py's record_metric.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "metric_series",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("trading_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("metric_name", sa.String(100), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index(
        "ix_metric_series_workspace_name_recorded",
        "metric_series",
        ["workspace_id", "metric_name", "recorded_at"],
    )
    op.create_index(
        "ix_metric_series_trading_session", "metric_series", ["trading_session_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_metric_series_trading_session", table_name="metric_series")
    op.drop_index("ix_metric_series_workspace_name_recorded", table_name="metric_series")
    op.drop_table("metric_series")
