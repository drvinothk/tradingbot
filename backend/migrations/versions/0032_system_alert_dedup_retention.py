"""system_alerts dedup + retention columns

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-03

Adds dedup_key/occurrence_count/last_seen_at to system_alerts so
alerting.manager.send_alert can collapse a recurring issue into one row
(occurrence_count++) instead of inserting a new row on every occurrence, and
the new scheduler.alert_housekeeping.AlertHousekeepingScheduler can
auto-resolve a row after system_alert_collapse_window_hours of no
recurrence and purge a resolved row after system_alert_retention_days.

last_seen_at is backfilled from created_at for every existing row -- every
row written before this migration is already older than the 24h collapse
window, so on the housekeeping scheduler's first run they all auto-resolve,
then age out under the normal 30-day purge rule. No manual cleanup needed
for the pre-existing backlog.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("system_alerts", sa.Column("dedup_key", sa.String(300), nullable=True))
    op.add_column(
        "system_alerts",
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "system_alerts", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute("UPDATE system_alerts SET last_seen_at = created_at")
    op.alter_column("system_alerts", "last_seen_at", nullable=False)
    op.create_index(
        "ix_system_alerts_workspace_dedup", "system_alerts", ["workspace_id", "dedup_key"]
    )


def downgrade() -> None:
    op.drop_index("ix_system_alerts_workspace_dedup", table_name="system_alerts")
    op.drop_column("system_alerts", "last_seen_at")
    op.drop_column("system_alerts", "occurrence_count")
    op.drop_column("system_alerts", "dedup_key")
