"""instrument_firewall_configs

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-14

Ops-Hardening Phase 7. One row per workspace -- the DB-backed replacement
for what would otherwise be a hardcoded live-instrument allowlist (no such
hardcode actually existed anywhere in this codebase before this migration;
see InstrumentFirewallConfig's own docstring, app/domain/ops/models.py).
Every existing workspace is seeded with the default ["NIFTY"] here, so
GET /system-settings/instrument-firewall returns real, meaningful data
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

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_ACTIVE_LIVE_INSTRUMENTS = ["NIFTY"]

instrument_firewall_configs_table = sa.table(
    "instrument_firewall_configs",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("workspace_id", postgresql.UUID(as_uuid=True)),
    sa.column("active_live_instruments", postgresql.JSONB),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
workspaces_table = sa.table("workspaces", sa.column("id", postgresql.UUID(as_uuid=True)))


def upgrade() -> None:
    op.create_table(
        "instrument_firewall_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("active_live_instruments", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    conn = op.get_bind()
    workspace_ids = [row[0] for row in conn.execute(sa.select(workspaces_table.c.id))]
    if workspace_ids:
        now = datetime.now(UTC)
        op.bulk_insert(
            instrument_firewall_configs_table,
            [
                {
                    "id": uuid.uuid4(),
                    "workspace_id": workspace_id,
                    "active_live_instruments": DEFAULT_ACTIVE_LIVE_INSTRUMENTS,
                    "created_at": now,
                    "updated_at": now,
                }
                for workspace_id in workspace_ids
            ],
        )


def downgrade() -> None:
    op.drop_table("instrument_firewall_configs")
