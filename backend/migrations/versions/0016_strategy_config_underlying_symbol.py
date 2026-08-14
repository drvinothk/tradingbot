"""strategy_configs.underlying_symbol

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-14

Ops-Hardening Phase 6 (Auto-Spawner). Nullable, no backfill -- every existing
row predates any notion of "which underlying does this strategy trade" (a
human always supplied `instrument_id` at `POST /strategies/{id}/start` time,
never persisted at the config level). `None` means "not yet configured for
auto-spawn" -- the daily auto-spawner alerts and skips any `is_enabled`
config still missing this, rather than guessing an underlying.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "strategy_configs",
        sa.Column("underlying_symbol", sa.String(length=60), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("strategy_configs", "underlying_symbol")
