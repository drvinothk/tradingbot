"""market_data_diagnostic_runs + market_data_diagnostic_snapshots

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-22

New tables backing the Market Terminal "Test Default"/"Test Failback" WS
quality diagnostic (see `MarketDataDiagnosticRun`/`MarketDataDiagnosticSnapshot`
docstrings in app/domain/ops/models.py, and
`app/modules/market_data/diagnostic_session.py`'s own module docstring for
the full design). No seed data — unlike migration 0021's per-workspace
defaults, a diagnostic run only ever exists once a user actually clicks
Test, so there is nothing to backfill for existing workspaces.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_data_diagnostic_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("detail", sa.String(500), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_market_data_diagnostic_runs_workspace_started",
        "market_data_diagnostic_runs",
        ["workspace_id", "started_at"],
    )

    op.create_table(
        "market_data_diagnostic_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("market_data_diagnostic_runs.id"),
            nullable=False,
        ),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(30), nullable=False),
        sa.Column("connected", sa.Boolean, nullable=False),
        sa.Column("ltp", sa.Numeric(12, 2), nullable=True),
        sa.Column("tick_ts", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_market_data_diagnostic_snapshots_run_recorded",
        "market_data_diagnostic_snapshots",
        ["run_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_market_data_diagnostic_snapshots_run_recorded",
        table_name="market_data_diagnostic_snapshots",
    )
    op.drop_table("market_data_diagnostic_snapshots")
    op.drop_index(
        "ix_market_data_diagnostic_runs_workspace_started",
        table_name="market_data_diagnostic_runs",
    )
    op.drop_table("market_data_diagnostic_runs")
