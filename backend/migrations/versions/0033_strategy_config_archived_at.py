"""strategy_configs.archived_at

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-04

Adds the "put this away" archive state to strategy_configs, distinct from
the existing is_enabled quick-pause toggle -- see StrategyConfig.archived_at's
own docstring. Nullable, no backfill needed (every existing row is
un-archived).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "strategy_configs", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("strategy_configs", "archived_at")
